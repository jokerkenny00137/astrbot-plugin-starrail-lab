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
import time

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    import aiohttp
except Exception:  # 极简环境下没有 aiohttp 时跳过内联，退化为远程链接
    aiohttp = None  # type: ignore[assignment]

try:  # AstrBot 以包的形式加载插件
    from .relic_score import get_relic_profile, key_stat_names, score_relic
except ImportError:  # 以普通脚本方式被导入时
    from relic_score import get_relic_profile, key_stat_names, score_relic

CARD_DIR = Path(__file__).resolve().parent / "card"
TEMPLATE_FILE = CARD_DIR / "character.html"

# 单件遗器的满分数。六件全满是 60 分。
RELIC_MAX_SCORE = 10.0

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

        # 原始分按该角色的理论上限归一化到 10 分制
        score = 0.0
        has_score = bool(profile and weight_max)
        if has_score:
            raw = score_relic(subs, weight)
            score = min(RELIC_MAX_SCORE, raw / weight_max * RELIC_MAX_SCORE)
            relic_scores.append(score)

        relics.append(
            {
                "tier": tier,
                "icon": _relic_url(relic.get("set_id")),
                "icon_alt": _asset_alt(f"relic/{relic.get('set_id')}") if relic.get("set_id") else "",
                "slot": relic.get("slot", ""),
                "name": relic.get("name", ""),
                "level": relic.get("level", 0),
                "main_name": relic.get("main_name", ""),
                "main_value": _affix(
                    relic.get("main_value", 0.0), relic.get("main_is_percent", True)
                ),
                "subs": [
                    f"{s['name']} {_affix(s['value'], s['is_percent'])}"
                    for s in subs
                ],
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
        jobs.append((data, "avatar_url", data["avatar_url"], ""))

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


async def render_character_card(
    star: Any,
    panel: Dict[str, Any],
    uid: str,
    nickname: str = "",
    timeout: float = 15.0,
) -> Optional[str]:
    """渲染角色卡，返回图片 URL 或本地路径；失败返回 None。

    timeout 必须有：渲染服务不可达时不能拖住整轮对话。
    """
    render = getattr(star, "html_render", None)
    if not callable(render):
        return None

    try:
        template = _load_template()
        data = build_card_data(panel, uid, nickname)
    except Exception:
        logger.exception("[星铁面板] 角色卡模板/数据准备失败")
        return None

    # 图片先内嵌，避免渲染端拉不到图或截到空图
    inlined = await inline_card_images(data)
    if inlined == 0:
        logger.warning("[星铁面板] 角色卡图片一张都没内联成功，渲染结果可能缺图")

    started = time.time()
    last_error: Optional[str] = None

    for attempt in (1, 2):
        try:
            result = await asyncio.wait_for(
                render(template, data, return_url=True, options={"full_page": True}),
                timeout=timeout,
            )
            logger.debug(
                f"[星铁面板] 角色卡渲染成功：耗时 {time.time() - started:.1f}s，"
                f"内联图片 {inlined} 张"
            )
            return result
        except asyncio.TimeoutError:
            # 超时说明渲染端点已经卡住，重试只会让用户多等一轮
            logger.warning(f"[星铁面板] 角色卡渲染超时（{timeout:.0f}s）")
            return None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                logger.warning(f"[星铁面板] 角色卡渲染失败，1 秒后重试：{last_error}")
                await asyncio.sleep(1)
                continue
            logger.warning(f"[星铁面板] 角色卡渲染失败：{last_error}")

    return None
