"""
繆思精工客服系統 — 人工介入模式管理

功能：
1. 切換用戶聊天模式（auto / manual）
2. manual 模式預設 60 分鐘後自動切回 auto
3. 支援延長手動時間
4. 背景執行緒定期檢查並切回過期的 manual 用戶

資料表 chat_mode：
    user_id        TEXT PRIMARY KEY
    mode           TEXT DEFAULT 'auto'     -- 'auto' 或 'manual'
    switched_at    TEXT                    -- 切換時間（ISO 格式）
    switched_by    TEXT                    -- 操作員名稱
    auto_revert_at TEXT                    -- 預計自動切回時間
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from error_handler import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_MANUAL_MINUTES = 60


# ============================================================
# SQLite 操作
# ============================================================

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_mode (
            user_id        TEXT PRIMARY KEY,
            mode           TEXT NOT NULL DEFAULT 'auto',
            switched_at    TEXT,
            switched_by    TEXT,
            auto_revert_at TEXT
        )
    """)
    conn.commit()
    return conn


def _build_status(user_id, mode, switched_at, switched_by, auto_revert_at) -> dict:
    remaining = None
    if mode == "manual" and auto_revert_at:
        delta = (datetime.fromisoformat(auto_revert_at) - datetime.now()).total_seconds()
        remaining = max(0, round(delta))

    return {
        "user_id": user_id,
        "mode": mode,
        "switched_at": switched_at,
        "switched_by": switched_by,
        "auto_revert_at": auto_revert_at,
        "remaining_seconds": remaining,
    }


# ============================================================
# 查詢
# ============================================================

def get_mode(user_id: str) -> dict:
    """取得用戶當前聊天模式。"""
    conn = _get_db()
    row = conn.execute(
        "SELECT mode, switched_at, switched_by, auto_revert_at "
        "FROM chat_mode WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return _build_status(user_id, "auto", None, None, None)

    return _build_status(user_id, row[0], row[1], row[2], row[3])


def is_manual(user_id: str) -> bool:
    """快速檢查用戶是否處於 manual 模式。"""
    conn = _get_db()
    row = conn.execute(
        "SELECT mode FROM chat_mode WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row is not None and row[0] == "manual"


def list_manual_users() -> list:
    """列出所有目前為 manual 模式的用戶。"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT user_id, switched_at, switched_by, auto_revert_at "
        "FROM chat_mode WHERE mode = 'manual'"
    ).fetchall()
    conn.close()

    return [
        _build_status(uid, "manual", sat, sby, ara)
        for uid, sat, sby, ara in rows
    ]


# ============================================================
# 切換 / 延長
# ============================================================

def set_mode(user_id: str, mode: str, operator: str | None = None) -> dict:
    """
    切換用戶聊天模式。

    - manual：自動設定 auto_revert_at = now + 60 分鐘
    - auto：清空 switched_by 和 auto_revert_at
    """
    if mode not in ("auto", "manual"):
        raise ValueError(f"Invalid mode: {mode}")

    now = datetime.now()
    conn = _get_db()

    if mode == "manual":
        auto_revert_at = (now + timedelta(minutes=DEFAULT_MANUAL_MINUTES)).isoformat()
        conn.execute(
            "INSERT INTO chat_mode (user_id, mode, switched_at, switched_by, auto_revert_at) "
            "VALUES (?, 'manual', ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  mode='manual', switched_at=excluded.switched_at, "
            "  switched_by=excluded.switched_by, auto_revert_at=excluded.auto_revert_at",
            (user_id, now.isoformat(), operator, auto_revert_at),
        )
    else:
        conn.execute(
            "INSERT INTO chat_mode (user_id, mode, switched_at, switched_by, auto_revert_at) "
            "VALUES (?, 'auto', ?, NULL, NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  mode='auto', switched_at=excluded.switched_at, "
            "  switched_by=NULL, auto_revert_at=NULL",
            (user_id, now.isoformat()),
        )

    conn.commit()
    conn.close()

    logger.info(f"chat_mode: user={user_id} -> {mode} (by={operator})")
    return get_mode(user_id)


def extend_manual(user_id: str, minutes: int) -> dict:
    """延長手動模式的時間。用戶必須已在 manual 模式。"""
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    conn = _get_db()
    row = conn.execute(
        "SELECT mode, auto_revert_at FROM chat_mode WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if row is None or row[0] != "manual":
        conn.close()
        raise ValueError(f"User {user_id} is not in manual mode")

    base = max(datetime.fromisoformat(row[1]), datetime.now())
    new_revert = (base + timedelta(minutes=minutes)).isoformat()

    conn.execute(
        "UPDATE chat_mode SET auto_revert_at = ? WHERE user_id = ?",
        (new_revert, user_id),
    )
    conn.commit()
    conn.close()

    logger.info(f"chat_mode: user={user_id} extended +{minutes}min -> {new_revert}")
    return get_mode(user_id)


# ============================================================
# 自動切回
# ============================================================

def revert_expired() -> list:
    """將所有 auto_revert_at < now 的 manual 用戶切回 auto。回傳被切回的 user_id 列表。"""
    now = datetime.now().isoformat()
    conn = _get_db()

    expired = [
        row[0] for row in conn.execute(
            "SELECT user_id FROM chat_mode WHERE mode='manual' AND auto_revert_at < ?",
            (now,),
        ).fetchall()
    ]

    if expired:
        conn.execute(
            "UPDATE chat_mode SET mode='auto', switched_at=?, switched_by=NULL, auto_revert_at=NULL "
            "WHERE mode='manual' AND auto_revert_at < ?",
            (now, now),
        )
        conn.commit()
        for uid in expired:
            logger.info(f"chat_mode: user={uid} auto-reverted to auto")

    conn.close()
    return expired


_revert_thread = None


def start_auto_revert_scheduler(interval_seconds: int = 60):
    """啟動背景執行緒，每 interval_seconds 秒檢查一次過期的 manual 用戶。"""
    global _revert_thread

    def _loop():
        while True:
            try:
                expired = revert_expired()
                if expired:
                    logger.info(f"auto-revert: {len(expired)} user(s) reverted")
            except Exception as e:
                logger.error(f"auto-revert error: {e}")
            time.sleep(interval_seconds)

    _revert_thread = threading.Thread(target=_loop, daemon=True, name="chat-mode-revert")
    _revert_thread.start()
    logger.info(f"chat_mode auto-revert scheduler started (every {interval_seconds}s)")
