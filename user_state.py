"""
繆思精工客服系統 — 用戶身分識別與對話狀態機

狀態定義：
  new               新用戶，身分未知
  identified        身分已知，一般對話
  inquiring_product 正在詢問特定產品（電視牆 / 熱彎 / 一體盆）
  inquiring_visit   正在詢問拜訪或看樣品
  pending_info      等待用戶提供資訊（尺寸圖 / 案場位置等）

身分定義（與 scripted_responses.json 一致）：
  owner        屋主
  designer     設計師
  manufacturer 廠商
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

from error_handler import DB_PATH

# ============================================================
# 常數
# ============================================================

VALID_IDENTITIES = ("owner", "designer", "manufacturer")
VALID_STATES = ("new", "identified", "inquiring_product", "inquiring_visit", "pending_info")
VALID_PRODUCTS = ("tv_wall", "hot_bend", "basin")

# 每個追問最多發出幾次（防止打擾用戶）
MAX_PROBE_COUNT = 2

# 身分關鍵字（順序：精確詞優先）
IDENTITY_KEYWORDS = {
    "designer": [
        "設計師", "室內設計師", "室內設計", "建築師", "設計公司", "設計行", "我是設計",
    ],
    "manufacturer": [
        "廠商", "代理商", "代理", "批發商", "進貨", "廠家", "我是廠", "合作廠", "商家",
    ],
    "owner": [
        "屋主", "自住", "自己住", "我家裡", "自己裝修", "自宅", "我家",
    ],
}

# 產品關鍵字 → product_focus 子狀態
PRODUCT_KEYWORDS = {
    "tv_wall": ["電視牆", "TV牆", "背景牆", "電視背景", "電視後面的牆"],
    "hot_bend": ["熱彎", "弧形板", "圓弧板", "彎曲岩板", "熱彎岩板", "熱彎大板"],
    "basin":   ["一體盆", "洗手台", "整體盆", "石材盆", "岩板盆", "一體式"],
}

# 拜訪 / 看樣品意圖關鍵字
VISIT_KEYWORDS = [
    "倉庫", "看實品", "來看", "想來", "能來", "可以來", "參觀", "拜訪",
    "台南看", "工廠看", "看看實體", "看實物",
]

# 報價 / 估價意圖關鍵字
QUOTE_KEYWORDS = [
    "報價", "詢價", "幫我估", "算價格", "估價", "要報價", "我要報",
    "報個價", "給個價", "估個價",
]

# 用戶提供資訊的關鍵字（表示 pending_info 可轉回 identified）
INFO_PROVIDED_KEYWORDS = [
    "圖紙", "尺寸圖", "設計圖", "平面圖", "施工圖", "這是圖", "傳圖給你",
    "已傳", "圖傳給", "縣市", "台北", "台中", "台南", "高雄", "新北",
    "桃園", "新竹", "苗栗", "彰化", "南投", "雲林", "嘉義", "屏東",
    "宜蘭", "花蓮", "台東", "澎湖",
]

# 追問模板 (state, product_focus, probe_key) → 追問文字（優先使用語錄原文）
PROBE_TEMPLATES: dict[tuple, str] = {
    ("inquiring_product", "tv_wall",  "location"):      "請問您的案場在哪個縣市呢？",
    ("inquiring_product", "tv_wall",  "building_type"): "案場是透天、公寓還是大樓呢？方便讓我們評估搬運方式～",
    ("inquiring_product", "hot_bend", "r_angle"):       "請問 R 角大小大概是多少呢？",
    ("inquiring_product", "hot_bend", "drawing"):       "有尺寸圖的話可以提供唷，我們可以幫您精準評估！",
    ("inquiring_product", "basin",    "size"):          "請問有特別想要的尺寸嗎？單盆還是雙盆呢？",
    ("inquiring_visit",   None,       "region"):        "請問您在哪個區域呢？方便的時間是？",
}

# 每個 state 應依序追問的 probe_key
PROBE_SEQUENCE = {
    ("inquiring_product", "tv_wall"):  ["location", "building_type"],
    ("inquiring_product", "hot_bend"): ["r_angle", "drawing"],
    ("inquiring_product", "basin"):    ["size"],
    ("inquiring_visit",   None):       ["region"],
}


# ============================================================
# SQLite 操作
# ============================================================

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id       TEXT PRIMARY KEY,
            identity      TEXT DEFAULT NULL,
            state         TEXT NOT NULL DEFAULT 'new',
            product_focus TEXT NOT NULL DEFAULT '[]',
            probe_counts  TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_state(user_id: str) -> dict:
    """
    取得用戶狀態。首次呼叫時自動以 state='new' 建立記錄。

    回傳：
        {
            "identity":      "str | None",
            "state":         "str",
            "product_focus": "list[str]",
            "probe_counts":  "dict[str, int]",
        }
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT identity, state, product_focus, probe_counts "
        "FROM user_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    if row is None:
        _create_user(user_id)
        return {
            "identity": None,
            "state": "new",
            "product_focus": [],
            "probe_counts": {},
        }

    return {
        "identity":      row[0],
        "state":         row[1],
        "product_focus": json.loads(row[2]),
        "probe_counts":  json.loads(row[3]),
    }


