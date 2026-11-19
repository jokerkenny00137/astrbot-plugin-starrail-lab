"""把单个角色的面板渲染成一张图片角色卡。

渲染依赖 AstrBot 内置的 `Star.html_render()`（HTML + Jinja2 → 图片），
不打包任何游戏素材，头像 / 光锥 / 属性图标全部走公共 CDN。

任何一步失败都返回 None，由调用方回退到原来的文本输出。
"""

from __future__ import annotations

import asyncio

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger

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

    try:
        return await asyncio.wait_for(
            render(template, data, return_url=True, options={"full_page": True}),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[星铁面板] 角色卡渲染超时（{timeout:.0f}s），改用文本输出")
        return None
    except Exception as exc:
        logger.warning(f"[星铁面板] 角色卡渲染失败：{type(exc).__name__}: {exc}")
        return None
