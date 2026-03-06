"""
繆思精工客服系統 — 用戶標籤系統

標籤類別：
  身分：homeowner / vendor / designer
  產品：tv_wall / basin / hot_bend / countertop
  狀態：visited / quoted / ordered / inactive
  區域：north / central / south / east
"""

import logging
import sqlite3
from datetime import datetime

from error_handler import DB_PATH

logger = logging.getLogger(__name__)

# ── 有效標籤定義 ──

VALID_TAGS = {
    "identity": ["homeowner", "vendor", "designer"],
    "product":  ["tv_wall", "basin", "hot_bend", "countertop"],
    "status":   ["visited", "quoted", "ordered", "inactive"],
    "region":   ["north", "central", "south", "east"],
}

ALL_VALID_TAGS: set[str] = set()
for _tags in VALID_TAGS.values():
    ALL_VALID_TAGS.update(_tags)

TAG_LABELS = {
    "homeowner": "屋主", "vendor": "廠商", "designer": "設計師",
    "tv_wall": "電視牆", "basin": "一體盆", "hot_bend": "熱彎", "countertop": "檯面",
    "visited": "已到訪", "quoted": "已報價", "ordered": "已下單", "inactive": "沉寂",
    "north": "北部", "central": "中部", "south": "南部", "east": "東部",
}

# ── 自動打標規則（關鍵字） ──

_IDENTITY_MAP = {"designer": "designer", "manufacturer": "vendor", "owner": "homeowner"}

_PRODUCT_KW = {
    "tv_wall":    ["電視牆", "TV牆", "背景牆", "電視背景"],
    "basin":      ["一體盆", "洗手台", "整體盆", "石材盆", "岩板盆"],
    "hot_bend":   ["熱彎", "弧形板", "圓弧板", "彎曲岩板"],
    "countertop": ["檯面", "廚房檯面", "中島", "流理台", "吧台"],
}

_REGION_KW = {
    "north":   ["台北", "新北", "基隆", "宜蘭"],
    "central": ["桃園", "新竹", "苗栗", "台中", "彰化", "南投"],
    "south":   ["雲林", "嘉義", "台南", "高雄", "屏東"],
    "east":    ["花蓮", "台東"],
}

_STATUS_KW = {
    "quoted": ["報價", "估價", "報個價", "幫我算"],
}


# ── SQLite ──

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_tags (
            user_id   TEXT NOT NULL,
            tag       TEXT NOT NULL,
            tagged_at TEXT NOT NULL,
            tagged_by TEXT NOT NULL DEFAULT 'manual',
            PRIMARY KEY (user_id, tag)
        )
    """)
    conn.commit()
    return conn


# ── CRUD ──

def get_tags(user_id: str) -> list[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT tag, tagged_at, tagged_by FROM user_tags WHERE user_id = ? ORDER BY tagged_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return [{"tag": r[0], "tagged_at": r[1], "tagged_by": r[2]} for r in rows]


def add_tag(user_id: str, tag: str, tagged_by: str = "manual") -> bool:
    if tag not in ALL_VALID_TAGS:
        return False
    conn = _get_db()
    conn.execute(
        "INSERT OR IGNORE INTO user_tags (user_id, tag, tagged_at, tagged_by) VALUES (?, ?, ?, ?)",
        (user_id, tag, datetime.now().isoformat(), tagged_by),
    )
    conn.commit()
    conn.close()
    return True


def remove_tag(user_id: str, tag: str) -> bool:
    conn = _get_db()
    cur = conn.execute("DELETE FROM user_tags WHERE user_id = ? AND tag = ?", (user_id, tag))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_users_by_tags(tags: list[str], match_mode: str = "any") -> list[str]:
    """回傳符合條件的 user_id 列表。match_mode: 'any' 任一符合 / 'all' 全部符合。"""
    if not tags:
        return []
    conn = _get_db()
    ph = ",".join("?" * len(tags))

    if match_mode == "all":
        rows = conn.execute(
            f"SELECT user_id FROM user_tags WHERE tag IN ({ph}) "
            f"GROUP BY user_id HAVING COUNT(DISTINCT tag) = ?",
            (*tags, len(tags)),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT DISTINCT user_id FROM user_tags WHERE tag IN ({ph})", tags
        ).fetchall()

    conn.close()
    return [r[0] for r in rows]


# ── 自動打標 ──

def auto_tag_from_message(user_id: str, message: str, identity: str | None = None) -> list[str]:
    """根據訊息內容和身分自動打標。只用關鍵字規則，不呼叫 LLM。"""
    added = []

    if identity and identity in _IDENTITY_MAP:
        if add_tag(user_id, _IDENTITY_MAP[identity], "ai"):
            added.append(_IDENTITY_MAP[identity])

    for tag, kws in _PRODUCT_KW.items():
        if any(kw in message for kw in kws):
            if add_tag(user_id, tag, "ai"):
                added.append(tag)

    for tag, kws in _REGION_KW.items():
        if any(kw in message for kw in kws):
            if add_tag(user_id, tag, "ai"):
                added.append(tag)

    for tag, kws in _STATUS_KW.items():
        if any(kw in message for kw in kws):
            if add_tag(user_id, tag, "ai"):
                added.append(tag)

    return added
