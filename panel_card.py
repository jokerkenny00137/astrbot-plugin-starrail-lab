"""把单个角色的面板渲染成一张图片角色卡。

渲染依赖 AstrBot 内置的 `Star.html_render()`（HTML + Jinja2 → 图片），
不打包任何游戏素材，头像 / 光锥 / 遗器图标全部走公共 CDN。

稳定性设计：
    1. 图片由插件自己下载并转成 base64 内嵌进 HTML。渲染端的浏览器不用再
       去外网拉图，既不会因为 CDN 慢而截出一张空图，也不受渲染节点网络限制。
    2. 图片带内存缓存，同一张图在 TTL 内只下一次。
    3. 渲染失败（非超时）会重试一次；超时不重试，避免总耗时翻倍。
    4. 任何一步失败都返回 None，由调用方回退到原来的文本输出。
"""

from __future__ import annotations

import asyncio
import base64
import random
import shutil
import time

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from astrbot.api import logger

try:
    import aiohttp
except Exception:  # 极简环境下没有 aiohttp 时跳过内联，退化为远程链接
    aiohttp = None  # type: ignore[assignment]

try:  # AstrBot 以包的形式加载插件
    from .relic_score import (
        KEY_WEIGHT_THRESHOLD,
        STAT_KEY_MAP,
        get_relic_profile,
        key_stat_names,
        score_relic,
    )
except ImportError:  # 以普通脚本方式被导入时
    from relic_score import (
        KEY_WEIGHT_THRESHOLD,
        STAT_KEY_MAP,
        get_relic_profile,
        key_stat_names,
        score_relic,
    )

CARD_DIR = Path(__file__).resolve().parent / "card"
TEMPLATE_FILE = CARD_DIR / "character.html"

# 单件遗器的满分数。六件全满是 60 分。
RELIC_MAX_SCORE = 10.0

# 远端渲染端点：AstrBot 官方端点列表。自带的 html_render 遇到卡住的端点
# 会一直等默认的 300 秒，这里自己做短超时轮询，失败快速切换。
T2I_LIST_URL = "https://api.soulter.top/astrbot/t2i-endpoints"
# 官方文档公开的两个端点。列表接口有时只返回其中一个（而且可能是 503 的那个），
# 所以这里内置兜底，轮询时逐个试。
T2I_DEFAULT_ENDPOINTS = [
    "https://t2i.rcfortress.site/text2img",
    "https://t2i.soulter.top/text2img",
]
T2I_DEFAULT_ENDPOINT = T2I_DEFAULT_ENDPOINTS[0]
_ENDPOINT_TTL = 600.0
_ENDPOINT_CACHE: Tuple[float, List[str]] = (0.0, [])

# 端点健康度：刚返回 5xx 的端点短时间内直接跳过。
# 官方公共端点过载时会连续 503，之前每个端点都还要重新连一次，
# 出图被拖到十几秒；记住失败后直接回退旧图/文字。
_ENDPOINT_COOLDOWN = 90.0
_ENDPOINT_FAILED_AT: Dict[str, float] = {}

# 公共素材库（jsDelivr 镜像，国内比 raw.githubusercontent.com 稳定）
ASSET_BASE = "https://cdn.jsdelivr.net/gh/Mar-7th/StarRailRes@master/icon"
# jsDelivr 对个别文件会返回 403，模板里用这个源兜底
ASSET_BASE_ALT = "https://raw.githubusercontent.com/Mar-7th/StarRailRes/master/icon"

# 属性 -> (主色, 柔光色)
ELEMENT_COLOR = {
    "Physical": ("#dfe4ee", "rgba(223,228,238,.26)"),
    "Fire": ("#ff8455", "rgba(255,132,85,.30)"),
    "Ice": ("#8ed8ff", "rgba(142,216,255,.28)"),
    "Thunder": ("#c48bff", "rgba(196,139,255,.30)"),
    "Wind": ("#6ee7b7", "rgba(110,231,183,.28)"),
    "Quantum": ("#b388ff", "rgba(179,136,255,.32)"),
    "Imaginary": ("#ffd76e", "rgba(255,215,110,.30)"),
}
DEFAULT_COLOR = ("#9db2ff", "rgba(157,178,255,.28)")

