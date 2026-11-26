"""从 StarRailRes 同步角色机制卡。

数据源是社区整理的**游戏文案**（`simple_desc` 为定性描述，不含占位符），
不是模型生成的，所以这里只做格式转换，不做总结和改写——也因此不需要人工审核。

更新判断用上游仓库的 commit，而不是游戏版本号：
上游一改，说明资料有变化，重新读一遍整组数据即可。

同步是懒加载的：插件启动后跑一次，之后按间隔在查询时顺带检查，
无人提问的时段完全不联网。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

try:  # AstrBot 以包的形式加载插件
    from .guide_store import get_auto_dir, reload_guides
except ImportError:  # 以普通脚本方式被导入时
    from guide_store import get_auto_dir, reload_guides

try:
    import aiohttp
except Exception:  # 极简环境没有 aiohttp 时同步不可用
    aiohttp = None  # type: ignore[assignment]

# 中文数据：角色、技能、星魂、命途、属性
SR_BASE = "https://cdn.jsdelivr.net/gh/Mar-7th/StarRailRes@master/index_min/cn/"
SR_FILES = (
    "characters.json",
    "character_skills.json",
    "character_ranks.json",
    "paths.json",
    "elements.json",
)

# 版本标识不用 GitHub commit（未认证会 403，jsDelivr 也不返回该仓库版本），
# 直接对角色表做内容哈希：上游一改，哈希必然变化，且不依赖任何第三方接口。
HEADERS = {"User-Agent": "astrbot-plugin-starrail-lab"}

VERSION_FILE = ".source_version"
HTTP_TIMEOUT = 15.0

# 技能按游戏内顺序排列，认不出的类型排最后
SKILL_ORDER = {"普攻": 1, "战技": 2, "终结技": 3, "天赋": 4, "秘技": 5}

# 文案里的数值占位符，例如 #1[i]%
_PLACEHOLDER = re.compile(r"#\d+\[[^\]]*\]")

# 技能名里用竖线包住的部分是备注标记，例如「|系统警告|」
_NAME_NOISE = re.compile(r"[|｜]")

# 单条描述超过这个长度就截断，避免一条占满整张卡
_ITEM_LIMIT = 220


def _clean(text: Any, limit: int = _ITEM_LIMIT) -> str:
    """清理文案：占位符换成 X，压掉多余空白，超长截断。"""
    value = _PLACEHOLDER.sub("X", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[:limit] + "…"
    return value


def _clean_name(text: Any) -> str:
    """技能/星魂名去掉竖线标记。"""
    return _NAME_NOISE.sub("", str(text or "")).strip()


def _version_path() -> Path:
    return get_auto_dir() / VERSION_FILE


def read_local_version() -> str:
    """已同步的上游 commit，没有则返回空字符串。"""
    try:
        text = _version_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.splitlines()[0].strip() if text else ""


def read_synced_at() -> float:
    """上次同步成功的时间戳，读不到返回 0。"""
    try:
        lines = _version_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    if len(lines) < 2:
        return 0.0
    try:
        return float(lines[1].strip())
    except (TypeError, ValueError):
        return 0.0


def count_auto_guides() -> int:
    try:
        return sum(1 for _ in get_auto_dir().glob("*.md"))
    except OSError:
        return 0


def build_card(
    character: Dict[str, Any],
    skills: Dict[str, Any],
    ranks: Dict[str, Any],
    paths: Dict[str, Any],
    elements: Dict[str, Any],
    version: str,
) -> str:
    """把一个角色转成机制卡 Markdown。只搬运文案，不改写。"""
    name = str(character.get("name") or "").strip()
    if not name:
        return ""

    path_name = str((paths.get(str(character.get("path") or "")) or {}).get("name") or "")
    element_name = str(
        (elements.get(str(character.get("element") or "")) or {}).get("name") or ""
    )
    tags = " · ".join(part for part in (element_name, path_name) if part)
    title = f"{name}（{tags}）" if tags else name

    # 上游同一个技能会给出多个版本（不同等级/含强化描述），只保留最完整的一条
    skill_best: Dict[Tuple[str, str], Tuple[int, str, str, str]] = {}
    for skill_id in character.get("skills") or []:
        skill = skills.get(str(skill_id))
        if not isinstance(skill, dict):
            continue
        # simple_desc 是定性描述、没有占位符，优先用它；没有才退回完整描述
        desc = _clean(skill.get("simple_desc") or skill.get("desc"))
        if not desc:
            continue
        type_text = _clean_name(skill.get("type_text"))
        # 只要五类主要技能，其他是内部条目（如「攻击」这类进战描述）
        if type_text not in SKILL_ORDER:
            continue
        skill_name = _clean_name(skill.get("name"))
        key = (type_text, skill_name)
        old = skill_best.get(key)
        if old is None or len(desc) > len(old[3]):
            skill_best[key] = (
                SKILL_ORDER.get(type_text, 9),
                type_text,
                skill_name,
                desc,
            )
    skill_rows = sorted(skill_best.values(), key=lambda row: (row[0], row[2]))

    # 星魂同理：同一命座有多条描述时取最长的那条
    rank_best: Dict[int, Tuple[int, str, str]] = {}
    for rank_id in character.get("ranks") or []:
        rank = ranks.get(str(rank_id))
        if not isinstance(rank, dict):
            continue
        desc = _clean(rank.get("desc"))
        if not desc:
            continue
        try:
            rank_no = int(rank.get("rank") or 0)
        except (TypeError, ValueError):
            rank_no = 0
        old = rank_best.get(rank_no)
        if old is None or len(desc) > len(old[2]):
            rank_best[rank_no] = (rank_no, _clean_name(rank.get("name")), desc)
    rank_rows = [rank_best[key] for key in sorted(rank_best)]

    if not skill_rows and not rank_rows:
        return ""

    lines = [
        f"# {title}",
        "",
        f"> 数据来源：StarRailRes（自动同步，{version[:8] or '未知版本'}）。",
        "> 内容为技能与星魂的定性描述，具体数值以游戏内为准。",
        "",
    ]

    if skill_rows:
        lines.append("## 技能")
        for _, type_text, skill_name, desc in skill_rows:
            label = f"「{skill_name}」" if skill_name else ""
            lines.append(f"- **{type_text}**{label}：{desc}")
        lines.append("")

    if rank_rows:
        lines.append("## 星魂")
        for rank_no, rank_name, desc in rank_rows:
            label = f"「{rank_name}」" if rank_name else ""
            prefix = f"{rank_no}命" if rank_no else "星魂"
            lines.append(f"- **{prefix}**{label}：{desc}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


async def _get_json(session: Any, url: str) -> Any:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


def signature_of(characters: Any) -> str:
    """对角色表算内容哈希，作为「资料版本」。"""
    try:
        raw = json.dumps(characters, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _fetch_rest(session: Any) -> Dict[str, Any]:
    """并发拉除角色表外的数据，任何一个失败就整体放弃，避免写出半套资料。"""
    names = tuple(name for name in SR_FILES if name != "characters.json")
    results = await asyncio.gather(
        *[_get_json(session, SR_BASE + name) for name in names],
        return_exceptions=True,
    )
    data: Dict[str, Any] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            raise RuntimeError(f"{name}: {type(result).__name__}: {result}")
        data[name] = result
    return data


async def sync_guides(force: bool = False) -> Dict[str, Any]:
    """把上游资料同步成本地机制卡。

    force=False 时，资料哈希没变就直接跳过，不写任何文件。
    """
    if aiohttp is None:
        return {"ok": False, "error": "缺少 aiohttp，无法联网同步", "written": 0}

    auto_dir = get_auto_dir()
    try:
        auto_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"创建目录失败：{exc}", "written": 0}

    started = time.time()
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            # 先只拉角色表：哈希没变就不用下整套数据
            characters = await _get_json(session, SR_BASE + "characters.json")
            version = signature_of(characters)
            if not version:
                return {"ok": False, "error": "上游角色表格式异常", "written": 0}

            local_version = read_local_version()
            existing = count_auto_guides()
            if not force and local_version == version and existing:
                return {
                    "ok": True,
                    "skipped": True,
                    "version": version,
                    "written": 0,
                    "total": existing,
                    "elapsed": round(time.time() - started, 1),
                }

            rest = await _fetch_rest(session)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[星铁面板] 同步机制卡失败：{message}")
        return {"ok": False, "error": message, "written": 0}

    if not isinstance(characters, dict):
        return {"ok": False, "error": "上游角色表格式异常", "written": 0}
    skills = rest.get("character_skills.json") or {}
    ranks = rest.get("character_ranks.json") or {}
    paths = rest.get("paths.json") or {}
    elements = rest.get("elements.json") or {}

    written = 0
    failed = 0
    used_names: set[str] = set()
    for character in characters.values():
        if not isinstance(character, dict):
            continue
        try:
            text = build_card(character, skills, ranks, paths, elements, version)
        except Exception as exc:  # 单个角色出错不影响其他角色
            failed += 1
            logger.debug(f"[星铁面板] 生成机制卡失败：{type(exc).__name__}: {exc}")
            continue
        if not text:
            continue
        name = _clean_name(character.get("name"))
        if not name:
            continue

        # 同名角色（开拓者的不同命途等）用标题区分，避免互相覆盖
        filename = name
        if filename in used_names:
            title = text.splitlines()[0].lstrip("#").strip()
            filename = title or name
        index = 2
        base_name = filename
        while filename in used_names:
            filename = f"{base_name}（{index}）"
            index += 1
        used_names.add(filename)

        try:
            (auto_dir / f"{filename}.md").write_text(text, encoding="utf-8")
            written += 1
        except OSError as exc:
            failed += 1
            logger.warning(f"[星铁面板] 写入机制卡失败（{filename}）：{exc}")

    if written:
        try:
            _version_path().write_text(
                f"{version}\n{int(time.time())}\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[星铁面板] 记录同步版本失败：{exc}")

    # 让缓存立刻看到新文件
    try:
        reload_guides()
    except Exception:
        pass

    elapsed = round(time.time() - started, 1)
    if written:
        logger.info(
            f"[星铁面板] 机制卡同步完成：{written} 个角色，"
            f"版本 {version[:8]}，耗时 {elapsed}s"
        )
    else:
        logger.warning(
            f"[星铁面板] 机制卡同步没有生成任何文件"
            f"（上游角色 {len(characters)} 个，失败 {failed} 个）"
        )

    return {
        "ok": written > 0,
        "version": version,
        "written": written,
        "failed": failed,
        "total": len(characters),
        "elapsed": elapsed,
        "error": "" if written else "没有生成任何机制卡",
    }
