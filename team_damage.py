"""星穹铁道配队伤害计算 —— 实验性功能，暂未接入 AstrBot 指令。

这个文件目前只做一件事：
    输入一份"配队 JSON"，输出这支队伍的伤害汇总。

【重要限制】依赖的 starrail_damage_cal 库只实现了 50 个角色的伤害逻辑，
最新只到 2.2 版本的「知更鸟」。之后的新角色调用会抛 UnsupportedAvatarError。
所以这部分目前只作为实验代码保留，插件对外只提供面板查询。

配队 JSON 的格式（team_config）::

    {
      "name": "测试配队",
      "members": [
        {"name": "希儿", "mihomo_raw": { ...米游社角色数据... }},
        {"name": "银狼", "uid": "100000001"}
      ]
    }

每个成员二选一：
  * "mihomo_raw"：本地已有的角色数据（不联网）。test.py 用这种方式。
  * "uid"      ：让计算库去 api.mihomo.me 拉取该玩家展示柜里的角色数据（要联网）。

"name" 可以省略，会自动从 mihomo_raw 里的 avatarId 推断。
"""

from __future__ import annotations

import asyncio

from typing import Any, Dict, List, Optional

try:
    from starrail_damage_cal import DamageCal
    from starrail_damage_cal.map.SR_MAP_PATH import avatarId2Name

    LIB_AVAILABLE = True
except ImportError:  # 没装依赖时不让整个文件崩掉
    DamageCal = None  # type: ignore[assignment]
    avatarId2Name = {}  # type: ignore[assignment]
    LIB_AVAILABLE = False


# starrail_damage_cal 每个技能的 damagelist 固定是 3 个数，含义为：
#   [0] 暴击伤害  —— 100% 暴击时的一次伤害
#   [1] 期望伤害  —— 按你的实际暴击率折算后的平均值（推荐看这个）
#   [2] 参考上限  —— 计算库内部套了一套"毕业面板"后的参考值，不是你的真实伤害
IDX_CRIT = 0
IDX_EXPECT = 1


class TeamCalcError(Exception):
    """配队配置有问题时抛出。"""


def _guess_avatar_name(member: Dict[str, Any]) -> Optional[str]:
    """没写 name 时，从 mihomo_raw 的 avatarId 反查角色名。"""
    raw = member.get("mihomo_raw")
    if not isinstance(raw, dict):
        return None
    avatars = (raw.get("detailInfo") or {}).get("avatarDetailList") or []
    if not avatars:
        return None
    avatar_id = avatars[0].get("avatarId")
    if avatar_id is None:
        return None
    return avatarId2Name.get(str(avatar_id))


def _normalize_damage(damage: Any) -> List[Dict[str, float]]:
    """把计算库返回的原始列表整理成统一结构。"""
    skills: List[Dict[str, float]] = []
    if not isinstance(damage, list):
        return skills

    for item in damage:
        if not isinstance(item, dict):
            continue
        values = item.get("damagelist") or []
        crit = float(values[IDX_CRIT]) if len(values) > IDX_CRIT else 0.0
        expect = float(values[IDX_EXPECT]) if len(values) > IDX_EXPECT else crit
        skills.append(
            {
                "skill": str(item.get("name", "未知技能")),
                "crit": crit,
                "expect": expect,
            }
        )
    return skills


