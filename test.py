"""本地测试脚本 —— 不需要 AstrBot，直接在电脑上的终端里跑。

用法：

    python test.py 100000000          查这个 UID 的角色面板（推荐）
    python test.py 100000000 -r       忽略缓存，重新联网查一次
    python test.py sample_team.json   老的伤害计算演示（只支持部分老角色）

退出码 0 表示成功，1 表示失败。
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TEAM_FILE = HERE / "sample_team.json"


def _fix_console_encoding() -> None:
    """Windows 控制台是 GBK，遇到生僻符号可能报编码错，降级成替换而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def print_help() -> None:
    print("=" * 46)
    print("星穹铁道插件 —— 本地测试")
    print("=" * 46)
    print()
    print("查玩家面板：")
    print("    python test.py 100000000")
    print()
    print("忽略缓存重新查：")
    print("    python test.py 100000000 -r")
    print()
    print("跑老的伤害计算演示：")
    print("    python test.py sample_team.json")
    print()
    print("第一次使用请先装依赖：")
    print("    python -m pip install -r requirements.txt")


# --------------------------------------------------------------------------
# 模式一：查玩家面板
# --------------------------------------------------------------------------


def run_uid_mode(uid: str, refresh: bool) -> int:
    from player_panel import PlayerQueryError, format_player_report, query_player_sync

    print("=" * 46)
    print(f"查询 UID：{uid}" + ("（忽略缓存）" if refresh else "（优先用缓存）"))
    print("=" * 46)

    try:
        result = query_player_sync(uid, force_refresh=refresh)
    except PlayerQueryError as exc:
        print(f"\n[查询失败] {exc}\n")
        return 1

    print()
    print(format_player_report(result))
    print()

    if not result["ok"]:
        print("[失败] 没有解析出任何角色")
        return 1

    print(f"[成功] 共解析 {len(result['characters'])} 个角色")
    return 0


# --------------------------------------------------------------------------
# 模式二：老的伤害计算演示
# --------------------------------------------------------------------------


def load_team(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到配队文件：{path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"配队文件不是合法的 JSON（第 {exc.lineno} 行第 {exc.colno} 列）"
        ) from exc


def run_team_mode(team_file: Path) -> int:
    from team_damage import (
        LIB_AVAILABLE,
        TeamCalcError,
        calculate_team_damage_sync,
        format_team_report,
    )

    print("=" * 46)
    print("配队伤害计算 —— 本地测试")
    print("=" * 46)

    if not LIB_AVAILABLE:
        print("依赖没装好，请先运行：")
        print("    python -m pip install -r requirements.txt")
        return 1

    team = load_team(team_file)
    members = team.get("members") or []
    print(f"配队文件：{team_file.name}")
    print(f"配队名称：{team.get('name', '未命名')}")
    print(f"成员数量：{len(members)}")
    for m in members:
        source = "mihomo_raw（离线）" if "mihomo_raw" in m else "uid（联网）"
        print(f"  - {m.get('name', '?')}  [{source}]")
    print()

    try:
        result = calculate_team_damage_sync(team)
    except TeamCalcError as exc:
        print(f"[失败] 配队配置有问题：{exc}")
        return 1

    print(format_team_report(result))
    print()

    if result.get("failed"):
        print("[失败] 以下角色没算出来：")
        for item in result["failed"]:
            print(f"  - {item['name']}: {item['error']}")
        return 1

    if result.get("team_expect_total", 0) <= 0:
        print("[失败] 伤害为 0，请检查数据是否合理。")
        return 1

    print("[成功] 伤害计算链路正常。")
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    _fix_console_encoding()

    refresh = ("-r" in sys.argv) or ("--refresh" in sys.argv)
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not targets:
        print_help()
        return 0

    target = targets[0]

    # 纯数字 = UID，其他 = 配队 JSON 文件
    if target.isdigit():
        return run_uid_mode(target, refresh)

    return run_team_mode(Path(target).resolve())


if __name__ == "__main__":
    sys.exit(main())
