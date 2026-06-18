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
import re

logger = logging.getLogger(__name__)

# 句界：在句末標點/換行「之後」切，保留標點（lookbehind）。
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")

enabled: bool = True
# 查詢策略："multi"＝多重查詢(路況/任務/人格)+RRF 融合；"single"＝只用單一 query（ablation 對照）。
query_mode: str = "multi"
# HyDE 查詢增強（預設關閉；長文件才划算，每子查詢多一次輕量 LLM 生成）。
# 僅在 hyde_enabled 且語料塊數 > HYDE_GATE_CHUNKS 時實際啟用（短語料不划算 → 走純檢索）。
hyde_enabled: bool = False
HYDE_GATE_CHUNKS = 50

# --- 具名常數（避免 magic number 散落）---
CHUNK_SIZE = 400                 # 每塊最多字元數
CHUNK_OVERLAP = 80               # 相鄰塊重疊字元數
SIM_FLOOR = 0.01                 # cosine 相似度下限（低於此視為不相關，不取）
DEFAULT_K = 4                    # 最終注入塊數（多重查詢融合後）
PER_QUERY_K = 5                  # 每條子查詢各取前幾塊
RRF_C = 60                       # Reciprocal Rank Fusion 常數（慣用 60）

_CHUNKS: list[str] = []          # 文字塊
_SOURCES: list[str] = []         # 各塊來源檔名
_VECTORIZER = None               # 延遲建立的 TfidfVectorizer
_MATRIX = None                   # fit_transform 結果


def _tail_overlap(sents: list[str], max_chars: int) -> list[str]:
    """取 sents 尾端、累計字元數 ≤ max_chars 的句子（用於塊間語意重疊）。

    若最後一句本身就超過 max_chars（例如無標點的長字串）→ 回 []（寧可不重疊，也不讓塊超量）。
    """
    out: list[str] = []
    total = 0
    for s in reversed(sents):
        if total + len(s) > max_chars:
            break
        out.insert(0, s)
        total += len(s)
    return out


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """句/段感知切塊：先斷句，再貪婪打包到 ~size 字、塊間以「整句」重疊（≤overlap 字）。

    好處：每塊是完整句子的集合（provenance 顯示乾淨、不攔腰切斷）。
    保底：無標點的超長句（如 CSV 整列）會硬切成 ≤size 的片段，避免單塊爆量。
    """
    text = (text or "").strip()
    if not text:
        return []
    # 斷句；過長的單句硬切成 ≤size 的單位
    units: list[str] = []
    for s in (p for p in _SENT_SPLIT.split(text) if p.strip()):
        if len(s) <= size:
            units.append(s)
        else:
            units.extend(s[j:j + size] for j in range(0, len(s), size))
    if not units:
        return [text]

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for u in units:
        if cur and cur_len + len(u) > size:        # 放不下 → 先收一塊
            chunks.append("".join(cur).strip())
            cur = _tail_overlap(cur, overlap)       # 帶尾端整句到下一塊
            cur_len = sum(len(x) for x in cur)
        cur.append(u)
        cur_len += len(u)
    if cur:
        chunks.append("".join(cur).strip())
    return [c for c in chunks if c]


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


def _topk_with_scores(query: str, n: int) -> list[tuple[int, float]]:
    """單一 query 檢索核心：回傳前 n 個 (塊索引, cosine 分數)，過濾 < SIM_FLOOR。

    無庫/未啟用/空 query 回 []。retrieve 與 retrieve_multi 共用此核心。
    """
    if not enabled or _MATRIX is None or not query:
        return []
    from sklearn.metrics.pairwise import cosine_similarity
    qv = _VECTORIZER.transform([query])
    sims = cosine_similarity(qv, _MATRIX)[0]
    order = sims.argsort()[::-1][:n]
    return [(int(i), float(sims[i])) for i in order if sims[i] > SIM_FLOOR]


def retrieve(query: str, k: int = 3) -> list[str]:
    """回傳與 query 最相關的前 k 個文字塊（single 查詢；ablation 對照用）。"""
    return [_CHUNKS[i] for i, _ in _topk_with_scores(query, k)]


def retrieve_multi(subqueries: dict[str, str], k: int = DEFAULT_K,
                   per_k: int = PER_QUERY_K, c: int = RRF_C) -> list[dict]:
    """多重查詢 + Reciprocal Rank Fusion（RRF）。

    每條子查詢各自檢索 per_k 塊，依「名次」用 RRF 融合（免疫跨查詢的分數尺度差異），
    去重後取融合分數最高的前 k 塊。被多條子查詢撈到的塊分數自動變高（更相關）。

    回傳結構化 hit（含 provenance）：
        {chunk, source, idx, via:[子查詢標籤], rrf, scores:{標籤: cos}}
    無庫/未啟用/無子查詢回 []。
    """
    if not enabled or _MATRIX is None or not subqueries:
        return []
    fused: dict[int, dict] = {}
    for tag, q in subqueries.items():
        for rank, (i, s) in enumerate(_topk_with_scores(q, per_k), start=1):
            f = fused.setdefault(i, {"rrf": 0.0, "via": [], "scores": {}})
            f["rrf"] += 1.0 / (c + rank)
            if tag not in f["via"]:
                f["via"].append(tag)
            f["scores"][tag] = round(s, 3)
    order = sorted(fused, key=lambda i: fused[i]["rrf"], reverse=True)[:k]
    return [{
        "chunk": _CHUNKS[i],
        "source": _SOURCES[i],
        "idx": i,
        "via": fused[i]["via"],
        "rrf": round(fused[i]["rrf"], 4),
        "scores": fused[i]["scores"],
    } for i in order]


def has_docs() -> bool:
    return bool(_CHUNKS)


def chunk_count() -> int:
    """目前知識庫的塊數（HyDE size gate 用）。"""
    return len(_CHUNKS)


def hyde_active() -> bool:
    """是否實際啟用 HyDE：開關開 且 語料夠大（短語料不划算）。"""
    return hyde_enabled and len(_CHUNKS) > HYDE_GATE_CHUNKS


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