# 图片内联缓存：url -> (写入时间, data URI)
_IMAGE_CACHE: Dict[str, Tuple[float, str]] = {}
_IMAGE_TTL = 3600.0  # 缓存 1 小时
_IMAGE_TIMEOUT = 6.0  # 单张图下载超时
_IMAGE_MAX_BYTES = 2_000_000  # 超过 2MB 的图不内联（正常素材都在 40KB 内）

# 角色卡本地缓存目录（由 main.py 指到 AstrBot 的 plugin_data 下）
CARD_CACHE_DIR: Path = CARD_DIR.parent / "data" / "cards"
CARD_LOG_FILE: Optional[Path] = None

# 最近一次渲染失败的原因，供控制面板自检展示
LAST_ERROR: str = ""
LAST_SUCCESS_AT: float = 0.0


def set_card_cache_dir(path: str | Path) -> Path:
    """设置角色卡缓存目录，同时把渲染日志放到同一层。"""
    global CARD_CACHE_DIR, CARD_LOG_FILE
    CARD_CACHE_DIR = Path(path)
    CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CARD_LOG_FILE = CARD_CACHE_DIR.parent / "card_render.log"
    return CARD_CACHE_DIR


def _log(text: str) -> None:
    """把渲染相关的关键事件写进插件自己的日志文件，方便排查。

    AstrBot 主日志需要开 DEBUG 才能看到细节，这里单独记一份，路径固定、
    只追加，用户可以直接把文件发出来定位问题。
    """
    if not CARD_LOG_FILE:
        return
    try:
        CARD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CARD_LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def card_image_path(uid: Any, panel: Dict[str, Any]) -> Path:
    key = panel.get("avatar_id") or panel.get("name") or "unknown"
    return CARD_CACHE_DIR / str(uid) / f"{key}.png"


def card_html_path(uid: Any, panel: Dict[str, Any]) -> Path:
    key = panel.get("avatar_id") or panel.get("name") or "unknown"
    return CARD_CACHE_DIR / str(uid) / f"{key}.html"


def get_render_status() -> Dict[str, Any]:
    """给控制面板自检用的渲染状态。"""
    return {
        "last_error": LAST_ERROR,
        "last_success_at": LAST_SUCCESS_AT,
        "cache_dir": str(CARD_CACHE_DIR),
        "log_file": str(CARD_LOG_FILE) if CARD_LOG_FILE else "",
        "cached_cards": sum(1 for _ in CARD_CACHE_DIR.glob("*/*.png")),
    }


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------


def _num(value: float) -> str:
    return f"{value:,.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _affix(value: float, is_percent: bool) -> str:
    return _pct(value) if is_percent else f"{value:.1f}"


def _asset(path: str) -> str:
    return f"{ASSET_BASE}/{path}.png" if path else ""


def _asset_alt(path: str) -> str:
    return f"{ASSET_BASE_ALT}/{path}.png" if path else ""


def _avatar_url(avatar_id: Any) -> str:
    # 变身类角色的 avatarId 可能不在素材库里，模板侧有 onerror 兜底
    return _asset(f"avatar/{avatar_id}") if avatar_id else ""


def _light_cone_url(weapon_id: Any) -> str:
    return _asset(f"light_cone/{weapon_id}") if weapon_id else ""


def _relic_url(set_id: Any) -> str:
    # 素材库按 3 位套装 ID 命名，例如 129 = 闪耀功勋的魔法少女
    return _asset(f"relic/{set_id}") if set_id else ""


# --------------------------------------------------------------------------
# 数据装配
# --------------------------------------------------------------------------


