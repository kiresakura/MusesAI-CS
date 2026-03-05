"""
繆思精工客服系統 — 意圖辨識模組

功能：
1. 用 keyword matching 快取簡單意圖（greeting / transfer），免呼叫 API
2. 複雜意圖呼叫 LLM 做 zero-shot classification
3. 回傳 {intent, confidence, reason}

支援的意圖：
- pricing: 詢價、報價相關
- spec: 查規格、尺寸、材質
- catalog: 查花色、色系、產品目錄
- service: 施工服務、熱彎、拜訪
- store: 商城產品、購買
- greeting: 打招呼、自我介紹
- transfer: 需要轉人工
- other: 無法歸類
"""

import json
import re
import requests
import time

from rag_config import (
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    INTENT_SYSTEM_PROMPT,
    MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT,
)


# ============================================================
# Keyword 快取規則（不需要呼叫 LLM 的簡單意圖）
# ============================================================

# greeting 關鍵字（完全匹配或開頭匹配）
_GREETING_EXACT = {
    "你好", "您好", "嗨", "哈囉", "hi", "hello", "hey",
    "早安", "午安", "晚安", "安安", "嗨嗨", "哈嘍",
    "請問", "你好呀", "您好呀",
}

# greeting 模式（正則匹配）
_GREETING_PATTERNS = [
    r"^(你好|您好|嗨|哈囉|hi|hello)[\s，。！!？?]*$",
    r"^你是誰[\s？?]*$",
    r"^(在嗎|有人嗎)[\s？?]*$",
]

# transfer 關鍵字
_TRANSFER_KEYWORDS = [
    "找真人", "真人客服", "轉人工", "人工客服", "人工服務",
    "轉接", "投訴", "客訴", "申訴", "找人", "找客服",
    "不要機器人", "不要AI", "不要 AI",
]

# 各意圖的輔助 keyword（用於快速匹配，提高命中率）
_INTENT_KEYWORD_HINTS = {
    "pricing": ["多少錢", "價格", "報價", "費用", "一坪", "每坪", "價位", "成本", "預算", "貴不貴", "便宜"],
    "spec": ["規格", "尺寸", "材質", "厚度", "重量", "防火", "防潮", "抗刮", "耐磨", "硬度", "是什麼", "特性", "特點"],
    "catalog": ["花色", "顏色", "色系", "色號", "系列", "目錄", "型錄", "款式", "花紋", "紋路"],
    "service": ["施工", "安裝", "熱彎", "拜訪", "配送", "運送", "丈量", "到府", "北部", "南部", "工期"],
    "store": ["商城", "購買", "下單", "訂購", "買", "哪裡買", "官網", "網站", "蝦皮"],
}


# ============================================================
# 快取分類（Keyword Matching）
# ============================================================

def _keyword_classify(message: str) -> dict | None:
    """
    用 keyword matching 快速分類簡單意圖。

    如果匹配成功，直接回傳結果（不呼叫 LLM）。
    如果無法匹配，回傳 None，交給 LLM 處理。
    """
    msg_lower = message.strip().lower()
    msg_clean = message.strip()

    # 1. 檢查 greeting（完全匹配）
    if msg_clean in _GREETING_EXACT or msg_lower in {g.lower() for g in _GREETING_EXACT}:
        return {
            "intent": "greeting",
            "confidence": 0.95,
            "reason": "關鍵字匹配：問候語",
        }

    # 2. 檢查 greeting（正則匹配）
    for pattern in _GREETING_PATTERNS:
        if re.match(pattern, msg_clean, re.IGNORECASE):
            return {
                "intent": "greeting",
                "confidence": 0.92,
                "reason": "模式匹配：問候語",
            }

    # 3. 檢查 transfer
    for keyword in _TRANSFER_KEYWORDS:
        if keyword in msg_clean:
            return {
                "intent": "transfer",
                "confidence": 0.95,
                "reason": f"關鍵字匹配：「{keyword}」",
            }

    # 4. 檢查其他意圖的強匹配（訊息很短且只包含單一意圖關鍵字）
    if len(msg_clean) <= 15:
        for intent, keywords in _INTENT_KEYWORD_HINTS.items():
            for kw in keywords:
                if kw in msg_clean:
                    return {
                        "intent": intent,
                        "confidence": 0.80,
                        "reason": f"關鍵字匹配：「{kw}」",
                    }

    # 無法快速分類
    return None


