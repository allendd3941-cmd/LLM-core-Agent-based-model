"""rag_store.py — decision-making 的 RAG 知識庫（使用者上傳檔案 → 決策時檢索注入）。

設計（最適合本專案、且零額外依賴、離線可跑）：
- 檢索用 sklearn TF-IDF（``char_wb`` n-gram，對中文友善），取代原本較弱的關鍵字 RAG。
- **每步（每批）只用「當前路況文字」當 query 全域檢索一次**注入 decision prompt，
  不做 per-agent 檢索（會爆 token/延遲）。
- 知識庫存記憶體（demo 用），可上傳純文字/markdown/csv，可清空、可開關。

定位（誠實）：RAG 只有在上傳「真正會影響決策的知識」（交通管制計畫、場館疏運手冊、
歷史壅塞模式…）時才有意義；用來把 LLM 決策 grounding 在權威/在地知識上。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

enabled: bool = True
_CHUNKS: list[str] = []          # 文字塊
_SOURCES: list[str] = []         # 各塊來源檔名
_VECTORIZER = None               # 延遲建立的 TfidfVectorizer
_MATRIX = None                   # fit_transform 結果


def _chunk(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def _rebuild() -> None:
    """重建 TF-IDF 索引（每次增刪後）。無文件則清空。"""
    global _VECTORIZER, _MATRIX
    if not _CHUNKS:
        _VECTORIZER = _MATRIX = None
        return
    from sklearn.feature_extraction.text import TfidfVectorizer
    _VECTORIZER = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
    _MATRIX = _VECTORIZER.fit_transform(_CHUNKS)


def add_text(name: str, text: str) -> int:
    """加入一份文件（切塊後索引）。回傳新增的塊數。"""
    chunks = _chunk(text)
    for c in chunks:
        _CHUNKS.append(c)
        _SOURCES.append(name or "uploaded")
    _rebuild()
    logger.info("RAG 加入 %s：%d 塊（庫共 %d 塊）", name, len(chunks), len(_CHUNKS))
    return len(chunks)


def retrieve(query: str, k: int = 3) -> list[str]:
    """回傳與 query 最相關的前 k 個文字塊。無庫/未啟用/無相似回 []。"""
    if not enabled or _MATRIX is None or not query:
        return []
    from sklearn.metrics.pairwise import cosine_similarity
    qv = _VECTORIZER.transform([query])
    sims = cosine_similarity(qv, _MATRIX)[0]
    order = sims.argsort()[::-1][:k]
    return [_CHUNKS[i] for i in order if sims[i] > 0.01]


def has_docs() -> bool:
    return bool(_CHUNKS)


def clear() -> None:
    global _CHUNKS, _SOURCES
    _CHUNKS, _SOURCES = [], []
    _rebuild()


def stats() -> dict:
    from collections import Counter
    return {
        "enabled": enabled,
        "chunks": len(_CHUNKS),
        "sources": [{"name": n, "chunks": c} for n, c in Counter(_SOURCES).items()],
    }
