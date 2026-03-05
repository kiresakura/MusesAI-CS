"""
繆思精工 RAG 知識庫引擎 — RAG 搜尋引擎

功能：
1. 載入 knowledge-vectors.json 向量資料庫
2. 接收使用者問題 → 轉為向量 → 餘弦相似度搜尋
3. 找出最相關的知識庫條目 → 組裝 prompt → 呼叫 LLM 生成回答
4. 支援互動式 CLI 問答模式

使用方式：
    python rag_search.py                    # 互動模式
    python rag_search.py "你們有什麼花色？"  # 單次查詢模式
"""

import json
import sys
import numpy as np
import requests
import time

# 載入設定
from rag_config import (
    EMBEDDING_API_KEY, EMBEDDING_API_URL, EMBEDDING_MODEL,
    LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    VECTORS_OUTPUT_PATH, SYSTEM_PROMPT,
    TOP_K, SIMILARITY_THRESHOLD,
    MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT
)


# ============================================================
# 向量資料庫載入
# ============================================================

def load_vector_db(path: str) -> list[dict]:
    """
    載入向量資料庫（knowledge-vectors.json）。
    
    回傳結構：[{id, text, embedding(numpy array), metadata}]
    """
    print(f"📂 載入向量資料庫：{path}")
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # 將 embedding 轉為 numpy array，加速後續計算
    for item in data:
        item["embedding"] = np.array(item["embedding"], dtype=np.float32)
    
    print(f"✅ 已載入 {len(data)} 筆資料（向量維度：{len(data[0]['embedding'])}）")
    return data


# ============================================================
# Embedding 查詢
# ============================================================

def get_query_embedding(query: str) -> np.ndarray:
    """
    將使用者問題轉為向量。
    
    呼叫 OpenRouter Embedding API，包含重試機制。
    """
    headers = {
        "Authorization": f"Bearer {EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": EMBEDDING_MODEL,
        "input": [query],
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                EMBEDDING_API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            
            if response.status_code == 429:
                wait_time = RETRY_DELAY * attempt
                print(f"  ⚠️  API 限速，等待 {wait_time} 秒...")
                time.sleep(wait_time)
                continue
            
            if response.status_code != 200:
                print(f"  ❌ Embedding API 錯誤 {response.status_code}: {response.text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                raise Exception(f"Embedding API 錯誤 {response.status_code}")
            
            result = response.json()
            embedding = result["data"][0]["embedding"]
            return np.array(embedding, dtype=np.float32)
            
        except requests.exceptions.Timeout:
            print(f"  ⏱️  請求超時（第 {attempt}/{MAX_RETRIES} 次）")
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
    
    raise Exception(f"重試 {MAX_RETRIES} 次後仍然失敗")


# ============================================================
# 餘弦相似度搜尋
# ============================================================

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """計算兩個向量的餘弦相似度。"""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
    
    return float(dot / (norm_a * norm_b))


def search_similar(
    query_embedding: np.ndarray,
    vector_db: list[dict],
    top_k: int = TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict]:
    """
    在向量資料庫中搜尋最相似的條目。
    
    回傳 top_k 個相似度超過 threshold 的結果，
    每個結果包含原始資料 + similarity 分數。
    """
    results = []
    
    for item in vector_db:
        sim = cosine_similarity(query_embedding, item["embedding"])
        if sim >= threshold:
            results.append({
                "id": item["id"],
                "text": item["text"],
                "metadata": item["metadata"],
                "similarity": sim,
            })
    
    # 按相似度排序，取前 K 個
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:top_k]


# ============================================================
# LLM 回答生成
# ============================================================

def build_rag_prompt(question: str, contexts: list[dict]) -> list[dict]:
    """
    組裝 RAG prompt。
    
    結構：system prompt + 知識庫相關內容 + 使用者問題
    """
    # 組合知識庫上下文
    context_parts = []
    for i, ctx in enumerate(contexts, 1):
        meta = ctx["metadata"]
        part = f"【參考資料 {i}】（{meta['category']}"
        if meta["subcategory"]:
            part += f" > {meta['subcategory']}"
        part += f"）\n標題：{meta['title']}\n內容：{meta['content']}"
        if meta["sizes"]:
            part += f"\n可用尺寸：{meta['sizes']}"
        if meta["surface"]:
            part += f"\n表面工藝：{meta['surface']}"
        if meta["link"]:
            part += f"\n相關連結：{meta['link']}"
        if meta["note"]:
            part += f"\n備註：{meta['note']}"
        context_parts.append(part)
    
    knowledge_text = "\n\n".join(context_parts)
    
    # 組裝 messages
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"以下是與客戶問題相關的知識庫資料：\n\n{knowledge_text}\n\n---\n\n客戶問題：{question}\n\n請根據以上知識庫資料回答客戶的問題。"
        }
    ]
    
    return messages


