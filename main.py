"""星穹铁道玩家面板查询 —— AstrBot 插件入口。

指令：
    /面板                   查看自己绑定账号的角色清单
    /面板 <UID>             查看该 UID 的角色清单
    /面板 <UID> 角色名       查看某个角色的完整面板
    /面板 <UID> 序号         同上，用清单里的编号
    /面板 <UID> 全部         一次看全部角色
    /面板 <UID> 刷新         兼容写法，推荐直接用 /刷新 <UID>

    /绑定 <UID>              把自己的 QQ 与游戏 UID 绑定
    /解绑                    解除绑定
    /刷新 [UID]              后台刷新该玩家的数据与角色卡；不填 UID 则刷新最近查看/绑定的玩家
    /刷新全部面板            管理员指令，后台刷新所有已绑定玩家

    /角色资料                查看已收录的角色机制卡
    /角色资料 <角色名>        预览某个角色的机制卡
    /重载资料                管理员指令，改完 guides/ 后重新读取
    /同步资料                管理员指令，立刻从 StarRailRes 同步机制卡
                             （顺带更新 Fribbels 的角色行迹倾向）

特性：
    * 查询默认读本地存档，只有「刷新」/ 管理员 / 定时任务才联网
    * 用户查完面板继续聊天时，数据会自动注入 AI 上下文，让 AI 能据此给建议
    * 支持定时自动更新所有已绑定玩家的面板数据
    * 刷新后在后台预渲染角色卡到本地，查询时优先发本地图
    * AI 回答角色机制前会调用 query_character_guide 查本地机制卡，查不到就直说
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import time

from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 临时上下文内容块（AstrBot 4.24+，旧版本则跳过注入）
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None  # type: ignore[assignment]

try:  # 可视化视图用的 Web API 辅助函数（AstrBot 4.24+）
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except ImportError:
    WEB_API_AVAILABLE = False
    error_response = json_response = request = None  # type: ignore[assignment]

try:  # AstrBot 以包的形式加载插件
    from .guide_store import (
        clip_guide,
        get_guide,
        list_guide_names,
        match_guide_names,
        set_guide_dir,
    )
    from .guide_store import reload_guides as reload_guide_files
    from .guide_sync import (
        count_auto_guides,
        read_synced_at,
        read_local_version,
        sync_guides,
    )
    from .trace_sync import count_traces, sync_traces
    from .trace_sync import read_synced_at as read_trace_synced_at
    from .panel_card import TEMPLATE_FILE, aiohttp as card_aiohttp
    from .panel_card import (
        build_card_html,
        card_html_path,
        card_image_path,
        get_render_status,
        probe_render,
        render_character_card,
        save_card_html,
        set_card_cache_dir,
    )
    from .panel_store import (
        bind_user,
        cleanup_unbound_panels,
        get_bound_uid,
        get_last_query,
        get_panel,
        list_bindings,
        list_panels,
        save_last_query,
        save_panel,
        set_db_path,
        unbind_user,
    )
    from .player_panel import (
        LIB_AVAILABLE,
        PlayerQueryError,
        build_llm_summary,
        find_character,
        format_character_list,
        format_character_panel,
        format_player_report,
        format_relic_detail,
        query_player,
        set_cache_dir,
    )
except ImportError:  # 以普通脚本方式被导入时
    from guide_store import (
        clip_guide,
        get_guide,
        list_guide_names,
        match_guide_names,
        set_guide_dir,
    )
    from guide_store import reload_guides as reload_guide_files
    from guide_sync import (
        count_auto_guides,
        read_synced_at,
        read_local_version,
        sync_guides,
    )
    from trace_sync import count_traces, sync_traces
    from trace_sync import read_synced_at as read_trace_synced_at
    from panel_card import TEMPLATE_FILE, aiohttp as card_aiohttp
    from panel_card import (
        build_card_html,
        card_html_path,
        card_image_path,
        get_render_status,
        probe_render,
        render_character_card,
        save_card_html,
        set_card_cache_dir,
    )
    from panel_store import (
        bind_user,
        cleanup_unbound_panels,
        get_bound_uid,
        get_last_query,
        get_panel,
        list_bindings,
        list_panels,
        save_last_query,
        save_panel,
        set_db_path,
        unbind_user,
    )
    from player_panel import (
        LIB_AVAILABLE,
        PlayerQueryError,
        build_llm_summary,
        find_character,
        format_character_list,
        format_character_panel,
        format_player_report,
        format_relic_detail,
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

# 练度分析说明词：随面板数据一起交给 AI，用户可在控制面板里改
DEFAULT_ANALYSIS_PROMPT = (
    "当用户要求分析角色练度、强度、配装或是否值得培养时，按以下结构回答，"
    "结论先行、用数据说话：\n"
    "1. 结论：先说角色定位（主C/副C/辅助/生存/击破等）和当前练度档位"
    "（偏低/合格/优秀），以及是否建议继续投入。\n"
    "2. 面板体检：对照该角色机制，逐项判断关键属性是否达标"
    "（暴击率/暴击伤害/速度/击破特攻/效果命中/治疗量/能量恢复等，只挑相关的说），"
    "给出参考区间，指出差距最大的项。\n"
    "3. 遗器诊断：套装是否契合定位；主词条是否正确；副词条有效词条数/评分如何"
    "（数据里有分数就引用），明确指出最该替换的 1~2 件及原因。\n"
    "4. 光锥与命座：光锥是否契合、叠影等级的影响；命座是否改变定位或值得继续抽。\n"
    "5. 提升优先级：按性价比给出不超过 3 条建议（遗器 > 光锥 > 行迹 > 命座），"
    "说明每条大概能带来什么提升。\n"
    "6. 数据不足时直接说明缺什么（例如没有遗器、缺少关键属性），不要编造数值。\n"
    "要求：使用游戏内术语；引用面板里的具体数值；不要复述整份面板；"
    "不要用“很强/很弱”这类空泛评价。"
)

# 控制面板能改的设置。类型、范围和默认值都写在这里，前端只负责展示。
SETTING_SPECS = {
    "panel_render_mode": {
        "label": "角色详情输出方式",
        "group": "输出与渲染",
        "type": "select",
        "options": ["auto", "image", "text"],
        "default": "auto",
        "hint": "auto：能出图就出图，失败回退文字；image：同 auto，但失败会写日志；text：只用文字",
    },
    "card_render_timeout": {
        "label": "角色卡渲染超时（秒）",
        "group": "输出与渲染",
        "type": "int",
        "min": 5,
        "max": 120,
        "default": 30,
        "hint": "远端渲染服务较慢时调大，范围 5~120；超时后会自动用 20 秒重试一次",
    },
    "card_fail_cooldown_minutes": {
        "label": "角色卡失败后冷却（分钟）",
        "group": "输出与渲染",
        "type": "int",
        "min": 0,
        "max": 120,
        "default": 5,
        "hint": "渲染失败后这段时间内直接走文字，填 0 表示每次都尝试",
    },
    "precache_cards": {
        "label": "刷新后自动预渲染角色卡",
        "group": "角色卡与缓存",
        "type": "bool",
        "default": True,
        "hint": "刷新数据后在后台把角色卡存到本地，查询时直接发本地图，减少渲染失败",
    },
    "card_inline_images": {
        "label": "角色卡图片内嵌",
        "group": "角色卡与缓存",
        "type": "bool",
        "default": True,
        "hint": "开启后把头像/光锥/遗器图转成 base64 一起发给渲染端，图更稳但请求体大；渲染超时可尝试关闭",
    },
    "card_endpoint_rotation": {
        "label": "自动切换渲染端点",
        "group": "角色卡与缓存",
        "type": "bool",
        "default": True,
        "hint": "从 AstrBot 官方端点列表里逐个尝试，失败快速换下一个，避免卡在一个 503 的端点上",
    },
    "card_render_endpoint": {
        "label": "自定义渲染端点",
        "group": "角色卡与缓存",
        "type": "text",
        "default": "",
        "hint": "可选，例如 https://t2i.example.com/text2img。留空则用 AstrBot 配置或官方端点列表",
    },
    "auto_update_minutes": {
        "label": "自动更新间隔（分钟）",
        "group": "数据更新",
        "type": "int",
        "min": 0,
        "max": 1440,
        "default": 0,
        "hint": "填 0 表示关闭；修改后立即生效",
    },
    "unbound_cache_ttl_days": {
        "label": "未绑定玩家缓存保留（天）",
        "group": "数据更新",
        "type": "int",
        "min": 0,
        "max": 365,
        "default": 3,
        "hint": "只清理未绑定玩家的面板/角色卡缓存；绑定玩家永久保留。填 0 表示不清理",
    },
    "llm_context_ttl_minutes": {
        "label": "面板数据注入 AI 上下文的有效期（分钟）",
        "group": "AI 上下文",
        "type": "int",
        "min": 0,
        "max": 1440,
        "default": 30,
        "hint": "查完面板后这段时间内继续聊天，插件会把数据交给 AI 参考",
    },
    "analysis_prompt": {
        "label": "练度分析说明词",
        "group": "AI 上下文",
        "type": "textarea",
        "default": DEFAULT_ANALYSIS_PROMPT,
        "hint": "查完面板后随数据一起交给 AI，决定它怎么分析角色练度；留空则不注入",
    },
}


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


def _fmt_ts(ts: Any) -> str:
    """时间戳 -> 2026-11-19 20:30。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "未知时间"


