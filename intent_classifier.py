"""
繆思精工客服系統 — 意圖辨識模組

功能：
1. 用 keyword scoring 快速分類（優先，無長度限制）
2. 複雜意圖呼叫 LLM 做 zero-shot classification
3. 回傳 {intent, confidence, reason, sub_intents, identity_hint}

支援的意圖（15 個）：
- pricing:   詢價、報價相關
- spec:      查規格、尺寸、特性
- catalog:   查花色、色系、產品目錄
- service:   施工服務、安裝、配送
- store:     商城產品、購買
- greeting:  打招呼、自我介紹
- transfer:  需要轉人工
- identity:  用戶自報身分
- visit:     拜訪倉庫、看實品
- hot_bend:  熱彎加工相關
- basin:     一體盆相關
- tv_wall:   電視牆相關
- material:  材質成分、製程
- promotion: 優惠、折扣、活動
- other:     無法歸類

複合意圖：
  sub_intents 欄位列出同時觸發的次要意圖（最多 3 個）
  identity_hint 欄位標示偵測到的身分（designer / manufacturer / owner / None）
"""

import json
import re
import requests
import time
from typing import Optional

from rag_config import (
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    INTENT_SYSTEM_PROMPT,
    MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT,
)


# ============================================================
# 意圖關鍵字（scoring 用）
# ============================================================

# priority 越小越優先（同分時作為 tie-breaker）
_INTENT_PRIORITY = {
    "catalog":   0,
    "hot_bend":  1,
    "basin":     1,
    "tv_wall":   1,
    "visit":     2,
    "pricing":   2,
    "material":  3,
    "promotion": 3,
    "spec":      4,
    "service":   4,
    "store":     5,
    "identity":  6,
    "greeting":  7,
    "transfer":  8,
    "other":     9,
}

_INTENT_KEYWORDS = {
    "pricing": [
        "多少錢", "價格", "報價", "費用", "一坪", "每坪", "價位",
        "成本", "預算", "貴不貴", "便宜", "詢價", "估價",
        "怎麼算", "計費", "工錢", "一才報價",
    ],
    "spec": [
        "規格", "尺寸", "厚度", "重量", "防火", "防潮", "抗刮",
        "耐磨", "硬度", "特性", "特點", "有什麼優點",
        "幾毫米", "板厚", "承重", "最大多大", "最小多小",
    ],
    "catalog": [
        "花色", "顏色", "色系", "色號", "系列", "目錄", "型錄",
        "款式", "花紋", "紋路", "高奢系列", "有哪些系列", "有哪些款",
        "有什麼顏色", "樣本",
    ],
    "service": [
        "施工", "安裝", "配送", "運送", "丈量", "到府",
        "工期", "施作", "施工方式", "工班", "師傅",
    ],
    "store": [
        "商城", "購買", "下單", "訂購", "哪裡買", "官網",
        "網站", "蝦皮",
    ],
    "greeting": [
        "你好", "您好", "嗨", "哈囉", "早安", "午安", "晚安",
        "安安", "在嗎", "有人嗎",
    ],
    "transfer": [
        "找真人", "真人客服", "轉人工", "人工客服", "人工服務",
        "轉接", "投訴", "客訴", "申訴", "找客服",
        "不要機器人", "不要AI",
    ],
    "identity": [
        "我是設計師", "室內設計師", "我是做室內", "做設計的",
        "我是廠商", "代理商", "我是屋主", "我家裡", "自己住",
        "我在做設計", "我做設計", "設計公司", "以設計師",
        "我是建築師",
    ],
    "visit": [
        "倉庫", "看實品", "來看", "想來", "能來", "可以來",
        "參觀", "拜訪", "台南看", "工廠看", "看看實體", "看實物",
        "過來看", "去台南", "到台南", "到你們那",
    ],
    "hot_bend": [
        "熱彎", "弧形板", "圓弧板", "彎曲岩板", "熱彎岩板",
        "熱彎大板", "R角", "熱彎費", "做熱彎", "熱彎加工",
        "圓弧", "熱彎板", "一才",
    ],
    "basin": [
        "一體盆", "洗手台", "整體盆", "石材盆", "岩板盆",
        "一體式", "浴室盆", "洗臉盆",
    ],
    "tv_wall": [
        "電視牆", "TV牆", "背景牆", "電視背景", "電視後面的牆",
        "電視牆施工", "電視牆費用", "電視牆尺寸", "電視後方",
        "客廳背景", "主牆",
    ],
    "material": [
        "材質", "成分", "石英石", "長石", "燒結", "是什麼做",
        "怎麼做的", "原料", "製程", "繆思岩是什麼", "人造石",
        "天然石", "比較大理石",
    ],
    "promotion": [
        "優惠", "折扣", "特價", "活動", "促銷", "團購",
        "有沒有優惠", "打折", "有優惠嗎", "特惠",
    ],
}