# ============================================================
# LLM 意圖分類
# ============================================================

def _llm_classify(message: str) -> dict:
    """
    呼叫 LLM API 做 zero-shot 意圖分類。

    回傳 {intent, confidence, reason}
    """
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"客戶訊息：「{message}」"},
        ],
        "temperature": 0.1,  # 低溫度，讓分類更穩定
        "max_tokens": 200,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                LLM_API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                wait_time = RETRY_DELAY * attempt
                print(f"  ⚠️  意圖分類 API 限速，等待 {wait_time} 秒...")
                time.sleep(wait_time)
                continue

            if response.status_code != 200:
                print(f"  ❌ 意圖分類 API 錯誤 {response.status_code}: {response.text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                raise Exception(f"意圖分類 API 錯誤 {response.status_code}")

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # 解析 LLM 回傳的 JSON
            return _parse_intent_response(content)

        except requests.exceptions.Timeout:
            print(f"  ⏱️  意圖分類請求超時（第 {attempt}/{MAX_RETRIES} 次）")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            raise

        except requests.exceptions.ConnectionError:
            print(f"  🔌 連線錯誤（第 {attempt}/{MAX_RETRIES} 次）")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            raise

    raise Exception(f"意圖分類重試 {MAX_RETRIES} 次後仍然失敗")


def _parse_intent_response(content: str) -> dict:
    """
    解析 LLM 回傳的意圖分類結果。

    嘗試提取 JSON，如果解析失敗則回傳 other。
    """
    valid_intents = {"pricing", "spec", "catalog", "service", "store", "greeting", "transfer", "other"}

    try:
        # 嘗試直接解析 JSON
        data = json.loads(content)
    except json.JSONDecodeError:
        # 嘗試從回應中提取 JSON 部分
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                return {"intent": "other", "confidence": 0.3, "reason": "LLM 回應解析失敗"}
        else:
            return {"intent": "other", "confidence": 0.3, "reason": "LLM 回應格式異常"}

    # 驗證並規範化結果
    intent = data.get("intent", "other")
    if intent not in valid_intents:
        intent = "other"

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    reason = data.get("reason", "")

    return {
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
    }


# ============================================================
# 公開 API
# ============================================================

def classify(message: str) -> dict:
    """
    意圖辨識主入口。

    優先用 keyword matching 快速分類，失敗再呼叫 LLM。

    參數：
        message: 客戶訊息文字

    回傳：
        {
            "intent": str,        # 意圖類別
            "confidence": float,  # 信心度 0.0~1.0
            "reason": str,        # 分類原因
        }
    """
    if not message or not message.strip():
        return {"intent": "other", "confidence": 0.0, "reason": "空訊息"}

    message = message.strip()

    # 第一步：keyword matching 快取
    quick_result = _keyword_classify(message)
    if quick_result is not None:
        return quick_result

    # 第二步：呼叫 LLM 分類
    try:
        return _llm_classify(message)
    except Exception as e:
        print(f"  ❌ LLM 意圖分類失敗：{e}")
        return {"intent": "other", "confidence": 0.2, "reason": f"分類錯誤：{str(e)}"}


# ============================================================
# 測試
# ============================================================

if __name__ == "__main__":
    print("🧪 意圖辨識模組測試\n")

    test_messages = [
        "你好",
        "嗨嗨",
        "你是誰",
        "繆思岩一坪多少錢？",
        "有什麼花色可以選？",
        "繆思岩是什麼材質？",
        "可以到北部施工嗎？",
        "石材桌在哪裡買？",
        "我要找真人客服",
        "我要投訴",
        "今天天氣怎麼樣",
        "高奢系列有哪些顏色",
    ]

    for msg in test_messages:
        result = classify(msg)
        print(f"📩 「{msg}」")
        print(f"   → 意圖：{result['intent']}  |  信心度：{result['confidence']:.2f}  |  原因：{result['reason']}")
        print()
