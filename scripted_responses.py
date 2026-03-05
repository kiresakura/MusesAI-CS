"""
繆思精工客服系統 — 預存語錄模組

功能：
1. 載入 scripted_responses.json 語錄資料庫
2. match_scripted_response() — 根據使用者訊息比對最佳語錄
3. get_by_id() / get_by_title() — 直接取得指定語錄（供事件觸發用）

匹配優先級：
    must > contextual > conditional > template
匹配方式：
    trigger_keywords 關鍵字比對，score = 命中關鍵字數量
過濾條件：
    user_identity（None = 不限）、channel
"""

import json
import os
from typing import Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_SCRIPT_DIR, "scripted_responses.json")

# 語錄資料庫（懶載入）
_responses: list[dict] = []

# priority 數值越小越優先
_PRIORITY_RANK = {"must": 0, "contextual": 1, "conditional": 2, "template": 3}


# ============================================================
# 資料載入
# ============================================================

def _load():
    """懶載入語錄資料庫（只載入一次）。"""
    global _responses
    if _responses:
        return
    with open(_DB_PATH, "r", encoding="utf-8") as f:
        _responses = json.load(f)


def reload():
    """強制重新載入（語錄檔案更新後使用）。"""
    global _responses
    _responses = []
    _load()


# ============================================================
# 查詢介面
# ============================================================

def get_by_id(response_id: str) -> Optional[dict]:
    """直接以 ID 取得語錄（用於事件觸發，例如身分確認後自動發送）。"""
    _load()
    for r in _responses:
        if r["id"] == response_id:
            return r
    return None


def get_by_title(title: str) -> Optional[dict]:
    """以標題取得語錄。"""
    _load()
    for r in _responses:
        if r["title"] == title:
            return r
    return None


def get_by_category(
    category: str,
    user_identity: Optional[str] = None,
    channel: str = "universal",
) -> list[dict]:
    """取得同分類的所有語錄，可按身分和渠道過濾。"""
    _load()
    results = []
    for r in _responses:
        if r["category"] != category:
            continue
        if not _channel_match(r["channel"], channel):
            continue
        if r.get("user_identity") and r["user_identity"] != user_identity:
            continue
        results.append(r)
    return results


# ============================================================
# 匹配引擎
# ============================================================

def _channel_match(resp_channel: str, request_channel: str) -> bool:
    """判斷語錄渠道是否符合目前請求渠道。"""
    if resp_channel == "universal":
        return True
    if resp_channel == "both":
        return True
    return resp_channel == request_channel


def _score(response: dict, msg_lower: str) -> int:
    """
    計算語錄與訊息的關鍵字匹配分數。

    完整詞組匹配（多字關鍵字）給 2 分，
    單字關鍵字命中給 1 分。
    """
    keywords = response.get("trigger_keywords", [])
    if not keywords:
        return 0

    score = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in msg_lower:
            # 多字詞組命中給予加權
            score += 2 if len(kw) >= 4 else 1
    return score


def match_scripted_response(
    user_message: str,
    conversation_state: Optional[dict] = None,
    user_identity: Optional[str] = None,
    channel: str = "universal",
    min_score: int = 1,
) -> Optional[dict]:
    """
    根據使用者訊息比對最佳預存語錄。

    參數：
        user_message: 使用者輸入的訊息
        conversation_state: 對話狀態（預留，目前支援 stage 欄位）
        user_identity: 使用者身分（owner / designer / manufacturer / None）
        channel: 目前渠道（fb / line / universal）
        min_score: 最低命中分數，低於此值不回傳

    回傳：
        匹配到的語錄 dict，或 None

    語錄 dict 結構：
        {
            "id": str,
            "title": str,
            "category": str,
            "priority": str,
            "content": str,
            "attachments": list[dict],
            ...
        }
    """
    _load()

    if not user_message or not user_message.strip():
        return None

    if conversation_state is None:
        conversation_state = {}

    msg_lower = user_message.strip().lower()
    candidates = []

    for resp in _responses:
        # ── 渠道過濾 ──
        if not _channel_match(resp.get("channel", "universal"), channel):
            continue

        # ── 身分過濾：語錄有指定身分時，必須與用戶身分完全吻合才允許觸發
        # user_identity=None（未確認身分）時，排除所有身分限定語錄
        resp_identity = resp.get("user_identity")
        if resp_identity and resp_identity != user_identity:
            continue

        # ── 關鍵字評分 ──
        s = _score(resp, msg_lower)
        if s < min_score:
            continue

        priority_rank = _PRIORITY_RANK.get(resp.get("priority", "template"), 3)
        candidates.append((s, priority_rank, resp))

    if not candidates:
        return None

    # 分數高者優先，分數相同時 priority 小者優先
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][2]


# ============================================================
# 回覆格式化
# ============================================================

def format_reply(response: dict) -> dict:
    """
    將語錄 dict 格式化為 chatbot_service 標準回傳格式。

    回傳：
        {
            "reply": str,
            "attachments": list[dict],
            "scripted_id": str,
            "scripted_title": str,
        }
    """
    content = response.get("content", "")
    attachments = response.get("attachments", [])

    # 過濾掉 image_pending（圖片尚未上傳，不發出 URL）
    available_attachments = [
        a for a in attachments if a.get("type") != "image_pending"
    ]

    return {
        "reply": content,
        "attachments": available_attachments,
        "scripted_id": response.get("id", ""),
        "scripted_title": response.get("title", ""),
    }


# ============================================================
# 測試
# ============================================================

if __name__ == "__main__":
    print("🧪 預存語錄匹配引擎測試\n")
    _load()
    print(f"✅ 載入 {len(_responses)} 條語錄\n")

    test_cases = [
        ("電視牆要怎麼施作？", None, "universal"),
        ("你們在台南哪裡可以看實品？", None, "universal"),
        ("R角最小可以幾公分？", None, "universal"),
        ("熱彎大概多少錢？", None, "universal"),
        ("我是設計師，想了解你們的產品", "designer", "universal"),
        ("一體盆最小深度要多少？", None, "universal"),
        ("你們有商城嗎？在哪裡買？", None, "universal"),
        ("繆思岩的材質是什麼做的？", None, "universal"),
        ("板材有哪些規格？", None, "universal"),
        ("你好", None, "universal"),
    ]

    for msg, identity, ch in test_cases:
        result = match_scripted_response(msg, user_identity=identity, channel=ch)
        if result:
            formatted = format_reply(result)
            print(f"📩 「{msg}」")
            print(f"   → [{result['id']}] {result['title']} (priority={result['priority']})")
            print(f"   💬 {formatted['reply'][:80]}...")
            if formatted["attachments"]:
                print(f"   📎 附件：{[a['label'] for a in formatted['attachments']]}")
        else:
            print(f"📩 「{msg}」")
            print(f"   → (無匹配，走 RAG + LLM)")
        print()
