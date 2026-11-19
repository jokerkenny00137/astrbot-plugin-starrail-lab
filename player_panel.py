"""按 UID 查询玩家角色面板。

功能：
    1. 从米游社/HoYoLAB 的角色展示柜拉取玩家数据（带本地缓存和自动重试）
    2. 把角色基础属性 + 光锥属性 + 遗器词条 + 技能树加成，合成真实的游戏面板
    3. 顺带列出每件遗器的主词条和副词条

这个文件不需要 AstrBot，可以直接在电脑上跑（配合 test.py）。

注意：面板【没有】计入光锥的技能效果（比如"装备者暴击率提高 8%"），
      因为那部分属于战斗中的加成，游戏面板本身也不一定显示。
"""

from __future__ import annotations

import asyncio
import json
import time

from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent

# 玩家数据缓存目录。同一个 UID 在有效期内重复查询不会再去请求网络。
# 作为 AstrBot 插件运行时，main.py 会把它改到 AstrBot 的 data/plugin_data 目录。
CACHE_DIR = HERE / "cache"

# 缓存有效期（秒）。10 分钟内重复查同一个人，直接用本地文件。
CACHE_TTL_SECONDS = 600

# 网络请求失败时的重试次数
MAX_RETRY = 3


def set_cache_dir(path: str | Path) -> Path:
    """修改缓存目录（AstrBot 环境要求数据放在自己的 data 目录里）。"""
    global CACHE_DIR
    CACHE_DIR = Path(path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR

try:
    from msgspec import convert
    from starrail_damage_cal import exception as sr_exc
    from starrail_damage_cal.mihomo.models import MihomoData
    from starrail_damage_cal.mono.Character import Character
    from starrail_damage_cal.to_data import api_to_dict

    LIB_AVAILABLE = True
except ImportError:
    LIB_AVAILABLE = False


# 属性伤害的中文名。库里的角色属性是英文，这里做一次翻译。
ELEMENT_CN = {
    "Physical": "物理",
    "Fire": "火",
    "Ice": "冰",
    "Thunder": "雷",
    "Wind": "风",
    "Quantum": "量子",
    "Imaginary": "虚数",
}


class PlayerQueryError(Exception):
    """查询失败。message 是给用户看的，可以直接打印或发给机器人。"""


# --------------------------------------------------------------------------
# 数据获取
# --------------------------------------------------------------------------


def _cache_file(uid: str) -> Path:
    return CACHE_DIR / uid / "rawData.json"


async def _fetch_from_network(uid: str) -> Dict[str, Any]:
    """联网拉取玩家数据，失败自动重试。"""
    last_error: Any = None

    for attempt in range(1, MAX_RETRY + 1):
        try:
            # api_to_dict 会把原始数据写到 CACHE_DIR/{uid}/rawData.json
            await api_to_dict(uid=uid, save_path=CACHE_DIR)
            raw_file = _cache_file(uid)
            if not raw_file.exists():
                raise PlayerQueryError("数据抓取成功，但没找到缓存文件")
            return json.loads(raw_file.read_text(encoding="utf-8"))

        except sr_exc.InvalidUidError as exc:
            raise PlayerQueryError(f"UID {uid} 无效，请检查是不是输错了") from exc

        except sr_exc.CharacterShowcaseNotOpenError as exc:
            raise PlayerQueryError(
                f"UID {uid} 的玩家没有公开角色展示柜，查不到角色数据"
            ) from exc

        except sr_exc.MihomoModelError as exc:
            # 接口返回了空数据或格式不对，绝大多数情况是 UID 不存在
            raise PlayerQueryError(
                f"查不到 UID {uid} 的数据。\n"
                "可能 UID 不存在，或者对方没有公开角色展示柜。"
            ) from exc

        except sr_exc.MihomoQueueTimeoutError as exc:
            # 公共接口排队超时，等一下再试
            last_error = exc
            if attempt < MAX_RETRY:
                await asyncio.sleep(3)

        except Exception as exc:  # 网络波动、接口改版等
            last_error = exc
            if attempt < MAX_RETRY:
                await asyncio.sleep(2)

    raise PlayerQueryError(f"查询失败（已重试 {MAX_RETRY} 次）：{last_error}")


async def fetch_player_raw(uid: str, force_refresh: bool = False) -> Dict[str, Any]:
    """拿玩家原始数据。优先用缓存，缓存过期或强制刷新时才联网。"""
    if not LIB_AVAILABLE:
        raise PlayerQueryError(
            "没有安装 starrail_damage_cal，请先运行：python -m pip install -r requirements.txt"
        )

    uid = str(uid).strip()
    if not uid.isdigit():
        raise PlayerQueryError("UID 必须是一串数字，比如 100000000")

    cache_file = _cache_file(uid)
    if not force_refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            return json.loads(cache_file.read_text(encoding="utf-8"))

    return await _fetch_from_network(uid)


# --------------------------------------------------------------------------
# 面板计算
# --------------------------------------------------------------------------


def _calc_stat(base_value: float, delta: float, ratio: float) -> float:
    """面板公式：基础值 × (1 + 百分比加成) + 固定值加成。"""
    return base_value * (1 + ratio) + delta


# 遗器部位（库里的 Type 是 int，1~6）
RELIC_SLOT_CN = {
    1: "头冠",
    2: "手部",
    3: "躯干",
    4: "脚部",
    5: "位面球",
    6: "连结绳",
}


def _relic_rarity(relic_id: Any) -> int:
    """遗器 ID 首位数字减一就是稀有度：6xxxx -> 5 星，5xxxx -> 4 星。"""
    try:
        return max(3, min(5, int(str(relic_id)[0]) - 1))
    except (TypeError, ValueError, IndexError):
        return 5


def _star(value: Any, default: int = 5) -> int:
    """星级归一化。库里角色星级是字符串 '5'，光锥星级是 int 5。"""
    try:
        return int(str(value).strip()[-1])
    except (TypeError, ValueError, IndexError):
        return default


async def build_character_panel(char: Character) -> Dict[str, Any]:
    """把单个角色算成一份面板。

    char 是库里 Character 的对象，内部会累积光锥、遗器、技能树的所有加成。
    注意：这个对象被计算过一次后状态就变了，同一个对象不要重复传进来。
    """
    # 这三步会把光锥属性、技能树加成、遗器词条都累加进 char.add_attr
    if char.equipment.equipmentID:
        await char.get_equipment_info()
    await char.get_char_attribute_bonus()
    await char.get_relic_info()

    base = char.base_attributes  # 角色本体 + 光锥的基础属性（白值）
    add = char.add_attr  # 各种词条加成

    element = char.char_element
    element_dmg = add.get(f"{element}AddedRatio", 0.0) + add.get(
        "AllDamageAddedRatio", 0.0
    )

    relics: List[Dict[str, Any]] = []
    for relic in char.char_relic or []:
        relic_id = getattr(relic, "relicId", 0) or 0
        relics.append(
            {
                "id": relic_id,
                "set_id": getattr(relic, "SetId", 0) or 0,
                "rarity": _relic_rarity(relic_id),
                "slot": RELIC_SLOT_CN.get(getattr(relic, "Type", 0), ""),
                "name": relic.relicName,
                "set_name": relic.SetName,
                "level": relic.Level,
                "main_name": relic.MainAffix.Name,
                "main_value": relic.MainAffix.Value,
                "main_is_percent": not relic.MainAffix.Property.endswith("Delta"),
                "subs": [
                    {
                        "name": sub.Name,
                        # 英文属性名，遗器评分要拿它去匹配权重表
                        "property": sub.Property,
                        "value": sub.Value,
                        "is_percent": not sub.Property.endswith("Delta"),
                    }
                    for sub in relic.SubAffixList
                ],
            }
        )

    return {
        "name": char.char_name,
        "avatar_id": char.char_id,
        "rarity": _star(getattr(char, "char_rarity", 0)),
        "level": char.char_level,
        "rank": char.char_rank,
        "element": ELEMENT_CN.get(element, element),
        "element_en": element,
        "weapon_name": char.equipment.equipmentName or "未装备",
        "weapon_id": char.equipment.equipmentID,
        "weapon_rarity": _star(getattr(char.equipment, "equipmentRarity", 0)),
        "weapon_level": char.equipment.equipmentLevel,
        "weapon_rank": char.equipment.equipmentRank,
        "hp": _calc_stat(
            base.hp, add.get("HPDelta", 0.0), add.get("HPAddedRatio", 0.0)
        ),
        "attack": _calc_stat(
            base.attack, add.get("AttackDelta", 0.0), add.get("AttackAddedRatio", 0.0)
        ),
        "defence": _calc_stat(
            base.defence,
            add.get("DefenceDelta", 0.0),
            add.get("DefenceAddedRatio", 0.0),
        ),
        "speed": _calc_stat(
            base.speed, add.get("SpeedDelta", 0.0), add.get("SpeedAddedRatio", 0.0)
        ),
        # 暴击率和暴击伤害是加在基础值上的（基础 5% / 50%）
        "crit_rate": base.CriticalChanceBase + add.get("CriticalChanceBase", 0.0),
        "crit_dmg": base.CriticalDamageBase + add.get("CriticalDamageBase", 0.0),
        "element_dmg": element_dmg,
        "heal_bonus": add.get("HealRatioBase", 0.0),
        "break_effect": add.get("BreakDamageAddedRatioBase", 0.0),
        "effect_hit": add.get("StatusProbabilityBase", 0.0),
        "effect_res": add.get("StatusResistanceBase", 0.0),
        "energy_regen": add.get("SPRatioBase", 0.0),
        "relics": relics,
    }


async def query_player(uid: str, force_refresh: bool = False) -> Dict[str, Any]:
    """查询一个玩家的所有展示柜角色面板。

    返回：
        {
          "ok": True,
          "uid": "...",
          "nickname": "...",
          "characters": [面板, ...],
          "errors": [{"id": ..., "error": ...}]
        }
    """
    raw = await fetch_player_raw(uid, force_refresh=force_refresh)

    detail = raw.get("detailInfo") or {}
    nickname = detail.get("nickname") or "未知玩家"

    mihomo_data = convert(raw, type=MihomoData)
    char_id_list, char_data = await api_to_dict(mihomo_raw=mihomo_data)

    characters: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for cid in char_id_list:
        try:
            # api_to_dict 返回的是原始模型，要先包成 Character 才能算属性
            characters.append(await build_character_panel(Character(char_data[cid])))
        except Exception as exc:
            # 某个角色算不出来不影响其他角色
            errors.append({"id": cid, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "ok": bool(characters),
        "uid": str(uid),
        "nickname": nickname,
        "characters": characters,
        "errors": errors,
    }


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------


def _num(value: float) -> str:
    """12345.6 -> 12,346"""
    return f"{value:,.0f}"


def _pct(value: float) -> str:
    """0.685 -> 68.5%"""
    return f"{value * 100:.1f}%"


def _fmt_affix(value: float, is_percent: bool) -> str:
    """遗器词条：百分比显示成 5.5%，固定值保留一位小数（和游戏里一致）。"""
    return _pct(value) if is_percent else f"{value:.1f}"


def format_character_panel(panel: Dict[str, Any]) -> str:
    """单个角色的面板文本。"""
    lines = [
        f"● {panel['name']}  Lv{panel['level']}  命座{panel['rank']}  {panel['element']}属性",
        f"  光锥  {panel['weapon_name']}  Lv{panel['weapon_level']}  叠影{panel['weapon_rank']}",
        f"  生命 {_num(panel['hp'])}   攻击 {_num(panel['attack'])}   "
        f"防御 {_num(panel['defence'])}   速度 {panel['speed']:.1f}",
        f"  暴击率 {_pct(panel['crit_rate'])}   暴击伤害 {_pct(panel['crit_dmg'])}   "
        f"{panel['element']}属性伤害 {_pct(panel['element_dmg'])}",
    ]

    # 只在有加成的时候才显示，避免刷屏
    extras = []
    if panel["heal_bonus"]:
        extras.append(f"治疗量 {_pct(panel['heal_bonus'])}")
    if panel["break_effect"]:
        extras.append(f"击破特攻 {_pct(panel['break_effect'])}")
    if panel["effect_hit"]:
        extras.append(f"效果命中 {_pct(panel['effect_hit'])}")
    if panel["energy_regen"]:
        extras.append(f"能量恢复 {_pct(panel['energy_regen'])}")
    if extras:
        lines.append("  " + "   ".join(extras))

    if panel["relics"]:
        lines.append("  遗器：")
        for relic in panel["relics"]:
            main = _fmt_affix(relic["main_value"], relic["main_is_percent"])
            lines.append(f"    +{relic['level']} {relic['name']}")
            lines.append(f"       主词条 {relic['main_name']} {main}")
            if relic["subs"]:
                sub_text = "  ".join(
                    f"{s['name']} {_fmt_affix(s['value'], s['is_percent'])}"
                    for s in relic["subs"]
                )
                lines.append(f"       副词条 {sub_text}")
    else:
        lines.append("  遗器：未装备")

    return "\n".join(lines)


def format_player_report(result: Dict[str, Any]) -> str:
    """整个玩家的报告文本。"""
    chars = result.get("characters", [])
    lines = [
        f"【{result.get('nickname', '未知玩家')}】UID {result.get('uid', '')}",
        f"展示柜角色 {len(chars)} 个",
        "-" * 40,
        "",
    ]

    for panel in chars:
        lines.append(format_character_panel(panel))
        lines.append("")

    if result.get("errors"):
        lines.append("-" * 40)
        lines.append("以下角色解析失败：")
        for item in result["errors"]:
            lines.append(f"  id={item['id']}  {item['error']}")
        lines.append("")

    lines.append("说明：面板已含光锥基础属性和遗器词条，未计入光锥技能效果。")
    return "\n".join(lines)


def query_player_sync(uid: str, force_refresh: bool = False) -> Dict[str, Any]:
    """同步版，方便普通脚本直接调用。"""
    return asyncio.run(query_player(uid, force_refresh=force_refresh))


# --------------------------------------------------------------------------
# 角色列表 / 按名字查找
# --------------------------------------------------------------------------


def format_character_list(result: Dict[str, Any]) -> str:
    """只列出角色清单，让用户自己挑一个看详情。"""
    chars = result.get("characters", [])
    uid = result.get("uid", "")
    lines = [
        f"【{result.get('nickname', '未知玩家')}】UID {uid}",
        f"展示柜共 {len(chars)} 个角色：",
        "",
    ]

    for i, c in enumerate(chars, 1):
        lines.append(
            f"  {i}. {c['name']}  Lv{c['level']}  {c['element']}  {c['weapon_name']}"
        )

    sample = chars[0]["name"] if chars else "希儿"
    lines.append("")
    lines.append("想看哪个角色的详细面板，发：")
    lines.append(f"  /选择查询 角色名      （不用打 UID，如 /选择查询 {sample}）")
    lines.append("  /选择查询 序号        （如 /选择查询 1）")
    lines.append("  /选择查询 全部        （一次看完整份）")
    lines.append(f"  未绑定也可以用：/面板 {uid} 角色名")
    return "\n".join(lines)


def find_character(result: Dict[str, Any], keyword: str) -> Any:
    """按角色名或序号找角色。找不到返回 None。"""
    chars = result.get("characters", [])
    kw = str(keyword or "").strip()
    if not kw:
        return None

    if kw.isdigit():
        idx = int(kw) - 1
        return chars[idx] if 0 <= idx < len(chars) else None

    for c in chars:  # 先精确匹配
        if c["name"] == kw:
            return c
    for c in chars:  # 再模糊匹配，比如「知更鸟」能命中「知更鸟•晴歌」
        if kw in c["name"] or c["name"] in kw:
            return c
    return None


def build_llm_summary(result: Dict[str, Any], max_chars: int = 3000) -> str:
    """生成一份给大模型看的紧凑摘要（不含每条副词条，控制长度）。"""
    chars = result.get("characters", [])
    lines = [
        f"玩家：{result.get('nickname', '未知玩家')}（UID {result.get('uid', '')}），"
        f"展示柜角色 {len(chars)} 个。"
    ]

    for c in chars:
        parts = [
            f"{c['name']} Lv{c['level']} 命座{c['rank']} {c['element']}属性",
            f"光锥「{c['weapon_name']}」叠影{c['weapon_rank']}",
            (
                f"攻击{c['attack']:.0f} 生命{c['hp']:.0f} "
                f"防御{c['defence']:.0f} 速度{c['speed']:.0f}"
            ),
            f"暴击率{c['crit_rate'] * 100:.1f}% 暴击伤害{c['crit_dmg'] * 100:.1f}%",
        ]
        if c["element_dmg"]:
            parts.append(f"{c['element']}伤{c['element_dmg'] * 100:.1f}%")
        if c["break_effect"]:
            parts.append(f"击破特攻{c['break_effect'] * 100:.1f}%")

        set_count: Dict[str, int] = {}
        for relic in c["relics"]:
            name = relic["set_name"]
            set_count[name] = set_count.get(name, 0) + 1
        if set_count:
            parts.append(
                "遗器：" + "、".join(f"{k}×{v}" for k, v in set_count.items())
            )
            mains = [r["main_name"] for r in c["relics"]]
            parts.append("主词条：" + "、".join(mains))

        lines.append("- " + "｜".join(parts))

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n（数据较长，已截断）"
    return text
