"""从 Fribbels HSR Optimizer 同步角色「行迹倾向」。

Fribbels 的 `src/data/game_data.json` 里有每个角色的 `traces`（行迹加成汇总），
例如三月七是「冰伤 22.4% / 防御 22.5% / 效果抵抗 10%」。这能反映角色吃哪些属性。

用途：`starrail_damage_cal` 的权重表没收录的新角色，原本只能让模型按通用标准猜，
用它兜底比猜靠谱。权重表有的角色仍然以权重表为准。

数据是英文属性名，同步时就翻译掉，运行时不再做映射。
"""

from __future__ import annotations

import hashlib
import json
import time

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from astrbot.api import logger

try:  # AstrBot 以包的形式加载插件
    from .guide_store import get_user_dir
except ImportError:  # 以普通脚本方式被导入时
    from guide_store import get_user_dir

try:
    import aiohttp
except Exception:  # 极简环境没有 aiohttp 时同步不可用
    aiohttp = None  # type: ignore[assignment]

# GitHub raw 优先，国内不通时退回 jsDelivr
GAME_DATA_URLS = (
    "https://raw.githubusercontent.com/fribbels/hsr-optimizer/main/src/data/game_data.json",
    "https://cdn.jsdelivr.net/gh/fribbels/hsr-optimizer@main/src/data/game_data.json",
)

HEADERS = {"User-Agent": "astrbot-plugin-starrail-lab"}
HTTP_TIMEOUT = 40.0
FILE_NAME = "traces.json"

# 行迹加成里出现的英文属性名 -> 中文
STAT_NAMES = {
    "HP": "生命值",
    "HP%": "生命值",
    "ATK": "攻击力",
    "ATK%": "攻击力",
    "DEF": "防御力",
    "DEF%": "防御力",
    "SPD": "速度",
    "SPD%": "速度",
    "CRIT Rate": "暴击率",
    "CRIT DMG": "暴击伤害",
    "Effect Hit Rate": "效果命中",
    "Effect RES": "效果抵抗",
    "Break Effect": "击破特攻",
    "Energy Regeneration Rate": "能量恢复效率",
    "All DMG Boost": "全属性伤害提高",
    "Ice DMG Boost": "冰属性伤害提高",
    "Fire DMG Boost": "火属性伤害提高",
    "Wind DMG Boost": "风属性伤害提高",
    "Lightning DMG Boost": "雷属性伤害提高",
    "Physical DMG Boost": "物理属性伤害提高",
    "Quantum DMG Boost": "量子属性伤害提高",
    "Imaginary DMG Boost": "虚数属性伤害提高",
    "Outgoing Healing Boost": "治疗量加成",
    # 3.x 的「欢愉」命途，上游用 Elation 作为加成键
    "Elation": "欢愉伤害提高",
}

# mtime -> {avatar_id: hint}
_CACHE: Tuple[float, Dict[str, str]] = (0.0, {})


def _file_path() -> Path:
    """和机制卡同级的 traces.json。"""
    return get_user_dir().parent / FILE_NAME


def _build_hint(traces: Dict[str, Any], limit: int = 3) -> str:
    """把 traces 转成「冰属性伤害提高/防御力/效果抵抗」这样的简短倾向。"""
    rows = []
    seen = set()
    for key, value in (traces or {}).items():
        name = STAT_NAMES.get(str(key))
        if not name or name in seen:
            continue
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            amount = 0.0
        seen.add(name)
        rows.append((amount, name))
    rows.sort(key=lambda item: -item[0])
    return "/".join(name for _, name in rows[:limit])


def _load_hints() -> Dict[str, str]:
    """按 mtime 缓存读取，文件很小，改完立即生效。"""
    global _CACHE

    path = _file_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _CACHE[0] == mtime:
        return _CACHE[1]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}

    raw = data.get("characters") if isinstance(data, dict) else None
    hints: Dict[str, str] = {}
    if isinstance(raw, dict):
        for avatar_id, item in raw.items():
            if isinstance(item, dict):
                hint = str(item.get("hint") or "").strip()
                if hint:
                    hints[str(avatar_id)] = hint
    _CACHE = (mtime, hints)
    return hints


def get_trace_hint(avatar_id: Any) -> str:
    """取某个角色的行迹倾向，没有资料返回空字符串。"""
    key = str(avatar_id or "").strip()
    if not key:
        return ""
    return _load_hints().get(key, "")


def _stat_label(name: str) -> str:
    """「冰属性伤害提高」-> 「冰属性伤害」，跟面板属性名对齐。"""
    return name[: -len("提高")] if name.endswith("提高") else name


def get_trace_stats(avatar_id: Any) -> Tuple[str, ...]:
    """行迹倾向拆成面板属性名，没有资料返回空元组。

    权重表没收录角色时，卡片用这些名字给高亮属性兜底。
    """
    hint = get_trace_hint(avatar_id)
    if not hint:
        return ()
    return tuple(_stat_label(part) for part in hint.split("/") if part)


def read_synced_at() -> float:
    try:
        data = json.loads(_file_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("_synced_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def count_traces() -> int:
    return len(_load_hints())


def _read_version() -> str:
    try:
        data = json.loads(_file_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("_version") or "")


async def _fetch_text(session: Any, url: str) -> Optional[str]:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                logger.debug(f"[星铁面板] 行迹数据下载失败：{url} -> HTTP {resp.status}")
                return None
            return await resp.text()
    except Exception as exc:
        logger.debug(f"[星铁面板] 行迹数据下载异常：{url} -> {type(exc).__name__}: {exc}")
        return None


async def sync_traces(force: bool = False) -> Dict[str, Any]:
    """拉取 Fribbels 的角色数据，只保留行迹倾向。

    force=False 时，内容哈希没变就直接跳过。
    """
    if aiohttp is None:
        return {"ok": False, "error": "缺少 aiohttp，无法联网同步", "count": 0}

    path = _file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"创建目录失败：{exc}", "count": 0}

    started = time.time()
    try:
        raw = None
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for url in GAME_DATA_URLS:
                raw = await _fetch_text(session, url)
                if raw:
                    break
        if not raw:
            return {"ok": False, "error": "所有下载地址都不可用", "count": 0}

        version = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cached_version = _read_version()
        existing = count_traces()
        if not force and cached_version == version and existing:
            return {
                "ok": True,
                "skipped": True,
                "version": version,
                "count": existing,
                "elapsed": round(time.time() - started, 1),
            }

        data = json.loads(raw)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[星铁面板] 同步行迹倾向失败：{message}")
        return {"ok": False, "error": message, "count": 0}

    characters = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(characters, dict):
        return {"ok": False, "error": "上游数据结构异常", "count": 0}

    hints: Dict[str, Dict[str, str]] = {}
    for avatar_id, item in characters.items():
        if not isinstance(item, dict):
            continue
        hint = _build_hint(item.get("traces") or {})
        if not hint:
            continue
        hints[str(avatar_id)] = {
            "name": str(item.get("name") or ""),
            "hint": hint,
        }

    if not hints:
        return {"ok": False, "error": "没有解析出任何行迹倾向", "count": 0}

    payload = {
        "_version": version,
        "_synced_at": int(time.time()),
        "characters": hints,
    }
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}", "count": 0}

    elapsed = round(time.time() - started, 1)
    logger.info(
        f"[星铁面板] 行迹倾向同步完成：{len(hints)} 个角色，耗时 {elapsed}s"
    )
    return {
        "ok": True,
        "version": version,
        "count": len(hints),
        "elapsed": elapsed,
    }