# 身分子類型關鍵字（用於 identity_hint）
_IDENTITY_SUBTYPE = {
    "designer": [
        "設計師", "室內設計", "建築師", "設計公司", "設計行",
        "室內設計師", "做設計", "我是設計",
    ],
    "manufacturer": [
        "廠商", "代理商", "代理", "批發商", "進貨", "廠家",
        "合作廠", "商家", "我是廠",
    ],
    "owner": [
        "屋主", "自住", "自己住", "我家裡", "自己裝修", "自宅", "我家",
    ],
}

# greeting 完全匹配（快速路徑，不進入 scoring）
_GREETING_EXACT = {
    "你好", "您好", "嗨", "哈囉", "hi", "hello", "hey",
    "早安", "午安", "晚安", "安安", "嗨嗨", "哈嘍",
    "請問", "你好呀", "您好呀",
}

_GREETING_PATTERNS = [
    r"^(你好|您好|嗨|哈囉|hi|hello)[\s，。！!？?]*$",
    r"^你是誰[\s？?]*$",
    r"^(在嗎|有人嗎)[\s？?]*$",
]

# transfer 完全匹配（快速路徑，不進入 scoring）
_TRANSFER_EXACT = [
    "找真人", "真人客服", "轉人工", "人工客服", "人工服務",
    "轉接", "投訴", "客訴", "申訴", "找客服",
    "不要機器人", "不要AI", "不要 AI",
]

_VALID_INTENTS = {
    "pricing", "spec", "catalog", "service", "store",
    "greeting", "transfer", "identity", "visit",
    "hot_bend", "basin", "tv_wall", "material", "promotion", "other",
}


# ============================================================
# 評分引擎
# ============================================================

def _kw_score(kw):
    """關鍵字長度對應分值：>=4 字元 2 分，2-3 字元 1 分，1 字元跳過。"""
    l = len(kw)
    if l >= 4:
        return 2
    if l >= 2:
        return 1
    return 0


def _compute_scores(msg):
    """計算所有意圖的關鍵字匹配分數，回傳 {intent: score}。"""
    scores = {}
    for intent, keywords in _INTENT_KEYWORDS.items():
        total = 0
        for kw in keywords:
            w = _kw_score(kw)
            if w > 0 and kw in msg:
                total += w
        if total > 0:
            scores[intent] = total
    return scores


def _detect_identity_hint(msg):
    """從訊息偵測身分子類型，回傳 designer / manufacturer / owner / None。"""
    for identity, keywords in _IDENTITY_SUBTYPE.items():
        for kw in keywords:
            if kw in msg:
                return identity
    return None


# ============================================================
# 快取分類（Keyword Scoring）
# ============================================================

def _keyword_classify(message):
    """
    用 keyword scoring 快速分類意圖。無長度限制。

    回傳包含 sub_intents 和 identity_hint 的結果，或 None（無任何關鍵字命中時）。
    """
    msg_lower = message.strip().lower()
    msg_clean = message.strip()

    # 1. greeting 完全匹配（最高優先）
    if msg_clean in _GREETING_EXACT or msg_lower in {g.lower() for g in _GREETING_EXACT}:
        return {
            "intent": "greeting",
            "confidence": 0.95,
            "reason": "關鍵字匹配：問候語",
            "sub_intents": [],
            "identity_hint": None,
        }

    # 2. greeting 正則匹配
    for pattern in _GREETING_PATTERNS:
        if re.match(pattern, msg_clean, re.IGNORECASE):
            return {
                "intent": "greeting",
                "confidence": 0.92,
                "reason": "模式匹配：問候語",
                "sub_intents": [],
                "identity_hint": None,
            }

    # 3. transfer 關鍵字（最高優先，直接回傳）
    for kw in _TRANSFER_EXACT:
        if kw in msg_clean:
            return {
                "intent": "transfer",
                "confidence": 0.95,
                "reason": f"關鍵字匹配：「{kw}」",
                "sub_intents": [],
                "identity_hint": None,
            }

    # 4. 全意圖 scoring
    scores = _compute_scores(msg_clean)
    if not scores:
        return None

    # 排序：分數高者優先，同分時 priority 小者（更高優先）
    ranked = sorted(
        scores.items(),
        key=lambda x: (-x[1], _INTENT_PRIORITY.get(x[0], 9)),
    )

    primary_intent, primary_score = ranked[0]
    sub_intents = [intent for intent, _ in ranked[1:4] if intent != primary_intent]
    identity_hint = _detect_identity_hint(msg_clean)

    # 信心度：基礎 0.60，每分 +0.07，上限 0.95
    confidence = min(0.95, 0.60 + primary_score * 0.07)

    return {
        "intent": primary_intent,
        "confidence": round(confidence, 2),
        "reason": f"關鍵字評分：「{primary_intent}」得 {primary_score} 分",
        "sub_intents": sub_intents,
        "identity_hint": identity_hint,
    }