def _create_user(user_id: str):
    now = datetime.now().isoformat()
    conn = _get_db()
    conn.execute(
        "INSERT OR IGNORE INTO user_state "
        "(user_id, identity, state, product_focus, probe_counts, created_at, updated_at) "
        "VALUES (?, NULL, 'new', '[]', '{}', ?, ?)",
        (user_id, now, now),
    )
    conn.commit()
    conn.close()


def update_state(user_id: str, **kwargs):
    """
    更新用戶狀態的指定欄位。

    支援欄位：identity / state / product_focus / probe_counts
    product_focus 和 probe_counts 接受 Python list / dict，自動序列化。
    """
    allowed = {"identity", "state", "product_focus", "probe_counts"}
    fields, values = [], []

    for key, val in kwargs.items():
        if key not in allowed:
            continue
        if key in ("product_focus", "probe_counts"):
            val = json.dumps(val, ensure_ascii=False)
        fields.append(f"{key} = ?")
        values.append(val)

    if not fields:
        return

    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(user_id)

    conn = _get_db()
    conn.execute(
        f"UPDATE user_state SET {', '.join(fields)} WHERE user_id = ?",
        values,
    )
    conn.commit()
    conn.close()


# ============================================================
# 偵測邏輯
# ============================================================

def detect_identity(message: str) -> Optional[str]:
    """
    從訊息中偵測身分關鍵字。

    回傳 "owner" / "designer" / "manufacturer"，或 None。
    精確詞優先：先比對長詞，再比對短詞，避免誤判。
    """
    msg = message.strip()
    for identity, keywords in IDENTITY_KEYWORDS.items():
        for kw in keywords:
            if kw in msg:
                return identity
    return None


def detect_product_focus(message: str) -> list:
    """從訊息中偵測提及的產品，回傳新增的 product_focus list（可能為空）。"""
    msg = message.strip()
    found = []
    for product, keywords in PRODUCT_KEYWORDS.items():
        for kw in keywords:
            if kw in msg:
                found.append(product)
                break
    return found


def detect_visit_intent(message: str) -> bool:
    """偵測是否有拜訪 / 看實品意圖。"""
    return any(kw in message for kw in VISIT_KEYWORDS)


def detect_quote_intent(message: str) -> bool:
    """偵測是否有報價 / 估價需求。"""
    return any(kw in message for kw in QUOTE_KEYWORDS)


def detect_info_provided(message: str) -> bool:
    """偵測是否用戶正在提供資訊（尺寸圖 / 縣市地點等）。"""
    return any(kw in message for kw in INFO_PROVIDED_KEYWORDS)


# ============================================================
# 狀態轉移
# ============================================================

