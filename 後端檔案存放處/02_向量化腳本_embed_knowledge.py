"""
繆思精工 RAG 知識庫引擎 — 知識庫向量化腳本

功能：
1. 讀取 knowledge-base.csv（108 筆知識庫資料）
2. 將每筆資料組合成可搜尋的文本
3. 呼叫 OpenRouter Embedding API 產生向量
4. 輸出 knowledge-vectors.json（本地向量資料庫）

使用方式：
    python embed_knowledge.py
"""

import csv
import json
import time
import requests
import sys
import os

# 載入設定
from rag_config import (
    EMBEDDING_API_KEY, EMBEDDING_API_URL, EMBEDDING_MODEL,
    KNOWLEDGE_CSV_PATH, VECTORS_OUTPUT_PATH,
    BATCH_SIZE, MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT
)


def read_knowledge_base(csv_path: str) -> list[dict]:
    """
    讀取知識庫 CSV 檔案，回傳結構化資料列表。
    
    每筆資料包含：編號、大分類、小分類、標題、內容、尺寸、工藝、連結、來源、備註
    """
    records = []
    
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append({
                "id": row.get("編號", "").strip(),
                "category": row.get("大分類", "").strip(),
                "subcategory": row.get("小分類", "").strip(),
                "title": row.get("標題", "").strip(),
                "content": row.get("內容", "").strip(),
                "sizes": row.get("可用尺寸", "").strip(),
                "surface": row.get("表面工藝", "").strip(),
                "link": row.get("相關連結", "").strip(),
                "source": row.get("資料來源", "").strip(),
                "note": row.get("備註", "").strip(),
            })
    
    return records


def compose_searchable_text(record: dict) -> str:
    """
    將一筆知識庫資料組合成可搜尋的文本。
    
    把所有欄位拼成一段自然語言，方便 embedding 模型理解語意。
    """
    parts = []
    
    # 分類資訊
    if record["category"]:
        parts.append(f"分類：{record['category']}")
    if record["subcategory"]:
        parts.append(f"子分類：{record['subcategory']}")
    
    # 標題和內容（主要資訊）
    if record["title"]:
        parts.append(f"標題：{record['title']}")
    if record["content"]:
        parts.append(f"內容：{record['content']}")
    
    # 補充資訊
    if record["sizes"]:
        parts.append(f"可用尺寸：{record['sizes']}")
    if record["surface"]:
        parts.append(f"表面工藝：{record['surface']}")
    if record["note"]:
        parts.append(f"備註：{record['note']}")
    
    return "\n".join(parts)


