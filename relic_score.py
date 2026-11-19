"""遗器评分：按角色权重给每件遗器的副词条打分。

数据来源是 starrail_damage_cal 自带的两张表：
    StarRailRelicScores  按 avatarId 的副词条权重 + 单件评分上限
    RelicSubAffixConfig  副词条单次强化的基础值

公式：
    某条副词条得分 = 该词条数值 / 单次强化基础值 × 该词条权重
    单件遗器得分   = 所有副词条得分之和

「数值 / 基础值」等价于「折合多少次平均 roll」，所以权重 1.0 的词条
每 roll 贡献约 1 分，单件满级遗器（9 roll）的理论上限在 9~10 之间，
和表里的 max 字段（如绯英 9.66）是同一把尺子。

库版本较老、没有这两张表、或者角色没被收录时，一律返回 None，
调用方保持原来的展示，不显示评分。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from starrail_damage_cal.excel.model import (
        RelicSubAffixConfig,
        StarRailRelicScores,
    )

    SCORE_AVAILABLE = True
except Exception:  # 库版本差异、数据文件缺失等
    SCORE_AVAILABLE = False

# 面板属性中文名 -> 权重表里的属性名
STAT_KEY_MAP = {
    "生命值": "HPAddedRatio",
    "攻击力": "AttackAddedRatio",
    "防御力": "DefenceAddedRatio",
    "速度": "SpeedDelta",
    "暴击率": "CriticalChanceBase",
    "暴击伤害": "CriticalDamageBase",
    "击破特攻": "BreakDamageAddedRatioBase",
    "效果命中": "StatusProbabilityBase",
    "效果抵抗": "StatusResistanceBase",
}

# 权重达到这个值就算「关键属性」，卡片上会金光标注
KEY_WEIGHT_THRESHOLD = 0.5


def _build_base_values() -> Dict[str, float]:
    """5 星遗器（GroupID=5）每条副词条的单次强化基础值。"""
    if not SCORE_AVAILABLE:
        return {}
    bases: Dict[str, float] = {}
    for row in RelicSubAffixConfig:
        if row.GroupID != 5:
            continue
        bases.setdefault(row.Property, float(row.BaseValue.Value))
    return bases


SUB_AFFIX_BASE: Dict[str, float] = _build_base_values()


def get_relic_profile(avatar_id: Any) -> Optional[Dict[str, Any]]:
    """取某个角色的权重配置。没收录就返回 None。"""
    if not SCORE_AVAILABLE or not avatar_id:
        return None
    profile = StarRailRelicScores.get(str(avatar_id))
    if profile is None:
        return None

    weight = {}
    for field_name in profile.weight.__struct_fields__:
        weight[field_name] = float(getattr(profile.weight, field_name) or 0.0)

    return {"weight": weight, "max": float(profile.max_value or 0.0)}


def score_relic(subs: List[Dict[str, Any]], weight: Dict[str, float]) -> float:
    """单件遗器的加权评分。subs 需要带英文 property 字段。"""
    total = 0.0
    for sub in subs:
        prop = sub.get("property") or ""
        base = SUB_AFFIX_BASE.get(prop)
        w = weight.get(prop, 0.0)
        if not base or w <= 0:
            continue
        try:
            total += (float(sub.get("value", 0.0)) / base) * w
        except (TypeError, ValueError):
            continue
    return total


def key_stat_names(weight: Dict[str, float], threshold: float = KEY_WEIGHT_THRESHOLD):
    """权重够高的面板属性中文名集合，用于卡片金光标注。"""
    return {
        cn
        for cn, prop in STAT_KEY_MAP.items()
        if weight.get(prop, 0.0) >= threshold
    }
