"""本地角色机制卡（guides/*.md）的加载与查询。

机制卡用来解决「AI 凭记忆编造技能机制」的问题：把角色定位、核心机制、
关键属性写成短文档，AI 回答前先查，查不到就明确说没有资料。

目录优先级（后者覆盖前者）：
    1. 插件自带的 guides/（随插件分发）
    2. plugin_data/guides/auto/（自动从 StarRailRes 同步，可被重新生成）
    3. plugin_data/guides/（用户手写，优先级最高，不会被同步覆盖）
同名文件以优先级高的为准。

文件格式（首行必须是标题，匹配靠它）：
    # 黄泉（虚数 · 虚无 · 主C）
"""

from __future__ import annotations

import re
import time

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

# 随插件分发的机制卡
BUILTIN_DIR = Path(__file__).resolve().parent / "guides"

# 用户可改的机制卡目录（由 main.py 指向 plugin_data/guides）
_USER_DIR: Path = Path(__file__).resolve().parent / "data" / "guides"

# 自动同步生成的子目录：可以随时被重新生成，所以和手写资料分开
AUTO_DIRNAME = "auto"

# 文件名 -> (mtime, 正文)
_CACHE: Dict[str, Tuple[float, str]] = {}

# 每条消息都要匹配角色名，目录扫描做 2 秒节流，避免每条消息都 glob 一遍
_SCAN_INTERVAL = 2.0
_LAST_SCAN = 0.0

# 单张机制卡给到模型的上限，超长会挤占上下文
MAX_GUIDE_CHARS = 1200


def set_guide_dir(path: str | Path) -> Path:
    """设置用户机制卡目录，并立即重载。"""
    global _USER_DIR
    _USER_DIR = Path(path)
    try:
        _USER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(f"[星铁面板] 创建机制卡目录失败：{type(exc).__name__}: {exc}")
    reload_guides()
    return _USER_DIR


def _scan(folder: Path) -> Dict[str, Path]:
    """列出目录下所有 .md，文件名去掉后缀作为角色名。"""
    found: Dict[str, Path] = {}
    try:
        if not folder.is_dir():
            return found
        for item in folder.glob("*.md"):
            if item.is_file():
                found[item.stem] = item
    except OSError as exc:
        logger.warning(f"[星铁面板] 读取机制卡目录失败（{folder}）：{exc}")
    return found


def get_auto_dir() -> Path:
    """自动同步目录：plugin_data/guides/auto。"""
    return _USER_DIR / AUTO_DIRNAME


def get_user_dir() -> Path:
    """用户手写目录：plugin_data/guides。"""
    return _USER_DIR


def reload_guides(force: bool = False) -> Dict[str, str]:
    """重新扫描三个目录，按 mtime 增量读取，返回现有全部机制卡。

    2 秒内重复调用会直接返回缓存（每条消息都要匹配角色名，不做节流太浪费）；
    手动改完文件想立刻生效时传 force=True。
    """
    global _CACHE, _LAST_SCAN

    now = time.time()
    if not force and _CACHE and now - _LAST_SCAN < _SCAN_INTERVAL:
        return {name: text for name, (_, text) in _CACHE.items()}
    _LAST_SCAN = now

    files: Dict[str, Path] = {}
    # 顺序即优先级：后面的同名文件覆盖前面的
    files.update(_scan(BUILTIN_DIR))
    files.update(_scan(get_auto_dir()))
    files.update(_scan(_USER_DIR))

    fresh: Dict[str, Tuple[float, str]] = {}
    for name, path in files.items():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        cached = _CACHE.get(name)
        if cached is not None and cached[0] == mtime:
            fresh[name] = cached
            continue

        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(f"[星铁面板] 读取机制卡失败（{path}）：{exc}")
            continue
        if text:
            fresh[name] = (mtime, text)

    _CACHE = fresh
    if not _USER_DIR.exists():
        try:
            _USER_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return {name: text for name, (_, text) in _CACHE.items()}


def load_guides() -> Dict[str, str]:
    """返回 {角色名: 正文}。每次调用都会重新扫描目录，但只重读改动过的文件。"""
    return reload_guides()


def list_guide_names() -> List[str]:
    """已收录的角色名，按拼音无关的稳定顺序（文件名排序）。"""
    return sorted(load_guides())


def _norm(text: str) -> str:
    """归一化：去掉空白、常见分隔符、括号补充说明，统一小写。"""
    value = str(text or "")
    value = re.sub(r"[（(][^（()）]*[)）]", "", value)
    value = re.sub(r"[\s·・、,，。.．:：\-_/\\]+", "", value)
    return value.lower()


def get_guide(name: str) -> Optional[str]:
    """按角色名取机制卡：精确 → 归一化相等 → 包含匹配。"""
    guides = load_guides()
    if not guides:
        return None

    raw = str(name or "").strip()
    if not raw:
        return None
    if raw in guides:
        return guides[raw]

    key = _norm(raw)
    if not key:
        return None

    for full, text in guides.items():
        if _norm(full) == key:
            return text

    # 「黄泉的机制」「黄泉 虚数」这类多带了字的情况，退一步做包含匹配
    for full, text in guides.items():
        norm_full = _norm(full)
        if key and (key in norm_full or norm_full in key):
            return text
    return None


def match_guide_names(text: str, limit: int = 2) -> List[str]:
    """从一段话里找出提到的已收录角色，用于请求前预注入资料。

    归一化之后比较，所以「阮·梅」「阮 梅」都能命中「阮梅」。
    单字角色名（如「刃」）容易误伤（「锋刃」「刀刃」），要求前后不是汉字。
    """
    raw = str(text or "")
    if not raw:
        return []

    norm_text = _norm(raw)
    if not norm_text:
        return []

    hits: List[Tuple[int, str]] = []
    for name in load_guides():
        norm_name = _norm(name)
        if not norm_name:
            continue
        if len(norm_name) >= 2:
            pos = norm_text.find(norm_name)
            matched = pos >= 0
        else:
            # 只卡前边界：「锋刃」「白刃」不算，「刃的配队」算
            found = re.search(
                rf"(?<![\u4e00-\u9fa5]){re.escape(norm_name)}", norm_text
            )
            pos = found.start() if found else -1
            matched = found is not None
        if matched:
            hits.append((pos, name))

    # 按在句子里出现的先后排序：先提到的角色优先注入
    hits.sort(key=lambda item: item[0])
    return [name for _, name in hits[:limit]]


def get_guide_title(text: str) -> str:
    """取机制卡首行的标题，用于日志与列表展示。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:40]
    return ""


def clip_guide(text: str, limit: int = MAX_GUIDE_CHARS) -> str:
    """机制卡超长时截断，避免一次工具调用吃掉太多上下文。"""
    text = str(text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n（资料较长，已截断）"
