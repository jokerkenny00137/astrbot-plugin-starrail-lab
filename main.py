"""星穹铁道玩家面板查询 —— AstrBot 插件入口。

指令：
    /面板                   查看自己绑定账号的角色清单
    /面板 <UID>             查看该 UID 的角色清单
    /面板 <UID> 角色名       查看某个角色的完整面板
    /面板 <UID> 序号         同上，用清单里的编号
    /面板 <UID> 全部         一次看全部角色
    /面板 <UID> 刷新         忽略缓存重新拉取

    /绑定 <UID>              把自己的 QQ 与游戏 UID 绑定
    /解绑                    解除绑定

特性：
    * 用户查完面板继续聊天时，数据会自动注入 AI 上下文，让 AI 能据此给建议
    * 支持定时自动更新所有已绑定玩家的面板数据
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 以包的形式加载插件
    from .panel_store import (
        bind_user,
        get_bound_uid,
        get_last_query,
        list_bindings,
        save_last_query,
        save_panel,
        set_db_path,
        unbind_user,
    )
    from .player_panel import (
        PlayerQueryError,
        build_llm_summary,
        find_character,
        format_character_list,
        format_character_panel,
        format_player_report,
        query_player,
        set_cache_dir,
    )
except ImportError:  # 以普通脚本方式被导入时
    from panel_store import (
        bind_user,
        get_bound_uid,
        get_last_query,
        list_bindings,
        save_last_query,
        save_panel,
        set_db_path,
        unbind_user,
    )
    from player_panel import (
        PlayerQueryError,
        build_llm_summary,
        find_character,
        format_character_list,
        format_character_panel,
        format_player_report,
        query_player,
        set_cache_dir,
    )


PLUGIN_NAME = "astrbot_plugin_starrail_lab"

# 单次查询超时（秒）
QUERY_TIMEOUT = 45

# 单条消息最大字符数，超长就分片发送
MAX_CHUNK = 1200

# 表示"强制刷新"的关键词
REFRESH_WORDS = {"刷新", "更新", "重新", "refresh", "-r"}

# 表示"看全部"的关键词
ALL_WORDS = {"全部", "所有", "全", "all"}

# 注入 AI 上下文的默认有效期（秒），可在插件配置里改
DEFAULT_CONTEXT_TTL = 1800


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
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._data_dir = self._resolve_data_dir()
        self._auto_task: asyncio.Task | None = None
        logger.info(f"[星铁面板] 已加载，数据目录：{self._data_dir}")

        minutes = int(self.config.get("auto_update_minutes", 0) or 0)
        if minutes > 0:
            self._auto_task = asyncio.create_task(self._auto_update_loop(minutes))
            logger.info(f"[星铁面板] 自动更新已开启，间隔 {minutes} 分钟")

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _resolve_data_dir(self) -> Path:
        """数据放 AstrBot 的 plugin_data 目录，不污染插件源码。"""
        try:
            from astrbot.api.star import StarTools

            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            data_dir = Path(__file__).resolve().parent / "data"

        data_dir.mkdir(parents=True, exist_ok=True)
        set_cache_dir(data_dir / "cache")
        set_db_path(data_dir / "starrail.db")
        return data_dir

    def _session_key(self, event: AstrMessageEvent) -> str:
        """会话标识。同一个群/私聊算一个会话。"""
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return str(umo)
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    def _resolve_uid(self, event: AstrMessageEvent, uid: str) -> str:
        """没填 UID 就用绑定的那个。"""
        uid = (uid or "").strip()
        if uid.isdigit():
            return uid
        bound = get_bound_uid(str(event.get_sender_id()), event.get_platform_name())
        return bound or ""

    # ------------------------------------------------------------------
    # 面板查询
    # ------------------------------------------------------------------

    @filter.command(
        "面板",
        alias={
            "星铁面板",
            "崩铁面板",
            "查面板",
            "角色面板",
            "星穹面板",
            "查uid",
        },
    )
    async def query_panel(
        self,
        event: AstrMessageEvent,
        uid: str = "",
        extra: str = "",
    ):
        """查询崩坏：星穹铁道玩家的角色面板。

        Args:
            uid(string): 玩家 UID，纯数字；不填则使用已绑定的 UID
            extra(string): 可选，角色名 / 序号 / 全部 / 刷新
        """
        uid = self._resolve_uid(event, uid)

        if not uid:
            yield event.plain_result(
                "用法：/面板 <UID>\n"
                "例如：/面板 100000000\n"
                "也可以先绑定：/绑定 100000000，之后直接发 /面板"
            )
            return

        extra = (extra or "").strip()
        force_refresh = extra.lower() in REFRESH_WORDS
        if force_refresh:
            extra = ""

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

        # 存档，供 AI 上下文注入和定时更新使用
        self._remember(self._session_key(event), uid, result)

        # ---- 决定输出什么 ----
        if not extra:
            for chunk in _split_text(format_character_list(result)):
                yield event.plain_result(chunk)
            return

        if extra.lower() in ALL_WORDS:
            for chunk in _split_text(format_player_report(result)):
                yield event.plain_result(chunk)
            return

        panel = find_character(result, extra)
        if panel is None:
            names = "、".join(c["name"] for c in result["characters"][:8])
            yield event.plain_result(
                f"没找到角色「{extra}」。\n"
                f"这个展示柜里有：{names}\n"
                "回复序号或角色名试试，比如：/面板 "
                f"{uid} 1"
            )
            return

        for chunk in _split_text(format_character_panel(panel)):
            yield event.plain_result(chunk)

    def _remember(self, session_key: str, uid: str, result: dict) -> None:
        """把这次查询存下来：会话上下文 + 完整存档。"""
        try:
            summary = build_llm_summary(result)
            save_last_query(session_key, uid, result.get("nickname", ""), summary)
            save_panel(uid, result.get("nickname", ""), result)
        except Exception:
            logger.exception("[星铁面板] 保存查询记录失败")

    # ------------------------------------------------------------------
    # 让 AI 能看见这些数据
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_panel_context(self, event: AstrMessageEvent, req):
        """在请求大模型前，把用户刚查过的面板数据塞进 system_prompt。

        这就是"查完面板后，机器人还能接着聊"的关键。
        """
        try:
            ttl = int(self.config.get("llm_context_ttl_minutes", 0) or 0) * 60
            if ttl <= 0:
                ttl = DEFAULT_CONTEXT_TTL
            record = get_last_query(self._session_key(event), max_age=ttl)
            if not record or not record.get("summary"):
                return

            block = (
                "\n\n【用户刚刚查询的游戏面板数据】\n"
                "以下是用户通过 /面板 指令查到的《崩坏：星穹铁道》角色面板，"
                "数据来自游戏内公开的角色展示柜，是真实的。\n"
                "回答与配装、面板、遗器、光锥相关的问题时，请**优先以此数据为依据**，"
                "不要凭记忆编造数值，也不要建议用户重新发一遍数据。\n"
                f"{record['summary']}\n"
            )
            req.system_prompt = (req.system_prompt or "") + block
        except Exception:
            # 注入失败绝不能影响正常聊天
            logger.exception("[星铁面板] 注入上下文失败")

    # ------------------------------------------------------------------
    # 绑定
    # ------------------------------------------------------------------

    @filter.command("绑定", alias={"绑定uid", "bind"})
    async def bind(self, event: AstrMessageEvent, uid: str = ""):
        """把自己的 QQ 和游戏 UID 绑定。

        Args:
            uid(string): 游戏 UID，纯数字
        """
        uid = (uid or "").strip()
        if not uid.isdigit():
            yield event.plain_result("用法：/绑定 <UID>\n例如：/绑定 100000000")
            return

        bind_user(
            str(event.get_sender_id()),
            uid,
            event.get_platform_name(),
        )
        yield event.plain_result(
            f"绑定成功！\n之后直接发 /面板 就能查 UID {uid} 的角色。"
        )

    @filter.command("解绑", alias={"解除绑定", "unbind"})
    async def unbind(self, event: AstrMessageEvent):
        """解除 QQ 与 UID 的绑定。"""
        ok = unbind_user(str(event.get_sender_id()), event.get_platform_name())
        if ok:
            yield event.plain_result("已解除绑定。")
        else:
            yield event.plain_result("你还没有绑定过 UID。")

    @filter.command("我的绑定", alias={"绑定信息", "mybind"})
    async def my_binding(self, event: AstrMessageEvent):
        """查看自己绑定的 UID。"""
        uid = get_bound_uid(str(event.get_sender_id()), event.get_platform_name())
        if uid:
            yield event.plain_result(f"你绑定的 UID：{uid}")
        else:
            yield event.plain_result("你还没有绑定 UID，可以发 /绑定 <UID>")

    @filter.command("绑定列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_binding(self, event: AstrMessageEvent):
        """查看所有绑定记录（仅管理员）。"""
        rows = list_bindings()
        if not rows:
            yield event.plain_result("目前还没有任何绑定记录。")
            return

        lines = [f"共有 {len(rows)} 条绑定记录："]
        for row in rows[:50]:
            lines.append(f"  {row['user_id']}  ->  {row['uid']}")
        if len(rows) > 50:
            lines.append(f"  ...（还有 {len(rows) - 50} 条）")
        for chunk in _split_text("\n".join(lines)):
            yield event.plain_result(chunk)

    # ------------------------------------------------------------------
    # 定时自动更新
    # ------------------------------------------------------------------

    async def _auto_update_loop(self, interval_minutes: int) -> None:
        """后台任务：定期刷新所有已绑定玩家的面板并入库。"""
        while True:
            await asyncio.sleep(interval_minutes * 60)
            try:
                await self._update_all_bound()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[星铁面板] 自动更新时出错")

    async def _update_all_bound(self) -> None:
        uids = {row["uid"] for row in list_bindings(1000)}
        if not uids:
            logger.info("[星铁面板] 自动更新：没有绑定记录，跳过")
            return

        ok = 0
        for uid in uids:
            try:
                result = await query_player(uid, force_refresh=True)
                if result.get("ok"):
                    save_panel(uid, result.get("nickname", ""), result)
                    ok += 1
            except Exception as exc:
                logger.warning(f"[星铁面板] 自动更新 UID {uid} 失败：{exc}")
            await asyncio.sleep(2)  # 别把公共接口打爆

        logger.info(f"[星铁面板] 自动更新完成：成功 {ok}/{len(uids)}")

    # ------------------------------------------------------------------

    async def terminate(self):
        """插件被卸载或重载时调用。"""
        if self._auto_task:
            self._auto_task.cancel()
        logger.info("[星铁面板] 插件已卸载")