def build_card_data(panel: Dict[str, Any], uid: str, nickname: str) -> Dict[str, Any]:
    """把 player_panel 的面板 dict 转成模板需要的结构。"""
    element_en = panel.get("element_en") or ""
    accent, accent_soft = ELEMENT_COLOR.get(element_en, DEFAULT_COLOR)
    element = panel.get("element") or ""

    stats: List[Dict[str, Any]] = [
        {"label": "生命值", "value": _num(panel.get("hp", 0)), "sub": ""},
        {"label": "击破特攻", "value": _pct(panel.get("break_effect", 0)), "sub": ""},
        {"label": "攻击力", "value": _num(panel.get("attack", 0)), "sub": ""},
        {"label": "治疗量加成", "value": _pct(panel.get("heal_bonus", 0)), "sub": ""},
        {"label": "防御力", "value": _num(panel.get("defence", 0)), "sub": ""},
        {
            "label": "能量恢复效率",
            "value": _pct(1 + panel.get("energy_regen", 0)),
            "sub": "",
        },
        {"label": "速度", "value": f"{panel.get('speed', 0):.1f}", "sub": ""},
        {"label": "效果命中", "value": _pct(panel.get("effect_hit", 0)), "sub": ""},
        {"label": "暴击率", "value": _pct(panel.get("crit_rate", 0)), "sub": ""},
        {"label": "效果抵抗", "value": _pct(panel.get("effect_res", 0)), "sub": ""},
        {"label": "暴击伤害", "value": _pct(panel.get("crit_dmg", 0)), "sub": ""},
        {
            "label": f"{element}属性伤害" if element else "属性伤害",
            "value": _pct(panel.get("element_dmg", 0)),
            "sub": "",
        },
    ]

    # 该角色的副词条权重（库里没收录就是 None，卡片不显示评分）
    profile = get_relic_profile(panel.get("avatar_id"))
    weight = profile["weight"] if profile else {}
    weight_max = profile["max"] if profile else 0.0

    relics: List[Dict[str, Any]] = []
    relic_scores: List[float] = []
    for relic in panel.get("relics", []):
        rarity = relic.get("rarity", 5)
        tier = "gold" if rarity >= 5 else ("purple" if rarity == 4 else "")
        subs = relic.get("subs", [])
        main_name = relic.get("main_name", "")

        # 主词条也算一次「关键属性」标注。新存档带 main_property，
        # 旧存档只能按中文名 + 是否百分比反推。
        main_property = relic.get("main_property", "")
        if not main_property and (
            relic.get("main_is_percent", True) or main_name == "速度"
        ):
            main_property = STAT_KEY_MAP.get(main_name, "")
        main_key = bool(weight) and (
            weight.get(main_property, 0.0) >= KEY_WEIGHT_THRESHOLD
        )

        # 原始分按该角色的理论上限归一化到 10 分制
        score = 0.0
        has_score = bool(profile and weight_max)
        if has_score:
            raw = score_relic(subs, weight)
            score = min(RELIC_MAX_SCORE, raw / weight_max * RELIC_MAX_SCORE)
            relic_scores.append(score)

        sub_items = [
            {
                "text": f"{s['name']} {_affix(s['value'], s['is_percent'])}",
                "key": bool(weight)
                and weight.get(s.get("property", ""), 0.0) >= KEY_WEIGHT_THRESHOLD,
            }
            for s in subs
        ]

        relics.append(
            {
                "tier": tier,
                "icon": _relic_url(relic.get("set_id")),
                "icon_alt": _asset_alt(f"relic/{relic.get('set_id')}") if relic.get("set_id") else "",
                "slot": relic.get("slot", ""),
                "name": relic.get("name", ""),
                "level": relic.get("level", 0),
                "main_name": main_name,
                "main_value": _affix(
                    relic.get("main_value", 0.0), relic.get("main_is_percent", True)
                ),
                "main_key": main_key,
                "subs": sub_items,
                "score": f"{score:.1f}" if has_score else "",
                "score_max": f"{RELIC_MAX_SCORE:.0f}" if has_score else "",
            }
        )

    # 六件遗器的总分，满分 10 × 件数
    relic_total = f"{sum(relic_scores):.1f}" if relic_scores else ""
    relic_total_max = f"{RELIC_MAX_SCORE * len(relic_scores):.0f}" if relic_scores else ""

    # 高权重属性，卡片上用金色标注
    key_labels = key_stat_names(weight) if weight else set()
    for stat in stats:
        stat["key"] = stat["label"] in key_labels

    # 套装件数统计：2 件以上单独列出，散件合并成一行
    counter: Dict[str, int] = {}
    for relic in panel.get("relics", []):
        name = relic.get("set_name", "")
        if name:
            counter[name] = counter.get(name, 0) + 1

    set_list = [
        {"name": name, "count": count, "loose": False}
        for name, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= 2
    ]
    loose_count = sum(1 for count in counter.values() if count < 2)
    if loose_count:
        set_list.append({"name": "散件", "count": loose_count, "loose": True})

    rarity = int(panel.get("rarity", 5) or 5)

    return {
        "nickname": nickname or "未知玩家",
        "uid": uid,
        "name": panel.get("name", ""),
        "level": panel.get("level", 0),
        "rank": panel.get("rank", 0),
        "element": element,
        "accent": accent,
        "accent_soft": accent_soft,
        "rarity": max(0, min(5, rarity)),
        "stars_text": "★" * max(0, min(5, rarity)),
        "avatar_url": _avatar_url(panel.get("avatar_id")),
        # 开拓者「记忆」命途（8008）在 jsDelivr 上是 403，需要 raw 源兜底
        "avatar_url_alt": (
            _asset_alt(f"avatar/{panel.get('avatar_id')}")
            if panel.get("avatar_id")
            else ""
        ),
        "weapon": {
            "name": panel.get("weapon_name") or "未装备",
            "level": panel.get("weapon_level", 0),
            "rank": panel.get("weapon_rank", 0),
            "icon": _light_cone_url(panel.get("weapon_id")),
            "icon_alt": (
                _asset_alt(f"light_cone/{panel.get('weapon_id')}")
                if panel.get("weapon_id")
                else ""
            ),
            "rarity": max(0, min(5, int(panel.get("weapon_rarity", 5) or 5))),
            "stars_text": "★" * max(0, min(5, int(panel.get("weapon_rarity", 5) or 5))),
        },
        "stats": stats,
        "relics": relics,
        "set_list": set_list,
        "relic_total": relic_total,
        "relic_total_max": relic_total_max,
    }


