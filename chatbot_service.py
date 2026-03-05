"""
繆思精工客服系統 — 主服務模組

功能：
1. process_message(user_id, message) — 主入口，處理客戶訊息並回傳回覆
2. 多輪對話支援（記憶體內存每個 user 最近 N 條對話）
3. 整合意圖辨識、RAG 搜尋、錯誤處理
4. CLI 測試模式

流程：
    收到訊息
      → intent_classifier.classify(message)
      → 根據 intent:
         - greeting → 直接回覆制式問候（不走 RAG）
         - transfer → 直接回覆轉人工訊息
         - pricing/spec/catalog/service/store → RAG 搜尋 + LLM 生成
         - other → fallback
      → 回傳回覆訊息

使用方式：
    python chatbot_service.py          # 互動式 CLI 測試
    python chatbot_service.py --auto   # 自動測試預設對話
"""

import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# 載入各模組
import intent_classifier
from error_handler import handle_error, DB_PATH
from rag_config import (
    GREETING_RESPONSES,
    TRANSFER_RESPONSE,
    TRANSFER_THRESHOLD,
    CUSTOMER_SERVICE_PROMPT,
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    VECTORS_OUTPUT_PATH,
    MAX_RETRIES, RETRY_DELAY,
)

# 延遲載入 rag_search（需要 numpy，可能較慢）
rag_search = None
vector_db = None


# ============================================================
# 對話歷史管理（SQLite 持久化）
# ============================================================

# 每個 user 最多保留的對話記錄數
MAX_HISTORY = 10

# 對話歷史超過此天數自動刪除
HISTORY_EXPIRE_DAYS = 7


def _get_db():
    """取得 SQLite 連線並確保 conversation_history 表存在。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_conv_user_id ON conversation_history(user_id)
    """)
    conn.commit()
    return conn


