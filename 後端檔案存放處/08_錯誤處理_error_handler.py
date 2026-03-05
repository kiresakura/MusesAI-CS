"""
繆思精工客服系統 — 錯誤處理與 Fallback 模組

功能：
1. 統一錯誤處理入口 handle_error()
2. 根據錯誤類型回傳友善的客戶回覆
3. 記錄錯誤 log 到 SQLite（並發安全）

錯誤類型：
- no_results: RAG 搜尋無結果
- low_confidence: 意圖辨識信心度低
- api_error: API 呼叫失敗
- rate_limit: 被限速
- inappropriate: 超出服務範圍
"""

import os
import sqlite3
from datetime import datetime

from rag_config import FALLBACK_MESSAGES

# SQLite 資料庫路徑（與本檔案同目錄）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatbot.db")

# 最多保留的 error log 筆數
MAX_ERROR_LOGS = 500


def _get_db():
    """取得 SQLite 連線並確保 error_logs 表存在。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            error_type TEXT NOT NULL,
            user_id TEXT DEFAULT 'unknown',
            message TEXT DEFAULT '',
            error_detail TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return conn


def handle_error(error_type: str, context: dict = None) -> str:
    """
    統一錯誤處理入口。

    參數：
        error_type: 錯誤類型（no_results / low_confidence / api_error / rate_limit / inappropriate）
        context: 錯誤上下文資訊（選填），包含 user_id, message, error_detail 等

    回傳：
        友善的客戶回覆訊息（str）
    """
    if context is None:
        context = {}

    reply = FALLBACK_MESSAGES.get(error_type, FALLBACK_MESSAGES.get("general", "系統發生錯誤，請稍後再試。"))

    _log_error(error_type, context)

    return reply


def _log_error(error_type: str, context: dict):
    """將錯誤記錄寫入 SQLite（WAL 模式，並發安全）。"""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO error_logs (timestamp, error_type, user_id, message, error_detail) VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                error_type,
                context.get("user_id", "unknown"),
                context.get("message", ""),
                context.get("error_detail", ""),
            ),
        )
        # 只保留最近 MAX_ERROR_LOGS 筆
        conn.execute(f"""
            DELETE FROM error_logs WHERE id NOT IN (
                SELECT id FROM error_logs ORDER BY id DESC LIMIT {MAX_ERROR_LOGS}
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️  錯誤 log 寫入失敗：{e}")


def get_error_stats() -> dict:
    """
    取得錯誤統計資訊（供管理後台或除錯用）。

    回傳：各錯誤類型的發生次數
    """
    try:
        conn = _get_db()
        cursor = conn.execute("SELECT error_type, COUNT(*) FROM error_logs GROUP BY error_type")
        stats = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return stats
    except Exception:
        return {}


# ============================================================
# 測試
# ============================================================

if __name__ == "__main__":
    print("🧪 錯誤處理模組測試\n")

    test_cases = [
        ("no_results", {"user_id": "test_user_1", "message": "你們有賣磁磚嗎"}),
        ("low_confidence", {"user_id": "test_user_2", "message": "嗯嗯"}),
        ("api_error", {"user_id": "test_user_3", "message": "報價", "error_detail": "HTTP 500"}),
        ("rate_limit", {"user_id": "test_user_4", "message": "你好"}),
        ("inappropriate", {"user_id": "test_user_5", "message": "今天天氣如何"}),
    ]

    for error_type, context in test_cases:
        reply = handle_error(error_type, context)
        print(f"【{error_type}】")
        print(f"  訊息：{context.get('message', '')}")
        print(f"  回覆：{reply}")
        print()

    stats = get_error_stats()
    print(f"📊 錯誤統計：{stats}")