def call_embedding_api(texts: list[str]) -> list[list[float]]:
    """
    呼叫 OpenRouter Embedding API，將文本批次轉為向量。
    
    包含重試機制，處理 API 限速（429）和其他錯誤。
    回傳每段文字對應的向量列表。
    """
    headers = {
        "Authorization": f"Bearer {EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": EMBEDDING_MODEL,
        "input": texts,
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                EMBEDDING_API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            
            # API 限速 → 等待後重試
            if response.status_code == 429:
                wait_time = RETRY_DELAY * attempt
                print(f"  ⚠️  API 限速，等待 {wait_time} 秒後重試（第 {attempt}/{MAX_RETRIES} 次）")
                time.sleep(wait_time)
                continue
            
            # 其他 HTTP 錯誤
            if response.status_code != 200:
                print(f"  ❌ API 回傳錯誤 {response.status_code}: {response.text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    raise Exception(f"API 錯誤 {response.status_code}")
            
            # 解析回應
            result = response.json()
            
            if "data" not in result:
                print(f"  ❌ API 回應格式異常: {json.dumps(result, ensure_ascii=False)[:200]}")
                raise Exception("API 回應中缺少 data 欄位")
            
            # 按照 index 排序，確保順序正確
            sorted_data = sorted(result["data"], key=lambda x: x["index"])
            embeddings = [item["embedding"] for item in sorted_data]
            
            return embeddings
            
        except requests.exceptions.Timeout:
            print(f"  ⏱️  API 請求超時（第 {attempt}/{MAX_RETRIES} 次）")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            else:
                raise
                
        except requests.exceptions.ConnectionError:
            print(f"  🔌 連線錯誤（第 {attempt}/{MAX_RETRIES} 次）")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            else:
                raise
    
    raise Exception(f"重試 {MAX_RETRIES} 次後仍然失敗")


def main():
    """主程式：讀取 CSV → 產生向量 → 儲存 JSON"""
    
    print("=" * 60)
    print("🏛️  繆思精工 知識庫向量化工具")
    print("=" * 60)
    
    # ── 步驟 1：讀取知識庫 ──
    print(f"\n📂 讀取知識庫：{KNOWLEDGE_CSV_PATH}")
    
    if not os.path.exists(KNOWLEDGE_CSV_PATH):
        print(f"❌ 找不到檔案：{KNOWLEDGE_CSV_PATH}")
        sys.exit(1)
    
    records = read_knowledge_base(KNOWLEDGE_CSV_PATH)
    print(f"✅ 共讀取 {len(records)} 筆資料")
    
    # ── 步驟 2：組合可搜尋文本 ──
    print("\n📝 組合搜尋文本...")
    texts = []
    for record in records:
        text = compose_searchable_text(record)
        texts.append(text)
    
    print(f"✅ 已組合 {len(texts)} 段文本")
    
    # ── 步驟 3：批次呼叫 Embedding API ──
    print(f"\n🔄 開始向量化（批次大小：{BATCH_SIZE}）...")
    
    all_embeddings = []
    total_batches = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(texts), BATCH_SIZE):
        batch_num = i // BATCH_SIZE + 1
        batch_texts = texts[i:i + BATCH_SIZE]
        
        print(f"  [{batch_num}/{total_batches}] 處理第 {i+1}~{min(i+BATCH_SIZE, len(texts))} 筆...", end=" ", flush=True)
        
        embeddings = call_embedding_api(batch_texts)
        all_embeddings.extend(embeddings)
        
        print(f"✅ (向量維度: {len(embeddings[0])})")
        
        # 批次間短暫暫停，避免觸發限速
        if i + BATCH_SIZE < len(texts):
            time.sleep(0.5)
    
    print(f"\n✅ 向量化完成！共產生 {len(all_embeddings)} 個向量")
    
    # ── 步驟 4：組裝並儲存向量資料庫 ──
    print(f"\n💾 儲存向量資料庫：{VECTORS_OUTPUT_PATH}")
    
    vector_db = []
    for idx, record in enumerate(records):
        entry = {
            "id": record["id"],
            "text": texts[idx],
            "embedding": all_embeddings[idx],
            "metadata": {
                "category": record["category"],
                "subcategory": record["subcategory"],
                "title": record["title"],
                "content": record["content"],
                "sizes": record["sizes"],
                "surface": record["surface"],
                "link": record["link"],
                "source": record["source"],
                "note": record["note"],
            }
        }
        vector_db.append(entry)
    
    # 確保輸出目錄存在
    os.makedirs(os.path.dirname(VECTORS_OUTPUT_PATH), exist_ok=True)
    
    with open(VECTORS_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(vector_db, f, ensure_ascii=False, indent=2)
    
    # 檔案大小
    file_size = os.path.getsize(VECTORS_OUTPUT_PATH)
    size_mb = file_size / (1024 * 1024)
    
    print(f"✅ 儲存完成！檔案大小：{size_mb:.1f} MB")
    
    # ── 完成 ──
    print("\n" + "=" * 60)
    print("🎉 知識庫向量化完成！")
    print(f"   📊 資料筆數：{len(vector_db)}")
    print(f"   📐 向量維度：{len(all_embeddings[0])}")
    print(f"   💾 輸出檔案：{VECTORS_OUTPUT_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
