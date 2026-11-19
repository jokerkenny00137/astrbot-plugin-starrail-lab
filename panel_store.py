"""SQLite 存储层。

存三类数据：
  1. last_query —— 每个会话"最近一次查询"的面板摘要。
     作用：用户查完面板后继续聊天时，把这份摘要注入大模型的上下文，
     机器人才能基于真实数据给建议，而不是瞎编。

  2. bindings —— QQ 号 与 游戏 UID 的绑定关系（预留给绑定功能）。

  3. panels —— 完整面板 JSON 存档（预留给定时自动更新功能）。

数据库文件默认放在插件目录的 data/ 下；
作为 AstrBot 插件运行时，main.py 会把它改到 AstrBot 的 plugin_data 目录。
"""

from __future__ import annotations

import json
import sqlite3
import time

from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent / "data" / "starrail.db"

# 「最近一次查询」的默认有效期（秒）。
# 超过这个时间就不再注入给大模型，避免拿几天前的数据当现状。
LAST_QUERY_TTL = 1800


def set_db_path(path: str | Path) -> Path:
    """修改数据库位置，并确保表结构存在。"""
    global DB_PATH
    DB_PATH = Path(path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    return DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等，重复调用没关系）。"""
    with closing(_connect()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS last_query (
                session_key TEXT PRIMARY KEY,
                uid         TEXT NOT NULL,
                nickname    TEXT,
                summary     TEXT,
                updated_at  REAL
            );

            CREATE TABLE IF NOT EXISTS bindings (
                platform   TEXT NOT NULL DEFAULT '',
                user_id    TEXT NOT NULL,
                uid        TEXT NOT NULL,
                created_at REAL,
                PRIMARY KEY (platform, user_id)
            );

            CREATE TABLE IF NOT EXISTS panels (
                uid        TEXT PRIMARY KEY,
                nickname   TEXT,
                data_json  TEXT,
                updated_at REAL
            );
            """
        )


# --------------------------------------------------------------------------
# 最近一次查询（给大模型做上下文用）
# --------------------------------------------------------------------------


def save_last_query(
    session_key: str,
    uid: str,
    nickname: str,
    summary: str,
) -> None:
    """记住某个会话最近查的是哪个玩家。"""
    now = time.time()
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO last_query (session_key, uid, nickname, summary, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                uid = excluded.uid,
                nickname = excluded.nickname,
                summary = excluded.summary,
                updated_at = excluded.updated_at
            """,
            (session_key, uid, nickname, summary, now),
        )


def get_last_query(
    session_key: str,
    max_age: float = LAST_QUERY_TTL,
) -> Optional[Dict[str, Any]]:
    """取某个会话最近一次的查询记录。过期或没有都返回 None。"""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT uid, nickname, summary, updated_at FROM last_query WHERE session_key = ?",
            (session_key,),
        ).fetchone()

    if row is None:
        return None
    if max_age and time.time() - (row["updated_at"] or 0) > max_age:
        return None
    return {
        "uid": row["uid"],
        "nickname": row["nickname"],
        "summary": row["summary"],
        "updated_at": row["updated_at"],
    }


def clear_last_query(session_key: str) -> bool:
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM last_query WHERE session_key = ?", (session_key,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# 绑定关系（预留给绑定功能）
# --------------------------------------------------------------------------


def bind_user(user_id: str, uid: str, platform: str = "") -> None:
    """绑定。一个 QQ 只保留一条记录，再次绑定就是换绑。"""
    user_id = str(user_id)
    with closing(_connect()) as conn, conn:
        # 先清掉这个 QQ 的旧记录，避免出现多条（这也是"换绑"的实现）
        conn.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT INTO bindings (platform, user_id, uid, created_at) VALUES (?, ?, ?, ?)",
            (platform, user_id, str(uid), time.time()),
        )


def get_bound_uid(user_id: str, platform: str = "") -> Optional[str]:
    """查某个 QQ 绑定的 UID。

    只按 QQ 号查，不看平台——否则在网页面板添加的绑定（平台为空）
    和聊天指令里查询（平台是 aiocqhttp）会匹配不上。
    """
    user_id = str(user_id)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT uid FROM bindings WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["uid"] if row else None


def unbind_user(user_id: str, platform: str = "") -> bool:
    """解除绑定。同样只按 QQ 号删，platform 参数仅为兼容旧调用。"""
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM bindings WHERE user_id = ?", (str(user_id),))
    return cur.rowcount > 0


def list_bindings(limit: int = 100) -> List[Dict[str, Any]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT platform, user_id, uid, created_at FROM bindings "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 完整面板存档（预留给定时自动更新）
# --------------------------------------------------------------------------


def save_panel(uid: str, nickname: str, result: Dict[str, Any]) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO panels (uid, nickname, data_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                nickname = excluded.nickname,
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (str(uid), nickname, json.dumps(result, ensure_ascii=False), time.time()),
        )


def get_panel(uid: str) -> Optional[Dict[str, Any]]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT nickname, data_json, updated_at FROM panels WHERE uid = ?",
            (str(uid),),
        ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["data_json"])
    except (TypeError, ValueError):
        return None
    data["updated_at"] = row["updated_at"]
    return data


def list_panels() -> List[Dict[str, Any]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT uid, nickname, updated_at FROM panels ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