def _trace_sync_note(result: Any) -> str:
    """把行迹倾向的同步结果转成一行补充说明，供 /同步资料 使用。"""
    if not isinstance(result, dict) or not result:
        return ""
    if not result.get("ok"):
        return f"\n（行迹倾向同步失败：{result.get('error') or '未知原因'}）"
    count = result.get("count") or 0
    if result.get("skipped"):
        return f"\n行迹倾向已是最新（{count} 个角色）。"
    return f"\n行迹倾向已更新（{count} 个角色）。"


def _lib_version() -> str:
    """starrail_damage_cal 的版本号，自检用。"""
    try:
        from importlib.metadata import version
    except ImportError:
        return ""
    for name in ("starrail-damage-cal", "starrail_damage_cal"):
        try:
            return version(name)
        except Exception:
            continue
    return ""


class StarRailPanelPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._data_dir = self._resolve_data_dir()
        self._auto_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._precache_task: asyncio.Task | None = None
        # UID -> 后台刷新任务，避免同一个玩家被重复刷新
        self._refresh_tasks: Dict[str, asyncio.Task] = {}
        # 未绑定玩家缓存清理任务
        self._maintenance_task: asyncio.Task | None = None
        # 预渲染排队：批量刷新时逐个来，避免同时压垮渲染服务
        self._precache_queue: List[tuple[str, Dict[str, Any]]] = []
        # 正在为聊天渲染的数量：预渲染看到它就停下来让路
        self._chat_rendering = 0
        # (会话|发送者) -> 最近一次查询的 UID，供 /选择查询 使用
        self._last_uid: Dict[str, str] = {}
        # (会话|发送者) -> 最近一次看过的角色名，注入遗器明细时用
        self._last_char: Dict[str, str] = {}
        # 渲染失败后进入冷却期，冷却结束会重新尝试出图
        self._card_fail_until = 0.0
        # 当前环境根本没有 html_render 时永久关闭
        self._card_disabled = False
        # 最近一次数据更新的结果，给控制面板看
        self._last_update_at = 0.0
        self._last_update_msg = ""
        # 机制卡同步：用上次成功时间做 TTL 基准，重启后不会立刻重拉
        self._guide_sync_at = read_synced_at()
        self._guide_sync_task: asyncio.Task | None = None
        self._guide_sync_last: Dict[str, Any] = {}
        self._guide_syncing = False
        # 同步失败后 10 分钟内不再重试，避免每次提问都白等
        self._guide_sync_failed_at = 0.0
        # 行迹倾向：权重表没收录的角色用它兜底，跟机制卡同一个同步节奏
        self._trace_sync_at = read_trace_synced_at()
        self._trace_sync_last: Dict[str, Any] = {}
        self._trace_sync_failed_at = 0.0
        logger.info(f"[星铁面板] 已加载，数据目录：{self._data_dir}")

        minutes = int(self.config.get("auto_update_minutes", 0) or 0)
        if minutes > 0:
            self._auto_task = asyncio.create_task(self._auto_update_loop(minutes))
            logger.info(f"[星铁面板] 自动更新已开启，间隔 {minutes} 分钟")

        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        if self._guide_sync_enabled():
            self._guide_sync_task = asyncio.create_task(self._first_guide_sync())

        self._register_web_apis()

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
        set_card_cache_dir(data_dir / "cards")
        # 用户可改的机制卡放 plugin_data 下，升级插件不会覆盖
        set_guide_dir(data_dir / "guides")
        return data_dir

    def _session_key(self, event: AstrMessageEvent) -> str:
        """会话标识。同一个群/私聊算一个会话。"""
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return str(umo)
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    def _resolve_uid(self, event: AstrMessageEvent, uid: str) -> str:
        """确定要查的 UID：填了就用填的，没填就用绑定的。

        真实 UID 都是 9 位数，少于 5 位的纯数字当作「角色序号」而不是 UID。
        """
        uid = (uid or "").strip()
        if uid.isdigit() and len(uid) >= 5:
            return uid
        return get_bound_uid(str(event.get_sender_id())) or ""

    def _user_key(self, event: AstrMessageEvent) -> str:
        """会话 + 发送者，用来记录「这个人最近查的是谁」。

        不能用会话级记录：群里 A 查了别人的面板，B 再发 /选择查询 不能串到 A 的目标。
        """
        return f"{self._session_key(event)}|{event.get_sender_id()}"

    # ------------------------------------------------------------------
    # 角色卡（图片输出）
    # ------------------------------------------------------------------

    def _card_enabled(self) -> bool:
        """当前是否应该尝试渲染角色卡。

        mode:
            auto  —— 能渲染就发图，失败回退文本（默认）
            image —— 同上，但会在日志里明确记录失败
            text  —— 只用文本
        """
        mode = str(self.config.get("panel_render_mode", "auto") or "auto").lower()
        if mode == "text":
            return False
        if self._card_disabled:
            return False
        if time.time() < self._card_fail_until:
            return False
        if not callable(getattr(self, "html_render", None)):
            self._card_disabled = True
            if mode == "image":
                logger.warning(
                    "[星铁面板] 当前 AstrBot 不支持 html_render，角色卡不可用，已回退文本"
                )
            return False
        return True

    def _card_skip_reason(self) -> str:
        """不出图时给日志一个明确的原因，方便排查。"""
        mode = str(self.config.get("panel_render_mode", "auto") or "auto").lower()
        if mode == "text":
            return "输出方式设置为 text"
        if self._card_disabled:
            return "当前环境没有 html_render"
        remaining = self._card_fail_until - time.time()
        if remaining > 0:
            return f"渲染冷却中（还剩 {remaining / 60:.0f} 分钟）"
        return ""

    def _card_timeout(self) -> float:
        """角色卡渲染超时（秒），限制在 5~120 之间。"""
        try:
            value = float(self.config.get("card_render_timeout", 30) or 30)
        except (TypeError, ValueError):
            return 30.0
        return max(5.0, min(120.0, value))

    def _inline_images_enabled(self) -> bool:
        value = self.config.get("card_inline_images", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _rotation_enabled(self) -> bool:
        value = self.config.get("card_endpoint_rotation", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _preferred_endpoint(self) -> str:
        """插件里填的端点 > AstrBot 配置里的 t2i_endpoint > 官方列表。"""
        value = str(self.config.get("card_render_endpoint", "") or "").strip()
        if value:
            return value
        try:
            cfg = self.context.get_config()
            if cfg:
                return str(cfg.get("t2i_endpoint") or "").strip()
        except Exception:
            pass
        return ""

    def _card_cooldown(self) -> float:
        """失败后的冷却时间（分钟），限制在 0~120 之间。"""
        try:
            value = float(self.config.get("card_fail_cooldown_minutes", 5) or 0)
        except (TypeError, ValueError):
            return 5.0
        return max(0.0, min(120.0, value))

    # ------------------------------------------------------------------
    # 角色机制卡（本地资料，供 AI 查询）
    # ------------------------------------------------------------------

    def _guide_enabled(self) -> bool:
        value = self.config.get("guide_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _guide_max_chars(self) -> int:
        try:
            value = int(float(self.config.get("guide_tool_max_chars", 1200) or 1200))
        except (TypeError, ValueError):
            return 1200
        return max(200, min(4000, value))

    def _guide_sync_enabled(self) -> bool:
        value = self.config.get("guide_auto_sync", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _guide_sync_hours(self) -> float:
        try:
            value = float(self.config.get("guide_sync_hours", 6) or 6)
        except (TypeError, ValueError):
            return 6.0
        return max(1.0, min(168.0, value))

    def _guide_prefetch(self) -> bool:
        """检测到角色名时，是否在请求前直接把机制卡塞进上下文。"""
        value = self.config.get("guide_prefetch", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _guide_block_search(self) -> bool:
        """预注入资料时，是否本轮临时摘掉联网搜索工具。"""
        value = self.config.get("guide_block_search", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _drop_search_tools(self, req) -> int:
        """临时移除本轮请求里的联网搜索工具。

        人格提示词常写「不确定必须先联网」，模型于是先搜再答，本地资料被跳过。
        这里只在检测到星铁角色名时动手，且只影响这一轮。
        AstrBot 内部结构变化时静默跳过，不影响正常聊天。
        """
        try:
            tool_set = getattr(req, "func_tool", None)
            tools = getattr(tool_set, "tools", None)
            if not isinstance(tools, list):
                return 0
            kept = []
            removed = 0
            for tool in tools:
                name = str(getattr(tool, "name", "") or "").lower()
                if "search" in name or "tavily" in name or "bing" in name:
                    removed += 1
                    continue
                kept.append(tool)
            if not removed:
                return 0
            tool_set.tools = kept
            logger.debug(f"[星铁面板] 本轮临时禁用 {removed} 个联网搜索工具")
            return removed
        except Exception:
            logger.debug("[星铁面板] 摘除搜索工具失败，跳过", exc_info=True)
            return 0

    def _guide_sync_due(self) -> bool:
        now = time.time()
        if now - self._guide_sync_failed_at < 600:
            return False
        return now - self._guide_sync_at >= self._guide_sync_hours() * 3600

    async def _run_guide_sync(self, force: bool = False) -> Dict[str, Any]:
        """执行一次同步。任何异常都收敛成结果字典。"""
        if self._guide_syncing:
            return {"ok": False, "error": "正在同步中", "written": 0}
        self._guide_syncing = True
        try:
            result = await sync_guides(force=force)
        except Exception as exc:
            logger.exception("[星铁面板] 同步机制卡出错")
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "written": 0,
            }
        finally:
            self._guide_syncing = False

        self._guide_sync_last = result or {}
        if self._guide_sync_last.get("ok"):
            self._guide_sync_at = time.time()
        else:
            self._guide_sync_failed_at = time.time()
        # 行迹倾向顺带同步，失败不影响机制卡的结果
        if self._guide_sync_last.get("ok") or not count_traces():
            try:
                self._guide_sync_last["traces"] = await self._run_trace_sync(force=force)
            except Exception:
                logger.debug("[星铁面板] 同步行迹倾向出错", exc_info=True)
        return self._guide_sync_last

    def _trace_sync_due(self) -> bool:
        now = time.time()
        if now - self._trace_sync_failed_at < 600:
            return False
        return now - self._trace_sync_at >= self._guide_sync_hours() * 3600

    async def _run_trace_sync(self, force: bool = False) -> Dict[str, Any]:
        """同步角色行迹倾向。任何异常都收敛成结果字典。"""
        if not force and not self._trace_sync_due():
            return {"ok": True, "skipped": True, "count": count_traces()}
        try:
            result = await sync_traces(force=force)
        except Exception as exc:
            logger.warning(
                f"[星铁面板] 同步行迹倾向出错：{type(exc).__name__}: {exc}"
            )
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
            }
        self._trace_sync_last = result or {}
        if self._trace_sync_last.get("ok"):
            self._trace_sync_at = time.time()
        else:
            self._trace_sync_failed_at = time.time()
            logger.debug(
                f"[星铁面板] 行迹倾向同步失败：{self._trace_sync_last.get('error')}"
            )
        return self._trace_sync_last

    async def _maybe_guide_sync(self, wait: float = 8.0) -> None:
        """资料缺失时顺带同步一次；超过 wait 秒就转后台，不拖住回答。"""
        if not self._guide_sync_enabled() or not self._guide_sync_due():
            return
        task = asyncio.create_task(self._run_guide_sync())
        self._guide_sync_task = task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except asyncio.TimeoutError:
            logger.info(f"[星铁面板] 机制卡同步超过 {wait:.0f} 秒，转后台继续")
        except Exception:
            pass

    async def _first_guide_sync(self) -> None:
        """启动后延迟几秒跑一次，别和插件加载抢网络。"""
        await asyncio.sleep(5)
        if not self._guide_sync_enabled():
            return
        # 上次同步还在有效期内且有文件，就不用再拉
        if not self._guide_sync_due() and count_auto_guides():
            # 机制卡不用重拉，但行迹倾向可能是这次升级才加的，缺就补一次
            if not count_traces():
                await self._run_trace_sync()
            return
        await self._run_guide_sync()

    @filter.llm_tool(name="query_character_guide")
    async def query_character_guide(self, event: AstrMessageEvent, character: str):
        """查询《崩坏：星穹铁道》角色的技能机制、行迹、星魂与配装方向。

        只要用户提到任何星铁角色的技能、行迹、星魂、光锥、遗器套装或
        「该怎么练」，都必须先调用本工具。本地有资料就以它为准，
        不要改用联网搜索；本工具明确返回「没有资料」时再去查别的。

        Args:
            character(string): 角色中文名，例如「黄泉」「流萤」
        """
        if not self._guide_enabled():
            return "本地机制卡功能已关闭。请明确告知用户你没有该角色的机制资料，不要凭记忆描述。"

        text = get_guide(character)
        if not text:
            # 本地没有可能是还没同步过，顺带拉一次（最多等几秒）
            await self._maybe_guide_sync()
            text = get_guide(character)
        if not text:
            return (
                f"本地资料中没有「{character}」的机制卡。"
                "请明确告知用户你没有该角色的机制资料，"
                "不要凭记忆或常识描述其技能、行迹、星魂效果。"
            )
        return clip_guide(text, self._guide_max_chars())

    @filter.command("角色资料", alias={"查资料", "机制"})
    async def show_guide(self, event: AstrMessageEvent, character: str = ""):
        """查看本地机制卡收录的内容。

        Args:
            character(string): 角色名；不填则列出已收录的角色
        """
        name = (character or "").strip()
        if not name:
            names = list_guide_names()
            if not names:
                yield event.plain_result(
                    "本地还没有任何角色机制卡。\n"
                    "把 md 文件放进插件数据目录的 guides/ 里即可。"
                )
                return
            auto = count_auto_guides()
            status = ""
            if auto:
                synced_at = read_synced_at()
                status = (
                    f"\n（其中 {auto} 个来自自动同步，"
                    f"上次同步 {_fmt_ts(synced_at) if synced_at else '未知'}）"
                )
            for chunk in _split_text(
                f"已收录 {len(names)} 个角色的机制卡：\n"
                + "、".join(names)
                + status
                + "\n\n用 /角色资料 <角色名> 查看具体内容。"
            ):
                yield event.plain_result(chunk)
            return

        text = get_guide(name)
        if not text:
            yield event.plain_result(
                f"本地没有「{name}」的机制卡。\n"
                "用 /角色资料 查看已收录的角色。"
            )
            return
        for chunk in _split_text(text):
            yield event.plain_result(chunk)

    @filter.command("重载资料", alias={"重载机制卡"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reload_guides(self, event: AstrMessageEvent):
        """改完 guides/ 里的 md 文件后重新读取，不用重启 AstrBot。"""
        reload_guide_files(force=True)
        names = list_guide_names()
        yield event.plain_result(
            f"已重新读取机制卡，共 {len(names)} 个角色。"
            if names
            else "已重新读取，但 guides/ 里没有找到任何 md 文件。"
        )

    @filter.command("同步资料", alias={"同步机制卡", "更新机制卡"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def sync_guides_command(self, event: AstrMessageEvent):
        """管理员：立刻从上游同步角色机制卡与行迹倾向，不用等自动检查。"""
        yield event.plain_result("开始同步机制卡，最多需要几十秒…")
        result = await self._run_guide_sync(force=True)
        if not result.get("ok"):
            yield event.plain_result(
                f"同步失败：{result.get('error') or '未知原因'}\n"
                "如果是一直失败，多半是服务器访问不了 GitHub / jsDelivr。"
                + _trace_sync_note(result.get("traces"))
            )
            return
        version = str(result.get("version") or "")[:8] or "未知"
        if result.get("skipped"):
            yield event.plain_result(
                f"资料已是最新（版本 {version}），无需更新。"
                + _trace_sync_note(result.get("traces"))
            )
            return
        yield event.plain_result(
            f"同步完成：{result.get('written')} 个角色，"
            f"版本 {version}，耗时 {result.get('elapsed')} 秒。"
            + _trace_sync_note(result.get("traces"))
            + "\n用 /角色资料 <角色名> 可以直接查看。"
        )

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
        raw_uid = (uid or "").strip()
        uid = self._resolve_uid(event, raw_uid)

        if not uid:
            yield event.plain_result(
                "用法：/面板 <UID>\n"
                "例如：/面板 100000000\n"
                "也可以先绑定：/绑定 100000000，之后直接发 /面板"
            )
            return

        extra = (extra or "").strip()
        # /面板 1 —— 只发了个短数字，当成角色序号
        if not extra and raw_uid.isdigit() and len(raw_uid) < 5:
            extra = raw_uid

        force_refresh = extra.lower() in REFRESH_WORDS
        if force_refresh:
            # 统一走后台刷新：数据和角色卡一起更新，立即返回，不阻塞聊天
            started = self._start_refresh(uid)
            if started:
                yield event.plain_result(
                    f"已开始后台刷新 UID {uid} 的数据与角色卡。\n"
                    f"完成后发 /面板 {uid} 查看即可。"
                )
            else:
                yield event.plain_result(f"UID {uid} 正在刷新中，请稍候再查。")
            return

        # 本地优先：数据库里已经有这个 UID 的面板就直接用，只有「刷新」才联网。
        # 这样玩家查询不会被公共接口的排队/解析库滞后卡住。
        result = None
        fetched = False
        if not force_refresh:
            result = self._local_panel(uid)
            if result is not None:
                logger.debug(f"[星铁面板] UID {uid} 使用本地存档")

        if result is None:
            try:
                result = await asyncio.wait_for(
                    query_player(uid, force_refresh=force_refresh),
                    timeout=QUERY_TIMEOUT,
                )
                fetched = True
            except asyncio.TimeoutError:
                result = self._local_panel(uid)
                if result is None:
                    yield event.plain_result(
                        f"查询超时了（超过 {QUERY_TIMEOUT} 秒）。\n"
                        "公共数据接口有时会排队，稍后再试一次吧。"
                    )
                    return
                result["_notice"] = (
                    f"刷新超时（超过 {QUERY_TIMEOUT} 秒），先给你看上次保存的数据"
                )
            except PlayerQueryError as exc:
                result = self._local_panel(uid)
                if result is None:
                    yield event.plain_result(f"查询失败：{exc}")
                    return
                result["_notice"] = f"刷新失败（{exc}），先给你看上次保存的数据"
            except Exception as exc:
                logger.exception(f"[星铁面板] 查询 UID {uid} 时出错")
                result = self._local_panel(uid)
                if result is None:
                    yield event.plain_result(f"查询出错了：{type(exc).__name__}: {exc}")
                    return
                result["_notice"] = f"刷新出错（{type(exc).__name__}），先给你看上次保存的数据"

        if not result.get("ok"):
            yield event.plain_result(
                "没有查到任何角色。\n"
                "可能原因：对方没有公开角色展示柜，或者 UID 填错了。"
            )
            return

        # 记住这个人这次查的是谁，/选择查询 接着用
        if len(self._last_uid) > 500:
            self._last_uid.clear()
        self._last_uid[self._user_key(event)] = uid

        if fetched:
            # 刚拉下来的新数据：入库 + 后台预渲染角色卡
            result["updated_at"] = time.time()

        # 本地存档也要写一份「最近查询」，否则本地优先之后 AI 拿不到上下文。
        # 必须按「会话 + 发送者」记：群里 A 查的面板不能被注入到 B 的提问里。
        self._remember(
            self._user_key(event), uid, result, save_archive=fetched
        )
        if fetched:
            self._schedule_precache(uid, result)

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

        # 记下这个人刚看的是哪个角色，后续注入遗器明细用
        if len(self._last_char) > 500:
            self._last_char.clear()
        self._last_char[self._user_key(event)] = str(panel.get("name") or "")

        # 本地已有与当前数据匹配的图片：直接发，不受渲染冷却影响
        cached_image = card_image_path(uid, panel)
        try:
            fresh_image = cached_image.exists() and (
                not result.get("updated_at")
                or cached_image.stat().st_mtime
                >= float(result["updated_at"]) - 2
            )
        except (OSError, TypeError, ValueError):
            fresh_image = False
        if fresh_image:
            logger.debug(f"[星铁面板] 发送本地缓存角色卡：{cached_image}")
            yield event.image_result(str(cached_image))
            return

        # 单角色详情：优先用本地缓存的图片，没有再走 html_render
        skip_reason = ""
        if self._card_enabled():
            self._chat_rendering += 1
            try:
                image = await render_character_card(
                    self,
                    panel,
                    uid,
                    result.get("nickname", ""),
                    timeout=self._card_timeout(),
                    fresh_after=float(result.get("updated_at") or 0),
                    inline_images=self._inline_images_enabled(),
                    use_rotation=self._rotation_enabled(),
                    preferred_endpoint=self._preferred_endpoint(),
                )
            finally:
                self._chat_rendering -= 1
            if image:
                logger.debug(f"[星铁面板] 发送角色卡：{panel.get('name')} -> {image}")
                yield event.image_result(image)
                return

            skip_reason = (
                get_render_status().get("last_error") or "渲染返回空，原因未知"
            )
            minutes = self._card_cooldown()
            if minutes > 0:
                self._card_fail_until = time.time() + minutes * 60
                logger.warning(
                    f"[星铁面板] 角色卡渲染失败（{skip_reason}），"
                    f"{minutes:.0f} 分钟内改用文本输出"
                )
        else:
            skip_reason = self._card_skip_reason()

        if skip_reason:
            logger.warning(
                f"[星铁面板] 角色卡未发送（{panel.get('name')}），改用文本：{skip_reason}"
            )

        text = format_character_panel(panel)
        if result.get("updated_at"):
            text += (
                f"\n  数据更新于 {_fmt_ts(result['updated_at'])}"
                f"（/刷新 {uid} 可后台更新）"
            )
        for chunk in _split_text(text):
            yield event.plain_result(chunk)

    @filter.command("选择查询", alias={"选角色", "看角色", "查询角色"})
    async def select_character(self, event: AstrMessageEvent, target: str = ""):
        """查看自己绑定账号的角色面板，不需要再输入 UID。

        Args:
            target(string): 角色名或序号；不填则列出所有角色
        """
        # 优先用这个人最近查过的 UID：查完别人的面板后，/选择查询 也应该查那个人
        uid = self._last_uid.get(self._user_key(event)) or self._resolve_uid(event, "")
        if not uid:
            yield event.plain_result(
                "你还没有绑定账号。\n"
                "先发 /绑定 <UID> 绑定，或者用 /面板 <UID> 直接查。"
            )
            return

        async for chunk in self.query_panel(event, uid, (target or "").strip()):
            yield chunk

    @filter.command("刷新", alias={"刷新面板", "更新面板"})
    async def refresh_command(self, event: AstrMessageEvent, uid: str = ""):
        """后台刷新某个玩家的数据与角色卡。

        Args:
            uid(string): 可选。不填则刷新「你最近查看的玩家」，没查过就用绑定账号
        """
        target = (uid or "").strip()
        if target and not (target.isdigit() and len(target) >= 5):
            yield event.plain_result(
                "用法：/刷新 [UID]\n"
                "例如：/刷新 100000000\n"
                "不带 UID 时，刷新你最近查看的玩家；没查过则刷新绑定账号。"
            )
            return

        if not target:
            target = (
                self._last_uid.get(self._user_key(event))
                or get_bound_uid(str(event.get_sender_id()))
                or ""
            )

        if not target:
            yield event.plain_result(
                "你还没查过任何玩家，也没有绑定账号。\n"
                "用法：/刷新 <UID>"
            )
            return

        if self._start_refresh(target):
            yield event.plain_result(
                f"已开始后台刷新 UID {target} 的数据与角色卡。\n"
                "刷新完成后直接用 /面板 查询即可（会自动使用新数据）。"
            )
        else:
            yield event.plain_result(f"UID {target} 正在刷新中，请稍候。")

    def _local_panel(self, uid: str) -> Dict[str, Any] | None:
        """读本地存档。没有或为空返回 None。"""
        try:
            data = get_panel(uid)
        except Exception:
            logger.exception(f"[星铁面板] 读取本地存档失败（UID {uid}）")
            return None
        if not data or not data.get("characters"):
            return None
        return data

    def _remember(
        self,
        session_key: str,
        uid: str,
        result: dict,
        save_archive: bool = True,
    ) -> None:
        """把这次查询存下来：会话上下文 + 完整存档。

        save_archive=False 只刷新「最近查询」（给 AI 上下文用），不动面板存档，
        用于本地优先的日常查询。
        """
        try:
            summary = build_llm_summary(result)
            save_last_query(session_key, uid, result.get("nickname", ""), summary)
            if save_archive and result.get("ok"):
                save_panel(uid, result.get("nickname", ""), result)
                self._last_update_at = float(result.get("updated_at") or time.time())
                self._last_update_msg = (
                    f"UID {uid} 更新成功，共 {len(result.get('characters') or [])} 个角色"
                )
        except Exception:
            logger.exception("[星铁面板] 保存查询记录失败")

    # ------------------------------------------------------------------
    # 后台刷新
    # ------------------------------------------------------------------

    def _start_refresh(self, uid: str) -> bool:
        """启动一个后台刷新任务。已经在刷新的 UID 不会重复启动。"""
        task = self._refresh_tasks.get(uid)
        if task and not task.done():
            return False

        task = asyncio.create_task(self._refresh_uid(uid))
        self._refresh_tasks[uid] = task

        def _cleanup(finished: asyncio.Task) -> None:
            if self._refresh_tasks.get(uid) is finished:
                self._refresh_tasks.pop(uid, None)

        task.add_done_callback(_cleanup)
        return True

    async def _refresh_uid(self, uid: str) -> None:
        """后台刷新单个玩家：拉数据 → 存档 → 预渲染角色卡。失败保留旧数据。"""
        try:
            result = await asyncio.wait_for(
                query_player(uid, force_refresh=True),
                timeout=QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[星铁面板] 刷新 UID {uid} 超时，保留旧数据")
            return
        except PlayerQueryError as exc:
            logger.warning(f"[星铁面板] 刷新 UID {uid} 失败：{exc}，保留旧数据")
            return
        except Exception:
            logger.exception(f"[星铁面板] 刷新 UID {uid} 出错，保留旧数据")
            return

        if not result.get("ok"):
            logger.warning(f"[星铁面板] 刷新 UID {uid} 没有返回角色，保留旧数据")
            return

        result["updated_at"] = time.time()
        try:
            save_panel(uid, result.get("nickname", ""), result)
        except Exception:
            logger.exception(f"[星铁面板] 保存 UID {uid} 的面板失败")

        self._last_update_at = result["updated_at"]
        self._last_update_msg = (
            f"UID {uid} 刷新成功，共 {len(result.get('characters') or [])} 个角色"
        )
        logger.info(f"[星铁面板] {self._last_update_msg}")
        self._schedule_precache(uid, result)

    # ------------------------------------------------------------------
    # 角色卡本地预渲染
    # ------------------------------------------------------------------

    def _precache_enabled(self) -> bool:
        value = self.config.get("precache_cards", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)

    def _schedule_precache(self, uid: str, result: Dict[str, Any]) -> None:
        """刷新成功后，在后台把角色卡渲染到本地，下次查询直接发本地图。"""
        if not self._precache_enabled():
            return
        if not callable(getattr(self, "html_render", None)):
            return
        # 同一个 UID 只保留最新一份，避免队列里堆旧数据
        self._precache_queue = [
            item for item in self._precache_queue if item[0] != uid
        ]
        self._precache_queue.append((uid, result))
        self._precache_queue = self._precache_queue[-20:]

        if not self._precache_task or self._precache_task.done():
            self._precache_task = asyncio.create_task(self._precache_worker())

    async def _precache_worker(self) -> None:
        """串行消费预渲染队列。"""
        # 延迟启动：刚刷新完用户往往会立刻查角色，别和聊天抢渲染服务
        await asyncio.sleep(15)
        while self._precache_queue:
            uid, result = self._precache_queue.pop(0)
            try:
                await self._precache_cards(uid, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[星铁面板] 预渲染 UID {uid} 时出错")

    async def _precache_cards(self, uid: str, result: Dict[str, Any]) -> None:
        """最多预渲染 12 个角色；连续失败 3 次就停，避免拖垮渲染服务。"""
        chars = list(result.get("characters") or [])[:12]
        nickname = result.get("nickname", "")
        fresh_after = float(result.get("updated_at") or 0)
        # 预渲染是后台任务：超时更短、不在超时后重试，尽快把渲染资源让给聊天
        timeout = min(self._card_timeout(), 20.0)
        ok = 0
        failed = 0
        skipped = 0

        for panel in chars:
            if not self._card_enabled():
                break
            # 聊天正在渲染时先让路，避免和用户查询抢渲染端点
            while self._chat_rendering > 0:
                await asyncio.sleep(2)
            # 聊天里已经渲染过、且比数据和模板都新的角色不用再渲染
            image_path = card_image_path(uid, panel)
            try:
                template_mtime = TEMPLATE_FILE.stat().st_mtime
            except OSError:
                template_mtime = 0.0
            try:
                reusable = (
                    image_path.exists()
                    and image_path.stat().st_mtime
                    >= max(fresh_after, template_mtime) - 2
                )
            except OSError:
                reusable = False
            if reusable:
                skipped += 1
                continue

            image = await render_character_card(
                self,
                panel,
                uid,
                nickname,
                timeout=timeout,
                fresh_after=fresh_after,
                save_html=True,
                inline_images=self._inline_images_enabled(),
                retry_on_timeout=False,
                use_rotation=self._rotation_enabled(),
                preferred_endpoint=self._preferred_endpoint(),
            )
            if image:
                ok += 1
                failed = 0
            else:
                failed += 1
                if failed >= 3:
                    # 只停止本次预渲染，不进入全局冷却：
                    # 聊天渲染失败会自己进入冷却，后台任务不该影响用户出图
                    logger.warning(
                        "[星铁面板] 预渲染连续失败 3 次，暂停本次预渲染（不影响聊天出图）"
                    )
                    break
            await asyncio.sleep(1)

        logger.info(
            f"[星铁面板] UID {uid} 角色卡预渲染完成："
            f"成功 {ok}，跳过已缓存 {skipped}，失败 {failed}"
        )

    # ------------------------------------------------------------------
    # 未绑定玩家缓存清理
    # ------------------------------------------------------------------

    def _unbound_ttl_days(self) -> int:
        try:
            return int(self.config.get("unbound_cache_ttl_days", 3) or 0)
        except (TypeError, ValueError):
            return 3

    async def _maintenance_loop(self) -> None:
        """定期清理未绑定玩家的过期缓存，绑定玩家永远保留。"""
        while True:
            try:
                await self._cleanup_unbound_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[星铁面板] 清理未绑定玩家缓存失败")
            await asyncio.sleep(6 * 3600)

    async def _cleanup_unbound_cache(self) -> None:
        days = self._unbound_ttl_days()
        if days <= 0:
            return

        bound = {row["uid"] for row in list_bindings(1000)}
        removed = cleanup_unbound_panels(days * 86400, bound)
        if not removed:
            return

        for uid in removed:
            shutil.rmtree(self._data_dir / "cards" / uid, ignore_errors=True)
            shutil.rmtree(self._data_dir / "cache" / uid, ignore_errors=True)

        logger.info(
            f"[星铁面板] 已清理 {len(removed)} 个未绑定玩家超过 {days} 天的缓存："
            f"{'、'.join(removed[:10])}"
        )

    def _restart_maintenance_task(self) -> None:
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    # ------------------------------------------------------------------
    # 让 AI 能看见这些数据
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_panel_context(self, event: AstrMessageEvent, req):
        """在请求大模型前，把用户刚查过的面板数据作为临时用户内容附上。

        使用 extra_user_content_parts + mark_as_temp，只参与本轮请求、
        不写回会话历史，也不改 system_prompt（避免破坏 prompt 缓存）。
        """
        try:
            # 角色机制卡预注入：和面板数据无关，没查过面板也要生效。
            # AstrBot 的 on_llm_request 只保留一个处理器，所以必须放在同一个钩子里。
            if self._guide_enabled() and self._guide_prefetch():
                self._prefetch_guide(event, req)

            ttl = int(self.config.get("llm_context_ttl_minutes", 0) or 0) * 60
            if ttl <= 0:
                ttl = DEFAULT_CONTEXT_TTL
            # 按「会话 + 发送者」取：同群其他人的查询不会串到这里
            record = get_last_query(self._user_key(event), max_age=ttl)
            if not record or not record.get("summary"):
                return

            summary = record["summary"]
            # 刷新过但还没重新查询时，用最新的本地存档重建摘要，
            # 免得 AI 拿刷新前那份旧面板（旧遗器 / 旧光锥）回答
            try:
                fresh = get_panel(str(record.get("uid") or ""))
                if fresh and float(fresh.get("updated_at") or 0) > float(
                    record.get("updated_at") or 0
                ):
                    rebuilt = build_llm_summary(fresh)
                    if rebuilt:
                        summary = rebuilt
            except Exception:
                logger.debug("[星铁面板] 用最新存档重建摘要失败，沿用上次摘要")

            block = (
                "【用户刚刚查询的游戏面板数据】\n"
                "以下是用户通过 /面板 指令查到的《崩坏：星穹铁道》角色面板，"
                "数据来自游戏内公开的角色展示柜，是真实的。\n"
                "回答与配装、面板、遗器、光锥相关的问题时，请优先以此数据为依据，"
                "不要凭记忆编造数值，也不要建议用户重新发一遍数据。\n"
                "这份数据只属于摘要里那个 UID 对应的玩家，不代表同群/同会话的其他成员，"
                "不要把它当成别人的角色；摘要里也没有队伍（配队）信息，"
                "被问到配队时只能基于这些角色给建议，不要编造队伍成员或站位。\n"
                f"{summary}"
            )

            # 刚看过的那个角色：把六件遗器的完整词条一起给。
            # 摘要里为了控制长度只放了汇总，AI 会据此说「没有副词条」。
            last_name = self._last_char.get(self._user_key(event), "")
            if last_name:
                try:
                    last_panel = None
                    fresh_panel = get_panel(str(record.get("uid") or ""))
                    if fresh_panel:
                        last_panel = find_character(fresh_panel, last_name)
                    if last_panel is not None:
                        detail = format_relic_detail(last_panel)
                        if detail:
                            block += (
                                f"\n\n【{last_name} 的六件遗器明细】\n"
                                + detail
                                + "\n以上副词条已全部列出，用户问词条时直接引用，"
                                "不要再向他索要。"
                            )
                except Exception:
                    logger.debug("[星铁面板] 生成遗器明细失败", exc_info=True)

            # 只放「有哪些角色能查」，正文由工具按需返回，避免撑爆上下文
            if self._guide_enabled():
                names = list_guide_names()
                if names:
                    shown = names[:40]
                    more = "" if len(names) <= 40 else f" 等共 {len(names)} 个"
                    block += (
                        "\n\n【可查询机制资料的角色】\n"
                        + "、".join(shown)
                        + more
                        + "\n涉及这些角色的技能机制、行迹、星魂、光锥或遗器套装效果时，"
                        "先调用 query_character_guide 工具取得资料再回答，"
                        "不要用联网搜索代替它；"
                        "工具说没有资料的角色，直接告诉用户你没有该角色的机制资料。"
                    )

            block += (
                "\n\n【机制表述纪律】\n"
                "- 面板数值一律以上面提供的数据为准，不要重新估算。\n"
                "- 每个角色的「有效词条」来自属性权重表，代表这个角色实际吃哪些属性。"
                "判断该堆什么时只依据它：不在有效词条里的属性，不要建议用户堆。\n"
                "- 权重表没收录的角色，数据里会给出「行迹倾向」（该角色行迹自带的属性加成），"
                "按它判断倾向即可；没有行迹倾向时按该角色的定位和技能倍率说明，"
                "并明确告诉用户这是推断，不要编造权重表。\n"
                "- 面板里出现的数值（暴击率、击破特攻等）只用于判断是否达标，"
                "不代表该属性对这个角色有用。\n"
                "- 倍率、持续回合、触发条件这类具体数字，资料里没写就不要写，"
                "只描述定性效果并说明「具体数值以游戏内描述为准」。\n"
                "- 不要编造版本强度榜、T0 排名、主流抽取共识。\n"
                "- 回答前先自问：这条机制我是确实记得，还是在猜？在猜就删掉。\n"
            )

            guide = str(
                self.config.get("analysis_prompt", DEFAULT_ANALYSIS_PROMPT) or ""
            ).strip()
            # 只在用户确实在问面板/练度相关问题时才带上分析框架，
            # 免得闲聊也被强行套模板
            user_text = str(getattr(event, "message_str", "") or "")
            analysis_keywords = (
                "练度", "面板", "配装", "遗器", "光锥", "词条", "暴击", "爆伤",
                "速度", "击破", "效果命中", "治疗", "命座", "星魂", "强度",
                "培养", "值得", "分析", "评分", "毕业", "达标", "属性", "伤害",
                "角色", "提升", "优化", "够不够", "怎么样", "看看",
            )
            if guide and any(word in user_text for word in analysis_keywords):
                block += "\n\n【练度分析要求】\n" + guide[:4000]

            # 不要改写 system_prompt：它是请求前缀，随查询变化会破坏 prompt 缓存。
            # 用 TextPart 追加到本轮用户消息，并标记为临时内容，不写入会话历史。
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None or TextPart is None:
                logger.debug(
                    "[星铁面板] 当前 AstrBot 不支持 extra_user_content_parts，"
                    "跳过面板上下文注入"
                )
                return

            part = TextPart(text=block)
            mark_temp = getattr(part, "mark_as_temp", None)
            if callable(mark_temp):
                part = mark_temp() or part
            parts.append(part)
        except Exception:
            # 注入失败绝不能影响正常聊天
            logger.exception("[星铁面板] 注入上下文失败")

    def _prefetch_guide(self, event: AstrMessageEvent, req) -> None:
        """用户直接提到已收录角色时，把机制卡提前放进本轮上下文。

        人格提示词里常有「不确定就先联网搜索」，模型于是习惯性先搜，
        本地资料反而被跳过。这里不等它调用工具，直接把资料摆上，
        让「不确定」的前提不成立；需要时再顺手摘掉本轮的搜索工具。
        """
        try:
            user_text = str(getattr(event, "message_str", "") or "")
            if not user_text.strip():
                return

            hits = match_guide_names(user_text, limit=2)
            if not hits:
                return

            texts: List[str] = []
            for name in hits:
                text = get_guide(name)
                if text:
                    texts.append(clip_guide(text, self._guide_max_chars()))
            if not texts:
                return

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None or TextPart is None:
                logger.info(
                    "[星铁面板] 无法预注入角色资料：当前 AstrBot 不支持 "
                    "extra_user_content_parts 或 TextPart"
                )
                return

            if self._guide_block_search():
                self._drop_search_tools(req)

            block = (
                "【本地角色资料（已提供，无需联网搜索）】\n"
                "以下内容取自游戏原文，可以直接引用。\n\n"
                + "\n\n".join(texts)
                + "\n\n用户问的就是这些角色的技能、行迹、星魂或配装方向时，"
                "直接依据以上资料回答；资料没覆盖的部分才说明不确定。"
                "不要为了这些角色再去联网搜索。"
            )

            part = TextPart(text=block)
            mark_temp = getattr(part, "mark_as_temp", None)
            if callable(mark_temp):
                part = mark_temp() or part
            parts.append(part)
            logger.info(f"[星铁面板] 已预注入角色资料：{'、'.join(hits)}")
        except Exception:
            logger.exception("[星铁面板] 预注入角色资料失败")

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

    @filter.command("刷新全部面板", alias={"更新全部面板"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def refresh_all_panels(self, event: AstrMessageEvent):
        """后台刷新所有本地存档玩家（仅管理员）。"""
        if self._refresh_task and not self._refresh_task.done():
            yield event.plain_result("已经在刷新中，请稍候")
            return

        self._refresh_task = asyncio.create_task(self._update_all_bound())
        yield event.plain_result(
            "已开始后台刷新所有已绑定玩家。\n"
            "完成后可以在控制面板「玩家数据」页查看结果。"
        )

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
        # 只刷新已绑定玩家；未绑定玩家由各自 /刷新 触发，缓存到期后自动清理
        uids = {row["uid"] for row in list_bindings(1000)}
        if not uids:
            logger.info("[星铁面板] 全部刷新：没有绑定记录，跳过")
            return

        ok = 0
        failed: List[str] = []
        for uid in uids:
            try:
                result = await query_player(uid, force_refresh=True)
                if result.get("ok"):
                    result["updated_at"] = time.time()
                    save_panel(uid, result.get("nickname", ""), result)
                    self._schedule_precache(uid, result)
                    ok += 1
                else:
                    failed.append(f"{uid}(无角色)")
            except Exception as exc:
                logger.warning(f"[星铁面板] 自动更新 UID {uid} 失败：{exc}")
                failed.append(f"{uid}({type(exc).__name__})")
            await asyncio.sleep(2)  # 别把公共接口打爆

        self._last_update_at = time.time()
        self._last_update_msg = (
            f"全部刷新完成：成功 {ok}/{len(uids)}"
            + (f"，失败：{'、'.join(failed[:5])}" if failed else "")
        )
        logger.info(f"[星铁面板] {self._last_update_msg}")

    # ------------------------------------------------------------------
    # 控制面板后端接口（供 views/admin 页面调用）
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        """注册控制面板用的接口。注册失败不影响聊天功能。"""
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[星铁面板] 当前 AstrBot 版本不支持插件视图 API，控制面板不可用"
            )
            return

        routes = [
            ("/bindings", self.api_list_bindings, ["GET"], "列出绑定关系"),
            ("/bindings/add", self.api_add_binding, ["POST"], "新增绑定"),
            ("/bindings/delete", self.api_delete_binding, ["POST"], "删除绑定"),
            ("/refresh", self.api_refresh_one, ["POST"], "刷新单个玩家面板"),
            ("/refresh_all", self.api_refresh_all, ["POST"], "刷新全部绑定玩家"),
            ("/panels", self.api_panels, ["GET"], "列出本地已存档玩家"),
            ("/panel", self.api_panel_detail, ["POST"], "读取某个玩家的面板数据"),
            ("/card", self.api_card, ["POST"], "生成/读取角色卡预览"),
            ("/settings", self.api_settings, ["GET"], "读取插件设置"),
            ("/settings/save", self.api_save_settings, ["POST"], "保存插件设置"),
            ("/diagnose", self.api_diagnose, ["GET"], "环境自检"),
            ("/render_test", self.api_render_test, ["POST"], "测试渲染端点"),
        ]

        registered = 0
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}{path}", handler, methods, desc
                )
                registered += 1
            except Exception:
                logger.exception(f"[星铁面板] 注册接口 {path} 失败")

        logger.info(f"[星铁面板] 控制面板接口已注册 {registered}/{len(routes)} 个")

    async def api_list_bindings(self):
        """GET 绑定列表，附带每个 UID 的面板更新时间。"""
        try:
            rows = list_bindings(500)
            panels = {p["uid"]: p for p in list_panels()}
        except Exception as exc:
            logger.exception("[星铁面板] 读取绑定列表失败")
            return error_response(f"读取数据库失败：{exc}", status_code=500)

        data = []
        for row in rows:
            panel = panels.get(row["uid"]) or {}
            data.append(
                {
                    "platform": row["platform"] or "",
                    "user_id": row["user_id"],
                    "uid": row["uid"],
                    "nickname": panel.get("nickname", ""),
                    "updated_at": panel.get("updated_at", 0),
                    "created_at": row["created_at"] or 0,
                }
            )

        return json_response({"bindings": data, "total": len(data)})

    async def api_add_binding(self):
        """POST 新增一条绑定。"""
        payload = await request.json(default={})
        user_id = str(payload.get("user_id", "")).strip()
        uid = str(payload.get("uid", "")).strip()
        platform = str(payload.get("platform", "")).strip()

        if not user_id.isdigit():
            return error_response("QQ 号必须是纯数字")
        if not uid.isdigit():
            return error_response("UID 必须是纯数字")

        try:
            bind_user(user_id, uid, platform)
        except Exception as exc:
            logger.exception("[星铁面板] 新增绑定失败")
            return error_response(f"保存失败：{exc}", status_code=500)

        return json_response({"ok": True, "user_id": user_id, "uid": uid})

    async def api_delete_binding(self):
        """POST 删除一条绑定。"""
        payload = await request.json(default={})
        user_id = str(payload.get("user_id", "")).strip()
        platform = str(payload.get("platform", "")).strip()

        if not user_id:
            return error_response("缺少 QQ 号")

        try:
            ok = unbind_user(user_id, platform)
        except Exception as exc:
            logger.exception("[星铁面板] 删除绑定失败")
            return error_response(f"删除失败：{exc}", status_code=500)

        return json_response({"ok": bool(ok)})

    async def api_refresh_one(self):
        """POST 立即刷新某个 UID 的面板。"""
        payload = await request.json(default={})
        uid = str(payload.get("uid", "")).strip()
        if not uid.isdigit():
            return error_response("UID 必须是纯数字")

        try:
            result = await asyncio.wait_for(
                query_player(uid, force_refresh=True),
                timeout=QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return error_response("查询超时，公共接口可能正在排队", status_code=504)
        except PlayerQueryError as exc:
            return error_response(str(exc))
        except Exception as exc:
            logger.exception(f"[星铁面板] 刷新 UID {uid} 失败")
            return error_response(f"{type(exc).__name__}: {exc}", status_code=500)

        if result.get("ok"):
            result["updated_at"] = time.time()
            save_panel(uid, result.get("nickname", ""), result)
            self._last_update_at = result["updated_at"]
            self._last_update_msg = (
                f"UID {uid} 手动刷新成功，共 {len(result.get('characters') or [])} 个角色"
            )
            self._schedule_precache(uid, result)

        return json_response(
            {
                "ok": bool(result.get("ok")),
                "nickname": result.get("nickname", ""),
                "characters": len(result.get("characters", [])),
                "updated_at": result.get("updated_at", 0),
            }
        )

    async def api_refresh_all(self):
        """POST 后台刷新全部绑定玩家（立即返回，不等结果）。"""
        if self._refresh_task and not self._refresh_task.done():
            return json_response({"started": True, "message": "已经在刷新中，请稍候"})

        self._refresh_task = asyncio.create_task(self._update_all_bound())
        return json_response({"started": True, "message": "已开始后台刷新"})

    # ------------------------------------------------------------------
    # 本地存档 / 角色卡预览
    # ------------------------------------------------------------------

    async def api_panels(self):
        """GET 本地已存档的玩家列表（含角色数量）。"""
        try:
            rows = list_panels()
        except Exception as exc:
            logger.exception("[星铁面板] 读取本地存档失败")
            return error_response(f"读取数据库失败：{exc}", status_code=500)

        data = []
        for row in rows:
            item = dict(row)
            try:
                panel = get_panel(row["uid"]) or {}
                item["characters"] = len(panel.get("characters") or [])
            except Exception:
                item["characters"] = 0
            data.append(item)

        return json_response(
            {
                "panels": data,
                "total": len(data),
                "last_update_at": self._last_update_at,
                "last_update_msg": self._last_update_msg,
            }
        )

    async def api_panel_detail(self):
        """POST {uid} 读取某个玩家的本地面板数据。"""
        payload = await request.json(default={})
        uid = str(payload.get("uid", "")).strip()
        if not uid.isdigit():
            return error_response("UID 必须是纯数字")

        result = self._local_panel(uid)
        if result is None:
            return error_response("这个 UID 还没有本地存档，点「刷新」拉取一次", status_code=404)

        return json_response(result)

    async def api_card(self):
        """POST {uid, name} 返回角色卡图片，渲染不出来才回退 HTML。"""
        payload = await request.json(default={})
        uid = str(payload.get("uid", "")).strip()
        target = str(payload.get("name", "")).strip()
        if not uid.isdigit():
            return error_response("UID 必须是纯数字")

        result = self._local_panel(uid)
        if result is None:
            return error_response("这个 UID 还没有本地存档，先刷新数据", status_code=404)

        panel = find_character(result, target)
        if panel is None:
            return error_response(f"没找到角色「{target}」", status_code=404)

        nickname = result.get("nickname", "")
        updated_at = float(result.get("updated_at") or 0)

        image = ""
        path = card_image_path(uid, panel)
        try:
            template_mtime = TEMPLATE_FILE.stat().st_mtime
        except OSError:
            template_mtime = 0.0

        def _image_fresh() -> bool:
            try:
                return path.exists() and path.stat().st_mtime >= max(
                    updated_at, template_mtime
                ) - 2
            except OSError:
                return False

        def _read_image() -> str:
            try:
                return "data:image/png;base64," + base64.b64encode(
                    path.read_bytes()
                ).decode()
            except OSError:
                return ""

        # 本地已经渲染过、且不比数据和模板旧的图片直接用
        if _image_fresh():
            image = _read_image()

        # 没有可用图片就当场渲染一张：面板里要的是能点开放大的图片，
        # 不能因为预渲染没跑过就退化成 iframe HTML。
        # 这里不受聊天渲染冷却限制——是用户主动点击的一次请求。
        mode = str(self.config.get("panel_render_mode", "auto") or "auto").lower()
        if not image and mode != "text" and callable(getattr(self, "html_render", None)):
            rendered = await render_character_card(
                self,
                panel,
                uid,
                nickname,
                timeout=min(self._card_timeout(), 60.0),
                fresh_after=updated_at,
                save_html=True,
                inline_images=self._inline_images_enabled(),
                retry_on_timeout=False,
                use_rotation=self._rotation_enabled(),
                preferred_endpoint=self._preferred_endpoint(),
            )
            if rendered:
                # 渲染成功一般会落到本地缓存；写盘失败时拿到的是远端 URL
                image = _read_image() or (
                    str(rendered) if str(rendered).startswith("http") else ""
                )

        # 已经拿到图片就不再生成 HTML：前端只在没有图片时才用 iframe，
        # 多跑一遍只会白白等一次模板渲染。
        html = ""
        if not image:
            try:
                html_path = card_html_path(uid, panel)
                if (
                    html_path.exists()
                    and html_path.stat().st_mtime
                    >= max(updated_at, template_mtime) - 2
                ):
                    html = html_path.read_text(encoding="utf-8")
                else:
                    saved = await save_card_html(panel, uid, nickname)
                    if saved and saved.exists():
                        html = saved.read_text(encoding="utf-8")
                    else:
                        html = await build_card_html(panel, uid, nickname)
            except Exception as exc:
                logger.warning(
                    f"[星铁面板] 生成角色卡预览失败：{type(exc).__name__}: {exc}"
                )

        return json_response(
            {
                "ok": True,
                "uid": uid,
                "name": panel.get("name", ""),
                "nickname": nickname,
                "updated_at": result.get("updated_at", 0),
                "image": image,
                "html": html,
                "text": format_character_panel(panel),
            }
        )

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def _setting_value(self, key: str) -> Any:
        spec = SETTING_SPECS[key]
        raw = self.config.get(key, spec["default"])
        if spec["type"] == "int":
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return int(spec["default"])
        if spec["type"] == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() not in {"0", "false", "no", "off", ""}
            return bool(raw)
        if spec["type"] in {"text", "textarea"}:
            return str(raw if raw is not None else spec["default"]).strip()
        text = str(raw or spec["default"]).strip()
        return text if text in spec.get("options", []) else spec["default"]

    def _save_plugin_config(self) -> bool:
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            logger.warning(
                "[星铁面板] 当前配置对象不支持保存，设置只在本次运行内生效"
            )
            return False
        try:
            saver()
            return True
        except Exception:
            logger.exception("[星铁面板] 保存插件配置失败")
            return False

    def _restart_auto_task(self) -> None:
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
        self._auto_task = None
        try:
            minutes = int(self.config.get("auto_update_minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes > 0:
            self._auto_task = asyncio.create_task(self._auto_update_loop(minutes))
            logger.info(f"[星铁面板] 自动更新已重启，间隔 {minutes} 分钟")
        else:
            logger.info("[星铁面板] 自动更新已关闭")

    async def api_settings(self):
        """GET 当前设置与可编辑规则。"""
        return json_response(
            {
                "values": {key: self._setting_value(key) for key in SETTING_SPECS},
                "specs": SETTING_SPECS,
                "persist_supported": callable(
                    getattr(self.config, "save_config", None)
                ),
            }
        )

    async def api_save_settings(self):
        """POST 保存设置，写回插件配置并立即生效。"""
        payload = await request.json(default={})
        values = payload.get("values")
        if not isinstance(values, dict):
            values = payload

        clean: Dict[str, Any] = {}
        errors: List[str] = []

        for key, spec in SETTING_SPECS.items():
            if key not in values:
                continue
            raw = values[key]
            if spec["type"] == "int":
                try:
                    num = int(float(str(raw).strip()))
                except (TypeError, ValueError):
                    errors.append(f"{spec['label']} 必须是数字")
                    continue
                if spec.get("min") is not None:
                    num = max(int(spec["min"]), num)
                if spec.get("max") is not None:
                    num = min(int(spec["max"]), num)
                clean[key] = num
            elif spec["type"] == "bool":
                if isinstance(raw, str):
                    clean[key] = raw.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    clean[key] = bool(raw)
            elif spec["type"] == "textarea":
                clean[key] = str(raw or "").strip()[:4000]
            elif spec["type"] == "text":
                text = str(raw or "").strip()
                if text and not text.startswith(("http://", "https://")):
                    errors.append(f"{spec['label']} 必须以 http:// 或 https:// 开头")
                    continue
                clean[key] = text
            else:
                text = str(raw).strip()
                if text not in spec.get("options", []):
                    errors.append(f"{spec['label']} 取值不合法")
                    continue
                clean[key] = text

        if errors:
            return error_response("；".join(errors))

        for key, value in clean.items():
            self.config[key] = value

        saved = self._save_plugin_config()
        if "auto_update_minutes" in clean:
            self._restart_auto_task()
        if "unbound_cache_ttl_days" in clean:
            self._restart_maintenance_task()

        return json_response(
            {
                "ok": True,
                "saved": saved,
                "values": {key: self._setting_value(key) for key in SETTING_SPECS},
            }
        )

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------

    async def api_diagnose(self):
        """GET 环境自检，用来判断角色卡渲染 / 刷新失败卡在哪一步。"""
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add(
            "插件 Web 接口",
            WEB_API_AVAILABLE,
            "可用" if WEB_API_AVAILABLE else "当前 AstrBot 版本不支持 register_web_api",
        )
        add(
            "数据解析库",
            LIB_AVAILABLE,
            "已安装" if LIB_AVAILABLE else "未安装 starrail_damage_cal",
        )

        try:
            from .relic_score import SCORE_AVAILABLE as relic_ok
        except ImportError:
            try:
                from relic_score import SCORE_AVAILABLE as relic_ok
            except Exception:
                relic_ok = False
        add("遗器评分表", bool(relic_ok), "可用" if relic_ok else "当前库版本没有权重表")

        try:
            trace_count = count_traces()
        except Exception:
            trace_count = 0
        trace_at = read_trace_synced_at() if trace_count else 0.0
        add(
            "行迹倾向数据",
            trace_count > 0,
            (
                f"{trace_count} 个角色（同步于 {_fmt_ts(trace_at)}）"
                if trace_count
                else "尚未同步，权重表没收录的角色只能按定位推断"
            ),
        )

        render_fn = getattr(self, "html_render", None)
        add(
            "html_render",
            callable(render_fn),
            "可用" if callable(render_fn) else "当前环境没有 html_render，角色卡只能出文字",
        )

        try:
            add(
                "角色卡模板",
                TEMPLATE_FILE.exists(),
                str(TEMPLATE_FILE),
            )
        except Exception as exc:
            add("角色卡模板", False, f"{type(exc).__name__}: {exc}")

        add(
            "图片下载组件",
            card_aiohttp is not None,
            "aiohttp 可用" if card_aiohttp else "缺少 aiohttp，卡片图片无法内嵌",
        )

        status = get_render_status()
        cooldown = max(0.0, self._card_fail_until - time.time())
        add(
            "角色卡状态",
            not self._card_disabled and cooldown <= 0,
            (
                "已永久关闭（没有 html_render）"
                if self._card_disabled
                else (
                    f"冷却中，剩余 {cooldown / 60:.0f} 分钟"
                    if cooldown > 0
                    else "正常"
                )
            ),
        )

        try:
            bindings = len(list_bindings(1000))
        except Exception:
            bindings = 0
        try:
            panels = list_panels()
        except Exception:
            panels = []

        info = {
            "library_version": _lib_version(),
            "render_mode": self._setting_value("panel_render_mode"),
            "endpoint_rotation": self._rotation_enabled(),
            "inline_images": self._inline_images_enabled(),
            "preferred_endpoint": self._preferred_endpoint() or "官方端点列表",
            "data_dir": str(self._data_dir),
            "db_path": str(self._data_dir / "starrail.db"),
            "cache_dir": str(self._data_dir / "cache"),
            "card_cache_dir": status["cache_dir"],
            "card_log_file": status["log_file"],
            "cached_cards": status["cached_cards"],
            "last_render_error": status["last_error"],
            "last_render_success_at": status["last_success_at"],
            "bindings": bindings,
            "panels": len(panels),
            "auto_update_minutes": self._setting_value("auto_update_minutes"),
            "auto_update_running": bool(
                self._auto_task and not self._auto_task.done()
            ),
            "last_update_at": self._last_update_at,
            "last_update_msg": self._last_update_msg,
            "trace_count": trace_count,
            "trace_synced_at": trace_at,
        }

        return json_response({"checks": checks, "info": info})

    async def api_render_test(self):
        """POST 用极简 HTML 测一次渲染端点，判断超时是端点慢还是卡片太大。"""
        payload = await request.json(default={})
        try:
            timeout = float(payload.get("timeout") or 30)
        except (TypeError, ValueError):
            timeout = 30.0
        timeout = max(5.0, min(120.0, timeout))

        result = await probe_render(
            self,
            timeout=timeout,
            use_rotation=self._rotation_enabled(),
            preferred_endpoint=self._preferred_endpoint(),
        )
        result["timeout"] = timeout
        result["inline_images"] = self._inline_images_enabled()
        result["rotation"] = self._rotation_enabled()
        result["endpoint"] = result.get("endpoint") or self._preferred_endpoint()
        return json_response(result)

    # ------------------------------------------------------------------

    async def terminate(self):
        """插件被卸载或重载时调用。"""
        for task in (
            self._auto_task,
            self._refresh_task,
            self._precache_task,
            self._maintenance_task,
            self._guide_sync_task,
            *self._refresh_tasks.values(),
        ):
            if task and not task.done():
                task.cancel()
        logger.info("[星铁面板] 插件已卸载")