def call_llm(messages: list[dict]) -> str:
    """
    呼叫 LLM API 生成回答。
    
    包含重試機制，處理各種 API 錯誤。
    """
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                LLM_API_URL,
                headers=headers,
                json=payload,
                timeout=60,  # LLM 生成可能較慢，給較長超時
            )
            
            if response.status_code == 429:
                wait_time = RETRY_DELAY * attempt
                print(f"  ⚠️  LLM API 限速，等待 {wait_time} 秒...")
                time.sleep(wait_time)
                continue
            
            if response.status_code != 200:
                print(f"  ❌ LLM API 錯誤 {response.status_code}: {response.text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                raise Exception(f"LLM API 錯誤 {response.status_code}")
            
            result = response.json()
            
            if "choices" not in result or len(result["choices"]) == 0:
                raise Exception(f"LLM 回應格式異常: {json.dumps(result, ensure_ascii=False)[:200]}")
            
            return result["choices"][0]["message"]["content"]
            
        except requests.exceptions.Timeout:
            print(f"  ⏱️  LLM 請求超時（第 {attempt}/{MAX_RETRIES} 次）")
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
    
    raise Exception(f"重試 {MAX_RETRIES} 次後仍然失敗")


# ============================================================
# RAG 完整流程
# ============================================================

def rag_query(question: str, vector_db: list[dict], verbose: bool = True) -> dict:
    """
    RAG 完整流程：問題 → 向量搜尋 → LLM 回答。
    
    回傳：{answer, references}
    """
    # 步驟 1：問題轉向量
    if verbose:
        print("🔍 正在搜尋相關知識...")
    
    query_embedding = get_query_embedding(question)
    
    # 步驟 2：向量搜尋
    results = search_similar(query_embedding, vector_db)
    
    if verbose:
        print(f"📚 找到 {len(results)} 筆相關資料")
        for r in results:
            print(f"   • [{r['id']}] {r['metadata']['title']} (相似度: {r['similarity']:.3f})")
    
    # 步驟 3：組裝 prompt 並呼叫 LLM
    if len(results) == 0:
        return {
            "answer": "抱歉，目前知識庫中沒有找到與您問題相關的資料。建議您直接聯繫我們的客服人員，我們會盡快為您解答！",
            "references": [],
        }
    
    if verbose:
        print("🤖 正在生成回答...")
    
    messages = build_rag_prompt(question, results)
    answer = call_llm(messages)
    
    # 整理引用資料
    references = []
    for r in results:
        references.append({
            "id": r["id"],
            "title": r["metadata"]["title"],
            "category": r["metadata"]["category"],
            "similarity": round(r["similarity"], 3),
        })
    
    return {
        "answer": answer,
        "references": references,
    }


# ============================================================
# 互動式 CLI
# ============================================================

def interactive_mode(vector_db: list[dict]):
    """
    互動式問答模式。
    
    輸入問題即可獲得 RAG 回答，輸入 quit/exit/q 退出。
    """
    print("\n" + "=" * 60)
    print("🏛️  繆思精工 RAG 知識庫問答系統")
    print("=" * 60)
    print("輸入問題即可查詢，輸入 quit / exit / q 退出\n")
    
    while True:
        try:
            question = input("❓ 您的問題：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n👋 感謝使用，再見！")
            break
        
        if not question:
            continue
        
        if question.lower() in ("quit", "exit", "q"):
            print("\n👋 感謝使用，再見！")
            break
        
        print()
        
        try:
            result = rag_query(question, vector_db)
            
            print("\n" + "─" * 50)
            print("💬 回答：")
            print(result["answer"])
            print("\n📎 引用資料：")
            for ref in result["references"]:
                print(f"   • [{ref['id']}] {ref['title']}（{ref['category']}，相似度 {ref['similarity']}）")
            print("─" * 50 + "\n")
            
        except Exception as e:
            print(f"\n❌ 發生錯誤：{e}\n")


# ============================================================
# 主程式
# ============================================================

def main():
    """主程式入口"""
    
    # 載入向量資料庫
    try:
        vector_db = load_vector_db(VECTORS_OUTPUT_PATH)
    except FileNotFoundError:
        print(f"❌ 找不到向量資料庫：{VECTORS_OUTPUT_PATH}")
        print("   請先執行 embed_knowledge.py 產生向量資料庫")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ 向量資料庫格式錯誤：{VECTORS_OUTPUT_PATH}")
        sys.exit(1)
    
    # 判斷模式：命令列參數 → 單次查詢；無參數 → 互動模式
    if len(sys.argv) > 1:
        # 單次查詢模式
        question = " ".join(sys.argv[1:])
        print(f"\n❓ 問題：{question}\n")
        
        result = rag_query(question, vector_db)
        
        print("\n" + "─" * 50)
        print("💬 回答：")
        print(result["answer"])
        print("\n📎 引用資料：")
        for ref in result["references"]:
            print(f"   • [{ref['id']}] {ref['title']}（{ref['category']}，相似度 {ref['similarity']}）")
        print("─" * 50)
    else:
        # 互動模式
        interactive_mode(vector_db)


if __name__ == "__main__":
    main()