def compute_transition(message: str, current: dict) -> dict:
    """
    根據訊息和當前狀態，計算需要更新的欄位。

    優先順序：
      1. 產品關鍵字  → inquiring_product（累加 product_focus）
      2. 拜訪意圖    → inquiring_visit
      3. 報價意圖    → pending_info
      4. 提供資訊    → identified（從 pending_info 回升）

    回傳 dict（只包含有變化的欄位），無變化時回傳空 dict。
    """
    updates: dict = {}
    current_state = current.get("state", "new")
    current_focus = set(current.get("product_focus") or [])

    # 1. 產品關鍵字偵測
    new_products = detect_product_focus(message)
    if new_products:
        merged = list(current_focus | set(new_products))
        if set(merged) != current_focus:
            updates["product_focus"] = merged
        updates["state"] = "inquiring_product"
        return updates

    # 2. 拜訪意圖
    if detect_visit_intent(message):
        if current_state != "inquiring_visit":
            updates["state"] = "inquiring_visit"
        return updates

    # 3. 報價意圖
    if detect_quote_intent(message):
        if current_state != "pending_info":
            updates["state"] = "pending_info"
        return updates

    # 4. 用戶提供資訊 → 從 pending_info 回升
    if current_state == "pending_info" and detect_info_provided(message):
        updates["state"] = "identified"
        return updates

    return updates


# ============================================================
# 追問邏輯
# ============================================================

def get_next_probe(current: dict) -> Optional[tuple]:
    """
    根據當前狀態決定下一個追問。

    同一問題最多追問 MAX_PROBE_COUNT 次，超過不再問。

    回傳 (追問文字, probe_key)，無需追問時回傳 None。
    """
    state = current.get("state", "new")
    focus_list = current.get("product_focus") or []
    probe_counts = current.get("probe_counts") or {}

    if state == "inquiring_product" and focus_list:
        product = focus_list[0]
        sequence = PROBE_SEQUENCE.get(("inquiring_product", product), [])
        for probe_key in sequence:
            count_key = f"{product}_{probe_key}"
            if probe_counts.get(count_key, 0) < MAX_PROBE_COUNT:
                text = PROBE_TEMPLATES.get(("inquiring_product", product, probe_key))
                if text:
                    return text, count_key

    elif state == "inquiring_visit":
        sequence = PROBE_SEQUENCE.get(("inquiring_visit", None), [])
        for probe_key in sequence:
            count_key = f"visit_{probe_key}"
            if probe_counts.get(count_key, 0) < MAX_PROBE_COUNT:
                text = PROBE_TEMPLATES.get(("inquiring_visit", None, probe_key))
                if text:
                    return text, count_key

    return None


def increment_probe(user_id: str, probe_key: str, current_counts: dict):
    """DB 中遞增 probe_key 的計數。"""
    updated = dict(current_counts)
    updated[probe_key] = updated.get(probe_key, 0) + 1
    update_state(user_id, probe_counts=updated)


# ============================================================
# 測試
# ============================================================

if __name__ == "__main__":
    print("🧪 user_state 模組測試\n")

    uid = "test_state_001"
    # 清掉舊記錄方便重複測試
    conn = _get_db()
    conn.execute("DELETE FROM user_state WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()

    s = get_state(uid)
    print(f"初始狀態：{s}")

    print(f"\n偵測身分：")
    for msg in ["我是設計師", "我是廠商", "屋主啦", "你好"]:
        print(f"  「{msg}」→ {detect_identity(msg)}")

    print(f"\n偵測產品：")
    for msg in ["想了解電視牆", "問一下熱彎費用", "一體盆怎麼選", "你好請問"]:
        print(f"  「{msg}」→ {detect_product_focus(msg)}")

    print(f"\n狀態轉移測試：")
    current = {"state": "identified", "product_focus": [], "probe_counts": {}}
    for msg in ["我想了解電視牆", "你們倉庫在哪", "幫我報個價"]:
        t = compute_transition(msg, current)
        print(f"  「{msg}」→ {t}")

    print(f"\n追問測試：")
    current = {"state": "inquiring_product", "product_focus": ["tv_wall"], "probe_counts": {}}
    for _ in range(3):
        result = get_next_probe(current)
        if result:
            text, key = result
            print(f"  追問：{text}  (key={key})")
            current["probe_counts"][key] = current["probe_counts"].get(key, 0) + 1
        else:
            print("  無更多追問")