# --------------------------------------------------------------------------
# 图片内联
# --------------------------------------------------------------------------


async def _download_as_data_uri(session: Any, url: str) -> Optional[str]:
    """下载图片并转成 data URI。失败返回 None。"""
    if not url:
        return None
    if url.startswith("data:"):
        return url

    now = time.time()
    cached = _IMAGE_CACHE.get(url)
    if cached and now - cached[0] < _IMAGE_TTL:
        return cached[1]

    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
    except Exception:
        return None

    if not raw or len(raw) > _IMAGE_MAX_BYTES:
        return None

    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    _IMAGE_CACHE[url] = (now, uri)
    return uri


async def _load_image(session: Any, url: str, alt_url: str = "") -> Optional[str]:
    uri = await _download_as_data_uri(session, url)
    if uri is None and alt_url:
        uri = await _download_as_data_uri(session, alt_url)
    return uri


async def inline_card_images(data: Dict[str, Any]) -> int:
    """把卡片里的远程图片换成本地 base64，返回成功内联的张数。

    渲染端（远端浏览器）不需要再访问 CDN，图片缺失和空图问题会少很多。
    """
    if aiohttp is None:
        return 0

    jobs: List[Tuple[Dict[str, Any], str, str, str]] = []
    if data.get("avatar_url"):
        jobs.append(
            (
                data,
                "avatar_url",
                data["avatar_url"],
                data.get("avatar_url_alt", ""),
            )
        )

    weapon = data.get("weapon") or {}
    if weapon.get("icon"):
        jobs.append((weapon, "icon", weapon["icon"], weapon.get("icon_alt", "")))

    for relic in data.get("relics", []):
        if relic.get("icon"):
            jobs.append((relic, "icon", relic["icon"], relic.get("icon_alt", "")))

    if not jobs:
        return 0

    try:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *[_load_image(session, url, alt) for _, _, url, alt in jobs],
                return_exceptions=True,
            )
    except Exception:
        logger.exception("[星铁面板] 下载角色卡图片失败")
        return 0

    inlined = 0
    for (container, key, _, _), uri in zip(jobs, results):
        if isinstance(uri, str) and uri.startswith("data:"):
            container[key] = uri
            # 已经内嵌了，不需要模板里的远程兜底
            container[key + "_alt"] = ""
            inlined += 1
    return inlined


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_template() -> str:
    return TEMPLATE_FILE.read_text(encoding="utf-8")


async def build_card_html(
    panel: Dict[str, Any],
    uid: str,
    nickname: str = "",
    inline_images: bool = True,
) -> str:
    """本地生成完整角色卡 HTML（不依赖 html_render）。

    图片会被内嵌成 base64，所以保存下来的 HTML 单独打开也能正常显示，
    控制面板可以直接用 iframe 预览。
    """
    data = build_card_data(panel, uid, nickname)
    if inline_images:
        await inline_card_images(data)

    from jinja2 import Environment

    env = Environment(autoescape=True)
    return env.from_string(_load_template()).render(**data)