def _add_to_history(user_id: str, role: str, content: str):
    """將一條對話加入 user 的歷史記錄（SQLite）。"""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO conversation_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, role, content, datetime.now().isoformat()),
        )
        # 保留每個 user 最新的 MAX_HISTORY 條
        conn.execute(f"""
            DELETE FROM conversation_history
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM conversation_history
                WHERE user_id = ?
                ORDER BY id DESC LIMIT {MAX_HISTORY}
            )
        """, (user_id, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️  對話歷史寫入失敗：{e}")


def _get_history_context(user_id: str) -> str:
    """取得 user 的對話歷史，格式化為 prompt context。"""
    try:
        conn = _get_db()
        cursor = conn.execute(
            "SELECT role, content FROM conversation_history WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return ""

    if not rows:
        return ""

    lines = ["\n【對話歷史】"]
    for role, content in rows:
        role_label = "客戶" if role == "user" else "客服"
        lines.append(f"{role_label}：{content}")

    return "\n".join(lines)


def clear_history(user_id: str):
    """清除特定 user 的對話歷史。"""
    try:
        conn = _get_db()
        conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠️  清除對話歷史失敗：{e}")


def cleanup_expired_history():
    """刪除超過 HISTORY_EXPIRE_DAYS 天的對話歷史。"""
    try:
        cutoff = (datetime.now() - timedelta(days=HISTORY_EXPIRE_DAYS)).isoformat()
        conn = _get_db()
        cursor = conn.execute("DELETE FROM conversation_history WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            print(f"  🗑️  已清理 {deleted} 條過期對話歷史")
    except Exception as e:
        print(f"  ⚠️  清理過期歷史失敗：{e}")


# ============================================================
# RAG 引擎初始化（延遲載入）
# ============================================================

def _ensure_rag_loaded():
    """確保 RAG 引擎已載入（延遲初始化，避免啟動過慢）。"""
    global rag_search, vector_db

    if rag_search is not None and vector_db is not None:
        return True

    try:
        import rag_search as _rag_search
        rag_search = _rag_search

        print("📂 載入 RAG 知識庫...")
        vector_db = rag_search.load_vector_db(VECTORS_OUTPUT_PATH)
        return True

    except FileNotFoundError:
        print(f"  ❌ 找不到向量資料庫：{VECTORS_OUTPUT_PATH}")
        return False
    except Exception as e:
        print(f"  ❌ RAG 引擎載入失敗：{e}")
        return False


# ============================================================
# RAG + LLM 回覆生成
# ============================================================

def _generate_rag_response(user_id: str, message: str, intent: str) -> str:
    """
    透過 RAG 搜尋知識庫 + LLM 生成回覆。

    流程：
    1. 確保 RAG 引擎已載入
    2. 搜尋相關知識
    3. 組裝含上下文的 prompt
    4. 呼叫 LLM 生成回覆
    """
    # 確保 RAG 已載入
    if not _ensure_rag_loaded():
        return handle_error("api_error", {
            "user_id": user_id,
            "message": message,
            "error_detail": "RAG 引擎未載入",
        })

    try:
        # 搜尋相關知識
        query_embedding = rag_search.get_query_embedding(message)
        results = rag_search.search_similar(query_embedding, vector_db)

        if not results:
            return handle_error("no_results", {
                "user_id": user_id,
                "message": message,
            })

        # 組裝知識庫上下文
        context_parts = []
        for i, ctx in enumerate(results, 1):
            meta = ctx["metadata"]
            part = f"【參考資料 {i}】（{meta['category']}"
            if meta.get("subcategory"):
                part += f" > {meta['subcategory']}"
            part += f"）\n標題：{meta['title']}\n內容：{meta['content']}"
            if meta.get("sizes"):
                part += f"\n可用尺寸：{meta['sizes']}"
            if meta.get("surface"):
                part += f"\n表面工藝：{meta['surface']}"
            if meta.get("link"):
                part += f"\n相關連結：{meta['link']}"
            if meta.get("note"):
                part += f"\n備註：{meta['note']}"
            context_parts.append(part)

        knowledge_text = "\n\n".join(context_parts)

        # 取得對話歷史
        conversation_context = _get_history_context(user_id)

        # 組裝 system prompt（帶入對話上下文）
        system_prompt = CUSTOMER_SERVICE_PROMPT.replace(
            "{conversation_context}", conversation_context
        )

        # 組裝 messages
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"以下是與客戶問題相關的知識庫資料：\n\n{knowledge_text}\n\n"
                    f"---\n\n"
                    f"客戶的意圖類型：{intent}\n"
                    f"客戶問題：{message}\n\n"
                    f"請根據以上知識庫資料回答客戶的問題。"
                ),
            },
        ]

        # 呼叫 LLM
        answer = rag_search.call_llm(messages)
        return answer

    except requests.exceptions.Timeout:
        return handle_error("api_error", {
            "user_id": user_id,
            "message": message,
            "error_detail": "LLM API 請求超時",
        })
    except requests.exceptions.ConnectionError:
        return handle_error("api_error", {
            "user_id": user_id,
            "message": message,
            "error_detail": "網路連線錯誤",
        })
    except Exception as e:
        # 檢查是否為限速錯誤
        if "429" in str(e):
            return handle_error("rate_limit", {
                "user_id": user_id,
                "message": message,
                "error_detail": str(e),
            })
        return handle_error("api_error", {
            "user_id": user_id,
            "message": message,
            "error_detail": str(e),
        })


# ============================================================
# 主入口
# ============================================================

def process_message(user_id: str, message: str, verbose: bool = False) -> dict:
    """
    處理客戶訊息的主入口。

    參數：
        user_id: 使用者 ID
        message: 客戶訊息
        verbose: 是否印出處理過程

    回傳：
        {
            "reply": str,          # 回覆訊息
            "intent": str,         # 辨識出的意圖
            "confidence": float,   # 信心度
            "source": str,         # 回覆來源（keyword / rag / fallback）
        }
    """
    if not message or not message.strip():
        return {
            "reply": "請問有什麼需要幫忙的嗎？😊",
            "intent": "other",
            "confidence": 0.0,
            "source": "fallback",
        }

    message = message.strip()

    # ── Step 1：意圖辨識 ──
    intent_result = intent_classifier.classify(message)
    intent = intent_result["intent"]
    confidence = intent_result["confidence"]
    reason = intent_result["reason"]

    if verbose:
        print(f"  🏷️  意圖：{intent}  |  信心度：{confidence:.2f}  |  原因：{reason}")

    # ── Step 2：信心度過低 → 引導客戶描述或轉人工 ──
    if confidence < TRANSFER_THRESHOLD and intent not in ("greeting", "transfer"):
        reply = handle_error("low_confidence", {
            "user_id": user_id,
            "message": message,
        })
        _add_to_history(user_id, "user", message)
        _add_to_history(user_id, "assistant", reply)
        return {
            "reply": reply,
            "intent": intent,
            "confidence": confidence,
            "source": "fallback",
        }

    # ── Step 3：根據意圖處理 ──

    # greeting → 制式回覆，不走 RAG
    if intent == "greeting":
        reply = random.choice(GREETING_RESPONSES)
        _add_to_history(user_id, "user", message)
        _add_to_history(user_id, "assistant", reply)
        return {
            "reply": reply,
            "intent": intent,
            "confidence": confidence,
            "source": "keyword",
        }

    # transfer → 轉人工訊息
    if intent == "transfer":
        reply = TRANSFER_RESPONSE
        _add_to_history(user_id, "user", message)
        _add_to_history(user_id, "assistant", reply)
        return {
            "reply": reply,
            "intent": intent,
            "confidence": confidence,
            "source": "keyword",
        }

    # pricing / spec / catalog / service / store → RAG + LLM
    if intent in ("pricing", "spec", "catalog", "service", "store"):
        if verbose:
            print("  🔍 進入 RAG 搜尋流程...")

        reply = _generate_rag_response(user_id, message, intent)
        _add_to_history(user_id, "user", message)
        _add_to_history(user_id, "assistant", reply)
        return {
            "reply": reply,
            "intent": intent,
            "confidence": confidence,
            "source": "rag",
        }

    # other → fallback
    reply = handle_error("inappropriate", {
        "user_id": user_id,
        "message": message,
    })
    _add_to_history(user_id, "user", message)
    _add_to_history(user_id, "assistant", reply)
    return {
        "reply": reply,
        "intent": intent,
        "confidence": confidence,
        "source": "fallback",
    }


# ============================================================
# CLI 測試模式
# ============================================================

def auto_test():
    """
    自動測試模式：模擬預設對話，驗證各意圖路徑。
    """
    print("\n" + "=" * 60)
    print("🧪 繆思精工客服系統 — 自動測試")
    print("=" * 60 + "\n")

    test_cases = [
        {
            "user_id": "test_user_A",
            "message": "你好",
            "expected_intent": "greeting",
            "expected_source": "keyword",
        },
        {
            "user_id": "test_user_B",
            "message": "繆思岩一坪多少錢？",
            "expected_intent": "pricing",
            "expected_source": "rag",
        },
        {
            "user_id": "test_user_C",
            "message": "有什麼花色可以選？",
            "expected_intent": "catalog",
            "expected_source": "rag",
        },
        {
            "user_id": "test_user_D",
            "message": "我要找真人客服",
            "expected_intent": "transfer",
            "expected_source": "keyword",
        },
    ]

    passed = 0
    total = len(test_cases)

    for i, tc in enumerate(test_cases, 1):
        print(f"── 測試 {i}/{total} ──")
        print(f"📩 訊息：「{tc['message']}」")
        print(f"🎯 預期意圖：{tc['expected_intent']}  |  預期來源：{tc['expected_source']}")

        result = process_message(tc["user_id"], tc["message"], verbose=True)

        intent_ok = result["intent"] == tc["expected_intent"]
        source_ok = result["source"] == tc["expected_source"]

        status = "✅ PASS" if (intent_ok and source_ok) else "❌ FAIL"
        if intent_ok and source_ok:
            passed += 1

        print(f"📤 實際意圖：{result['intent']}  |  信心度：{result['confidence']:.2f}  |  來源：{result['source']}")
        print(f"💬 回覆（前 100 字）：{result['reply'][:100]}...")
        print(f"結果：{status}")
        if not intent_ok:
            print(f"  ⚠️  意圖不符：預期 {tc['expected_intent']}，實際 {result['intent']}")
        if not source_ok:
            print(f"  ⚠️  來源不符：預期 {tc['expected_source']}，實際 {result['source']}")
        print()

    print("=" * 60)
    print(f"📊 測試結果：{passed}/{total} 通過")
    print("=" * 60)


def interactive_mode():
    """
    互動式 CLI 測試模式。

    支援多個 user 的對話模擬。
    """
    print("\n" + "=" * 60)
    print("🏛️  繆思精工客服系統 — 互動測試")
    print("=" * 60)
    print("指令：")
    print("  輸入訊息 → 以預設 user 身份發送")
    print("  /user <id> → 切換 user")
    print("  /history → 查看當前 user 對話歷史")
    print("  /clear → 清除當前 user 對話歷史")
    print("  /quit → 退出")
    print()

    current_user = "test_user_1"
    print(f"👤 當前 User：{current_user}\n")

    while True:
        try:
            user_input = input("❓ 訊息：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n👋 再見！")
            break

        if not user_input:
            continue

        # 指令處理
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()

            if cmd in ("/quit", "/exit", "/q"):
                print("\n👋 再見！")
                break

            elif cmd == "/user":
                if len(parts) > 1:
                    current_user = parts[1]
                    print(f"  👤 切換到 User：{current_user}\n")
                else:
                    print(f"  👤 當前 User：{current_user}\n")

            elif cmd == "/history":
                try:
                    conn = _get_db()
                    cursor = conn.execute(
                        "SELECT role, content FROM conversation_history WHERE user_id = ? ORDER BY id ASC",
                        (current_user,),
                    )
                    rows = cursor.fetchall()
                    conn.close()
                except Exception:
                    rows = []

                if not rows:
                    print("  📝 無對話歷史\n")
                else:
                    print(f"  📝 {current_user} 的對話歷史（{len(rows)} 條）：")
                    for role, content in rows:
                        icon = "👤" if role == "user" else "🤖"
                        print(f"    {icon} {content[:80]}")
                    print()

            elif cmd == "/clear":
                clear_history(current_user)
                print(f"  🗑️  已清除 {current_user} 的對話歷史\n")

            else:
                print(f"  ❓ 未知指令：{cmd}\n")

            continue

        # 處理訊息
        print()
        result = process_message(current_user, user_input, verbose=True)

        print(f"\n{'─' * 50}")
        print(f"🏷️  意圖：{result['intent']}  |  信心度：{result['confidence']:.2f}  |  來源：{result['source']}")
        print(f"💬 回覆：")
        print(result["reply"])
        print(f"{'─' * 50}\n")


# ============================================================
# 主程式
# ============================================================

def main():
    """主程式入口"""
    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        auto_test()
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