async def calculate_member_damage(
    member: Dict[str, Any],
    index: int = 0,
) -> Dict[str, Any]:
    """计算单个角色的伤害。失败不抛异常，而是把错误写在返回值里。"""
    if not LIB_AVAILABLE:
        return {
            "index": index,
            "name": f"成员{index + 1}",
            "ok": False,
            "error": "未安装 starrail_damage_cal，请先执行 pip install -r requirements.txt",
            "skills": [],
            "crit_total": 0.0,
            "expect_total": 0.0,
        }

    if not isinstance(member, dict):
        return {
            "index": index,
            "name": f"成员{index + 1}",
            "ok": False,
            "error": "成员必须是 JSON 对象",
            "skills": [],
            "crit_total": 0.0,
            "expect_total": 0.0,
        }

    name = member.get("name") or _guess_avatar_name(member)
    result: Dict[str, Any] = {
        "index": index,
        "name": name or f"成员{index + 1}",
        "ok": False,
        "error": "",
        "skills": [],
        "crit_total": 0.0,
        "expect_total": 0.0,
    }

    if not name:
        result["error"] = "缺少 name 字段，且无法从 mihomo_raw 推断角色名"
        return result

    try:
        if isinstance(member.get("mihomo_raw"), dict):
            damage = await DamageCal.get_damage_data_by_mihomo_raw(
                member["mihomo_raw"], name
            )
        elif member.get("uid"):
            damage = await DamageCal.get_damage_data_by_uid(str(member["uid"]), name)
        else:
            result["error"] = "成员需要提供 mihomo_raw 或 uid"
            return result
    except Exception as exc:  # 网络、名字写错、数据缺字段……统一兜住
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    skills = _normalize_damage(damage)
    result["skills"] = skills
    result["crit_total"] = sum(s["crit"] for s in skills)
    result["expect_total"] = sum(s["expect"] for s in skills)
    result["ok"] = True
    return result


async def calculate_team_damage(team_config: Dict[str, Any]) -> Dict[str, Any]:
    """计算整队伤害。

    队伍总伤害 = 每位角色所有技能「期望伤害」之和。
    这是一个「一轮技能全打一遍」的粗略口径，不是严格的循环 DPS。
    """
    if not isinstance(team_config, dict):
        raise TeamCalcError("配队配置必须是一个 JSON 对象")

    members = team_config.get("members")
    if not isinstance(members, list) or not members:
        raise TeamCalcError("配队配置里必须有非空的 members 列表")
    if len(members) > 4:
        raise TeamCalcError("一支队伍最多 4 个角色")

    results = []
    for i, member in enumerate(members):
        results.append(await calculate_member_damage(member, i))

    ok_members = [r for r in results if r["ok"]]
    return {
        "ok": bool(ok_members),
        "team_name": str(team_config.get("name") or "未命名配队"),
        "members": results,
        "team_crit_total": sum(r["crit_total"] for r in ok_members),
        "team_expect_total": sum(r["expect_total"] for r in ok_members),
        "failed": [
            {"name": r["name"], "error": r["error"]} for r in results if not r["ok"]
        ],
    }


def calculate_team_damage_sync(team_config: Dict[str, Any]) -> Dict[str, Any]:
    """同步版，方便在 test.py / 普通脚本里直接调用。"""
    return asyncio.run(calculate_team_damage(team_config))


def _fmt(num: float) -> str:
    """把 1234567.89 显示成 1,234,568。"""
    return f"{num:,.0f}"


def format_team_report(result: Dict[str, Any]) -> str:
    """把计算结果排版成一段可读文本（以后接 QQ 机器人可以直接发出去）。"""
    lines = [f"【{result.get('team_name', '未命名配队')}】伤害报告", ""]

    for member in result.get("members", []):
        if not member["ok"]:
            lines.append(f"[X] {member['name']}：计算失败 —— {member['error']}")
            lines.append("")
            continue

        lines.append(f"[+] {member['name']}")
        for skill in member["skills"]:
            lines.append(
                f"    {skill['skill']:<8} 期望 {_fmt(skill['expect']):>12}"
                f"   暴击 {_fmt(skill['crit']):>12}"
            )
        lines.append(
            f"    小计     期望 {_fmt(member['expect_total']):>12}"
            f"   暴击 {_fmt(member['crit_total']):>12}"
        )
        lines.append("")

    lines.append("-" * 46)
    lines.append(f"队伍期望总伤：{_fmt(result.get('team_expect_total', 0))}")
    lines.append(f"队伍暴击总伤：{_fmt(result.get('team_crit_total', 0))}")
    lines.append("（口径：每位角色各技能打一次后相加，非严格循环 DPS）")
    return "\n".join(lines)