# ============================================================
# LLM 意圖分類
# ============================================================

def _llm_classify(message):
    """
    呼叫 LLM API 做 zero-shot 意圖分類。

    回傳 {intent, confidence, reason, sub_intents, identity_hint}
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
        "temperature": 0.1,
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


def _parse_intent_response(content):
    """
    解析 LLM 回傳的意圖分類結果。

    嘗試提取 JSON，如果解析失敗則回傳 other。
    """
    _fallback = {"intent": "other", "confidence": 0.3, "reason": "LLM 回應解析失敗",
                 "sub_intents": [], "identity_hint": None}

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                return _fallback
        else:
            return {**_fallback, "reason": "LLM 回應格式異常"}

    intent = data.get("intent", "other")
    if intent not in _VALID_INTENTS:
        intent = "other"

    confidence = data.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    reason = data.get("reason", "")

    sub_intents = data.get("sub_intents", [])
    if not isinstance(sub_intents, list):
        sub_intents = []
    sub_intents = [s for s in sub_intents if s in _VALID_INTENTS and s != intent]

    identity_hint = data.get("identity_hint")
    if identity_hint not in ("designer", "manufacturer", "owner", None):
        identity_hint = None

    return {
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "sub_intents": sub_intents,
        "identity_hint": identity_hint,
    }


# ============================================================
# 公開 API
# ============================================================

def classify(message):
    """
    意圖辨識主入口。

    優先用 keyword scoring 快速分類，失敗再呼叫 LLM。

    參數：
        message: 客戶訊息文字

    回傳：
        {
            "intent":        str,       # 意圖類別
            "confidence":    float,     # 信心度 0.0~1.0
            "reason":        str,       # 分類原因
            "sub_intents":   list,      # 複合意圖（次要意圖清單）
            "identity_hint": str|None,  # 偵測到的身分提示
        }
    """
    if not message or not message.strip():
        return {
            "intent": "other", "confidence": 0.0, "reason": "空訊息",
            "sub_intents": [], "identity_hint": None,
        }

    message = message.strip()

    # 第一步：keyword scoring 快取
    quick_result = _keyword_classify(message)
    if quick_result is not None:
        return quick_result

    # 第二步：呼叫 LLM 分類
    try:
        return _llm_classify(message)
    except Exception as e:
        print(f"  ❌ LLM 意圖分類失敗：{e}")
        return {
            "intent": "other", "confidence": 0.2, "reason": f"分類錯誤：{str(e)}",
            "sub_intents": [], "identity_hint": None,
        }


# ============================================================
# 測試
# ============================================================

if __name__ == "__main__":
    print("🧪 意圖辨識模組測試\n")

    test_cases = [
        # (訊息, 預期意圖, 預期身分提示)
        ("我是做室內設計的",       "identity", "designer"),
        ("熱彎一才多少錢",         "hot_bend", None),
        ("有一體盆的型錄嗎",       "catalog",  None),
        ("你們倉庫在哪",           "visit",    None),
        ("電視牆可以做到 120 吋嗎", "tv_wall",  None),
        ("你好",                   "greeting", None),
        ("繆思岩一坪多少錢？",     "pricing",  None),
        ("有什麼花色可以選？",     "catalog",  None),
        ("可以到北部施工嗎？",     "service",  None),
        ("石材桌在哪裡買？",       "store",    None),
        ("我要找真人客服",         "transfer", None),
        ("有折扣嗎",               "promotion", None),
        ("繆思岩是什麼材質做的",   "material", None),
    ]

    passed = 0
    for msg, expected_intent, expected_hint in test_cases:
        result = classify(msg)
        intent_ok = result["intent"] == expected_intent
        hint_ok = (expected_hint is None) or (result["identity_hint"] == expected_hint)
        ok = intent_ok and hint_ok
        passed += ok

        status = "✅" if ok else "❌"
        print(f"{status} 「{msg}」")
        print(f"   → 意圖：{result['intent']}（預期：{expected_intent}）  信心度：{result['confidence']:.2f}")
        if result["sub_intents"]:
            print(f"   📎 副意圖：{result['sub_intents']}")
        if result["identity_hint"] or expected_hint:
            print(f"   👤 身分提示：{result['identity_hint']}（預期：{expected_hint}）")
        if not ok:
            print(f"   ⚠️  原因：{result['reason']}")
        print()

    print(f"📊 通過 {passed}/{len(test_cases)}")