async def save_card_html(
    panel: Dict[str, Any],
    uid: str,
    nickname: str = "",
) -> Optional[Path]:
    """把角色卡 HTML 存到本地，返回文件路径。失败返回 None。"""
    path = card_html_path(uid, panel)
    try:
        html = await build_card_html(panel, uid, nickname)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return path
    except Exception as exc:
        _log(f"保存角色卡 HTML 失败（{uid} / {panel.get('name')}）：{type(exc).__name__}: {exc}")
        logger.warning(f"[星铁面板] 保存角色卡 HTML 失败：{type(exc).__name__}: {exc}")
        return None


def _inside_plugin_data(path: Path) -> bool:
    """路径是否落在插件自己的数据目录内（cards/、cache/ 等）。

    渲染结果里的本地路径一律要过这道检查：html_render 的返回值不应被用来
    读取插件数据目录之外的文件（例如伪造的 `file://` 路径）。
    """
    try:
        path.resolve().relative_to(CARD_CACHE_DIR.parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _local_path(result: str) -> Path:
    """把本地文件路径或 `file://` URL 转成 Path（兼容 Windows 盘符）。"""
    if result.startswith("file://"):
        parsed = urlparse(result)
        raw = url2pathname(unquote(parsed.path))
        if parsed.netloc:  # file://server/share/...
            raw = f"//{parsed.netloc}{raw}"
        return Path(raw)
    return Path(result)


async def _store_image(result: str, path: Path) -> bool:
    """把 html_render 的结果（URL 或本地文件）落到缓存目录。

    只读写插件数据目录内的文件，超出范围一律拒绝并记日志。
    """
    if not result:
        return False

    if not _inside_plugin_data(path):
        _log(f"拒绝写入插件数据目录之外的路径：{path}")
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if result.startswith(("http://", "https://")):
            if aiohttp is None:
                return False
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    result, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        return False
                    raw = await resp.read()
            if not raw:
                return False
            path.write_bytes(raw)
            return True

        src = _local_path(result)
        if not _inside_plugin_data(src):
            _log(f"拒绝读取插件数据目录之外的文件：{src}")
            return False
        if not src.is_file():
            return False
        if src.resolve() != path.resolve():
            shutil.copyfile(src, path)
        return True
    except Exception as exc:
        _log(f"缓存角色卡图片失败：{type(exc).__name__}: {exc}")
        return False


def _cache_usable(path: Path, fresh_after: float, max_age: float) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    # 模板改过（升级插件）后，旧图自动失效，避免一直发没有新样式的缓存卡
    try:
        if TEMPLATE_FILE.stat().st_mtime > mtime:
            return False
    except OSError:
        pass
    now = time.time()
    if fresh_after and mtime < fresh_after - 2:
        return False
    return now - mtime < max_age


# --------------------------------------------------------------------------
# 远端渲染端点（自带轮询，短超时快速切换）
# --------------------------------------------------------------------------


def _clean_endpoint(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.endswith("text2img"):
        url += "/text2img"
    return url


async def _list_endpoints() -> List[str]:
    """取 AstrBot 官方渲染端点列表，失败时退回默认端点。"""
    global _ENDPOINT_CACHE

    now = time.time()
    cached_at, cached = _ENDPOINT_CACHE
    if cached and now - cached_at < _ENDPOINT_TTL:
        return list(cached)

    urls: List[str] = []
    if aiohttp is not None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    T2I_LIST_URL, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        for item in payload.get("data") or []:
                            if not isinstance(item, dict):
                                continue
                            if item.get("active") and item.get("url"):
                                urls.append(_clean_endpoint(item["url"]))
        except Exception as exc:
            _log(f"获取渲染端点列表失败：{type(exc).__name__}: {exc}")

    seen = set()
    result = []
    for url in urls + T2I_DEFAULT_ENDPOINTS:
        if url and url not in seen:
            seen.add(url)
            result.append(url)

    _ENDPOINT_CACHE = (now, result)
    return list(result)


async def _render_remote(
    html: str,
    timeout: float,
    endpoint: str = "",
) -> Optional[str]:
    """把完整 HTML 直接发给渲染端点，返回图片 URL。

    自己控制每个端点的超时，避免 AstrBot 内置策略在卡住的端点等 300 秒。
    """
    if aiohttp is None:
        return None

    if endpoint:
        # 自定义端点优先；它挂了再试官方端点，避免配置过期后完全出不了图
        endpoints = [_clean_endpoint(endpoint)]
        try:
            endpoints += await _list_endpoints()
        except Exception:
            pass
    else:
        endpoints = await _list_endpoints()
        random.shuffle(endpoints)

    unique: List[str] = []
    seen = set()
    for ep in endpoints:
        if ep and ep not in seen:
            seen.add(ep)
            unique.append(ep)

    now = time.time()
    alive = [
        ep
        for ep in unique
        if now - _ENDPOINT_FAILED_AT.get(ep, 0.0) >= _ENDPOINT_COOLDOWN
    ]
    if unique and not alive:
        _log(
            "所有渲染端点都在冷却中，直接回退（"
            + "、".join(
                f"{ep} {now - _ENDPOINT_FAILED_AT.get(ep, 0.0):.0f}s 前失败"
                for ep in unique
            )
            + "）"
        )
        return None
    endpoints = alive
    if not endpoints:
        return None

    deadline = time.time() + timeout
    payload = {
        "tmpl": html,
        "json": True,
        "tmpldata": {},
        "options": {"full_page": True},
    }

    async with aiohttp.ClientSession() as session:
        for ep in endpoints:
            remain = deadline - time.time()
            if remain < 3:
                break
            attempt_timeout = min(15.0, remain)
            started = time.time()
            try:
                async with session.post(
                    f"{ep}/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=attempt_timeout),
                ) as resp:
                    if resp.status != 200:
                        # 5xx 是端点自身的问题（过载/下线），记下来一段时间内不再浪费预算
                        if resp.status >= 500:
                            _ENDPOINT_FAILED_AT[ep] = time.time()
                        _log(
                            f"渲染端点 {ep} 返回 HTTP {resp.status}"
                            f"（{time.time() - started:.1f}s）"
                        )
                        continue
                    data = await resp.json(content_type=None)
                image_id = (data.get("data") or {}).get("id")
                if image_id:
                    _ENDPOINT_FAILED_AT.pop(ep, None)
                    _log(f"渲染端点 {ep} 成功（{time.time() - started:.1f}s）")
                    return f"{ep}/{image_id}"
                _log(f"渲染端点 {ep} 返回里没有图片 id")
            except Exception as exc:
                _log(
                    f"渲染端点 {ep} 失败：{type(exc).__name__}: {exc}"
                    f"（{time.time() - started:.1f}s）"
                )

    return None


# 同一张角色卡正在渲染时复用同一个任务。
# 典型场景：后台预渲染还没跑完，用户已经在聊天里查同一个角色。
_RENDER_TASKS: Dict[str, "asyncio.Task[Optional[str]]"] = {}


async def _render_and_cache(
    star: Any,
    panel: Dict[str, Any],
    uid: str,
    nickname: str,
    timeout: float,
    save_html: bool,
    use_cache: bool,
    cache_path: Path,
    inline_images: bool = True,
    retry_on_timeout: bool = True,
    use_rotation: bool = True,
    preferred_endpoint: str = "",
) -> Optional[str]:
    """实际渲染路径。同一张卡只会有一个任务在跑。"""
    global LAST_ERROR, LAST_SUCCESS_AT

    def stale_cache() -> Optional[str]:
        """渲染失败时，宁可发一张旧图也别退化成纯文字。"""
        if use_cache and cache_path.exists():
            _log(f"渲染失败，退回上一次的缓存图（{panel.get('name')}）")
            return str(cache_path)
        return None

    # 先走自带端点轮询：每个端点短超时，失败立刻换下一个
    if use_rotation and aiohttp is not None:
        rotation_started = time.time()
        html = ""
        try:
            html = await build_card_html(
                panel, uid, nickname, inline_images=inline_images
            )
        except Exception as exc:
            _log(f"端点轮询准备失败：{type(exc).__name__}: {exc}")

        if html:
            html_images = html.count("data:image/")
            logger.info(
                f"[星铁面板] 端点轮询渲染：{panel.get('name')}，"
                f"HTML {len(html)} 字符，内联 {html_images} 张，"
                f"端点 {'自定义' if preferred_endpoint else '官方列表'}，"
                f"总预算 {timeout:.0f}s"
            )
            url = await _render_remote(html, timeout, preferred_endpoint)
            if url:
                if await _store_image(url, cache_path):
                    LAST_ERROR = ""
                    LAST_SUCCESS_AT = time.time()
                    _log(
                        f"端点轮询渲染成功（{panel.get('name')}）："
                        f"耗时 {LAST_SUCCESS_AT - rotation_started:.1f}s，{url}"
                    )
                    if save_html:
                        await save_card_html(panel, uid, nickname)
                    return str(cache_path)
                return url

            # 端点全挂时不再回退 html_render：用的是同一批端点，只会多等一轮
            LAST_ERROR = "所有渲染端点均不可用（503/超时）"
            _log("端点轮询全部失败，不再回退 html_render")
            logger.warning(
                "[星铁面板] 所有渲染端点均不可用（503/超时），本次改用文字/旧图"
            )
            return stale_cache()

    render = getattr(star, "html_render", None)
    if not callable(render):
        LAST_ERROR = "当前 AstrBot 没有 html_render，无法渲染图片"
        return stale_cache()

    try:
        template = _load_template()
        data = build_card_data(panel, uid, nickname)
    except Exception as exc:
        LAST_ERROR = f"模板/数据准备失败：{type(exc).__name__}: {exc}"
        _log(f"角色卡准备失败（{panel.get('name')}）：{LAST_ERROR}")
        logger.exception("[星铁面板] 角色卡模板/数据准备失败")
        return stale_cache()

    # 图片内嵌可以避免渲染端拉不到图，但请求体会大很多；
    # 远端渲染慢的时候可以在设置里关掉。
    inlined = 0
    if inline_images:
        inlined = await inline_card_images(data)
        if inlined == 0:
            logger.warning("[星铁面板] 角色卡图片一张都没内联成功，渲染结果可能缺图")

    logger.info(
        f"[星铁面板] 开始渲染角色卡：{panel.get('name')}，"
        f"模板 {len(template)} 字符，内联 {inlined} 张，超时 {timeout:.0f}s"
    )

    started = time.time()
    attempt_timeout = timeout
    for attempt in (1, 2):
        try:
            result = await asyncio.wait_for(
                render(template, data, return_url=True, options={"full_page": True}),
                timeout=attempt_timeout,
            )
        except asyncio.TimeoutError:
            # AstrBot 会在多个远端渲染端点之间随机选一个。超时可能只是抽到了
            # 一个卡住的端点，重试一次有机会换到可用的。
            if attempt == 1 and retry_on_timeout:
                attempt_timeout = min(timeout, 20.0)
                _log(
                    f"角色卡渲染超时（{timeout:.0f}s，内联 {inlined} 张，"
                    f"模板 {len(template)} 字符），用 {attempt_timeout:.0f}s 重试一次"
                )
                logger.warning(
                    f"[星铁面板] 角色卡渲染超时（{timeout:.0f}s，内联 {inlined} 张），"
                    f"去掉内嵌图、换 {attempt_timeout:.0f}s 重试一次"
                )
                if inlined:
                    # 重试时不再内嵌图片：请求体小很多，让渲染端自己去 CDN 取图
                    try:
                        data = build_card_data(panel, uid, nickname)
                    except Exception:
                        pass
                    inlined = 0
                continue

            LAST_ERROR = f"渲染超时（{timeout:.0f} 秒）"
            _log(
                f"角色卡渲染两次都超时（内联 {inlined} 张，"
                f"模板 {len(template)} 字符，{panel.get('name')}）"
            )
            logger.warning(
                f"[星铁面板] 角色卡渲染连续超时（内联 {inlined} 张）"
                + ("，可在设置里关闭「角色卡图片内嵌」再试" if inlined else "")
            )
            return stale_cache()
        except Exception as exc:
            LAST_ERROR = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                logger.warning(f"[星铁面板] 角色卡渲染失败，1 秒后重试：{LAST_ERROR}")
                await asyncio.sleep(1)
                continue
            _log(f"角色卡渲染失败（{panel.get('name')}）：{LAST_ERROR}")
            logger.warning(f"[星铁面板] 角色卡渲染失败：{LAST_ERROR}")
            return stale_cache()

        LAST_ERROR = ""
        LAST_SUCCESS_AT = time.time()
        logger.debug(
            f"[星铁面板] 角色卡渲染成功：耗时 {LAST_SUCCESS_AT - started:.1f}s，"
            f"内联图片 {inlined} 张"
        )

        if await _store_image(result, cache_path):
            _log(
                f"角色卡已缓存（{uid} / {panel.get('name')}）："
                f"{cache_path.name}，内联 {inlined} 张"
            )
            if save_html:
                await save_card_html(panel, uid, nickname)
            return str(cache_path)

        return result

    return None


async def render_character_card(
    star: Any,
    panel: Dict[str, Any],
    uid: str,
    nickname: str = "",
    timeout: float = 30.0,
    fresh_after: float = 0.0,
    save_html: bool = False,
    use_cache: bool = True,
    inline_images: bool = True,
    retry_on_timeout: bool = True,
    use_rotation: bool = True,
    preferred_endpoint: str = "",
) -> Optional[str]:
    """渲染角色卡，返回图片 URL 或本地路径；失败返回 None。

    timeout 必须有：渲染服务不可达时不能拖住整轮对话。
    fresh_after：面板数据的更新时间。缓存图比数据旧就重新渲染，
    避免玩家换了遗器之后还发旧卡。
    同一张卡并发请求会共用一次渲染，避免预渲染和聊天互相打架。
    """
    cache_path = card_image_path(uid, panel)
    if use_cache and _cache_usable(cache_path, fresh_after, 7 * 86400):
        logger.debug(f"[星铁面板] 角色卡命中本地缓存：{cache_path}")
        return str(cache_path)

    key = str(cache_path)
    task = _RENDER_TASKS.get(key)

    if task is not None and not task.done():
        try:
            # 渲染中的卡：等它出结果，出完通常就是本地文件
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout + 5)
        except asyncio.TimeoutError:
            logger.warning("[星铁面板] 等待同一张角色卡渲染超时")
            return str(cache_path) if use_cache and cache_path.exists() else None
        except Exception:
            return str(cache_path) if use_cache and cache_path.exists() else None

    task = asyncio.create_task(
        _render_and_cache(
            star,
            panel,
            uid,
            nickname,
            timeout,
            save_html,
            use_cache,
            cache_path,
            inline_images,
            retry_on_timeout,
            use_rotation,
            preferred_endpoint,
        )
    )
    _RENDER_TASKS[key] = task

    def _cleanup(finished: Any) -> None:
        if _RENDER_TASKS.get(key) is finished:
            _RENDER_TASKS.pop(key, None)

    task.add_done_callback(_cleanup)
    try:
        return await asyncio.shield(task)
    except Exception as exc:
        global LAST_ERROR
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        _log(f"角色卡渲染任务异常（{panel.get('name')}）：{LAST_ERROR}")
        logger.exception("[星铁面板] 角色卡渲染任务异常")
        return str(cache_path) if use_cache and cache_path.exists() else None


async def probe_render(
    star: Any,
    timeout: float = 30.0,
    use_rotation: bool = True,
    preferred_endpoint: str = "",
) -> Dict[str, Any]:
    """用一张极简 HTML 测试渲染链路，返回耗时与结果。

    先测插件自带的端点轮询，再测 AstrBot 的 html_render，
    这样能区分「端点本身不可用」和「卡片太大」。
    """
    template = (
        "<html><body style='font-family:sans-serif;padding:20px'>"
        "AstrBot T2I OK</body></html>"
    )

    if use_rotation and aiohttp is not None:
        started = time.time()
        # 自检要真实探测链路，不受「失败冷却」影响
        _ENDPOINT_FAILED_AT.clear()
        url = await _render_remote(template, timeout, preferred_endpoint)
        elapsed = round(time.time() - started, 2)
        if url:
            endpoint = url.rsplit("/", 1)[0]
            return {"ok": True, "elapsed": elapsed, "mode": "rotation", "endpoint": endpoint}

    render = getattr(star, "html_render", None)
    if not callable(render):
        return {
            "ok": False,
            "elapsed": 0.0,
            "mode": "rotation" if use_rotation else "astrbot",
            "error": "内置端点不可用，且当前环境没有 html_render",
        }

    started = time.time()
    try:
        await asyncio.wait_for(
            render(template, {}, return_url=True, options={"full_page": True}),
            timeout=timeout,
        )
        return {
            "ok": True,
            "elapsed": round(time.time() - started, 2),
            "mode": "astrbot",
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "elapsed": round(time.time() - started, 2),
            "mode": "astrbot",
            "error": f"渲染端点超时（{timeout:.0f}s）",
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed": round(time.time() - started, 2),
            "mode": "astrbot",
            "error": f"{type(exc).__name__}: {exc}",
        }
