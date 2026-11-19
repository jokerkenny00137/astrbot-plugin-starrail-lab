"""星穹铁道玩家面板查询 —— AstrBot 插件入口。

指令：
    /面板 <UID>          查询玩家的角色面板
    /面板 <UID> 刷新      忽略缓存，重新联网查询

面板数据来自玩家公开的角色展示柜，包含：
    角色等级、命座、光锥、攻击/生命/防御/速度、暴击率、暴击伤害、
    属性伤害加成、每件遗器的主词条与副词条。
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 以包的形式加载插件
    from .player_panel import (
        PlayerQueryError,
        format_player_report,
        query_player,
        set_cache_dir,
    )
except ImportError:  # 以普通脚本方式被导入时
    from player_panel import (
        PlayerQueryError,
        format_player_report,
        query_player,
        set_cache_dir,
    )


PLUGIN_NAME = "astrbot_plugin_starrail_lab"

# 查询超时（秒）。公共接口排队时可能很慢，给足时间但不无限等待。
QUERY_TIMEOUT = 45

# 单条消息最大字符数，超长就分片发送，避免被平台截断
MAX_CHUNK = 1200

# 表示"强制刷新"的关键词
REFRESH_WORDS = {"刷新", "更新", "重新", "refresh", "-r", "-R"}


def _split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """按行切分长文本。"""
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks or [text]


class StarRailPanelPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._cache_dir = self._resolve_cache_dir()
        logger.info(f"[星铁面板] 已加载，缓存目录：{self._cache_dir}")

    def _resolve_cache_dir(self) -> Path:
        """缓存放到 AstrBot 的插件数据目录，不污染插件源码目录。"""
        try:
            from astrbot.api.star import StarTools

            cache_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "cache"
        except Exception:
            # 本地脚本调试时没有 AstrBot，退回插件目录
            cache_dir = Path(__file__).resolve().parent / "cache"

        cache_dir.mkdir(parents=True, exist_ok=True)
        set_cache_dir(cache_dir)
        return cache_dir

    @filter.command("面板", alias={"星铁面板", "查面板"})
    async def query_panel(
        self,
        event: AstrMessageEvent,
        uid: str = "",
        extra: str = "",
    ):
        """查询崩坏：星穹铁道玩家的角色面板。

        Args:
            uid(string): 玩家 UID，纯数字
            extra(string): 可选，填「刷新」则忽略缓存重新查询
        """
        uid = (uid or "").strip()

        if not uid.isdigit():
            yield event.plain_result(
                "用法：/面板 <UID>\n"
                "例如：/面板 100000000\n"
                "忽略缓存重新查：/面板 100000000 刷新"
            )
            return

        force_refresh = (extra or "").strip().lower() in REFRESH_WORDS

        try:
            result = await asyncio.wait_for(
                query_player(uid, force_refresh=force_refresh),
                timeout=QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"查询超时了（超过 {QUERY_TIMEOUT} 秒）。\n"
                "公共数据接口有时会排队，稍后再试一次吧。"
            )
            return
        except PlayerQueryError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[星铁面板] 查询 UID {uid} 时出错")
            yield event.plain_result(f"查询出错了：{type(exc).__name__}: {exc}")
            return

        if not result.get("ok"):
            yield event.plain_result(
                "没有查到任何角色。\n"
                "可能原因：对方没有公开角色展示柜，或者 UID 填错了。"
            )
            return

        for chunk in _split_text(format_player_report(result)):
            yield event.plain_result(chunk)

    async def terminate(self):
        """插件被卸载或重载时调用。"""
        logger.info("[星铁面板] 插件已卸载")
