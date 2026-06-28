"""RAG 多重查詢 + RRF + 查詢建構器測試（里程碑 A）。

涵蓋：全域路況抽取、persona 聚合與壞 JSON 降級、子查詢組裝、
RRF 融合（多子查詢命中同塊→分數更高）、空庫與門檻、provenance 欄位。
"""

from __future__ import annotations

import pytest

from llm_server import rag_query, rag_store
from llm_server.perception import global_situation_text

PERCEPTION = (
    "【全域路況】\n"
    "整體交通：壅塞；壅塞趨勢：惡化；目的地：安平區\n"
    "壅塞熱點：安南區（高／3條壅塞）；中西區（中／1條壅塞）\n"
    "\n"
    "【各車當前狀況】\n"
    "・黃俊祺：位於安南區/中華路，腳下壅塞、速度緩慢…\n"
)

PROFILE = (
    '{"agents":[{"identity":{"occupation":"大學生","vehicle_ownership":"機車"},'
    '"traits":{"habits":["習慣提早到球場"],"economic_preferences_and_tradeoffs":["偏好省錢停車"]}},'
    '{"identity":{"occupation":"上班族","vehicle_ownership":"機車"},'
    '"traits":{"decision_making_tendencies":["常依現場人潮調整"]}}]}'
)


@pytest.fixture(autouse=True)
def _clean_store():
    """每個測試前後清空知識庫，避免模組全域狀態互相污染。"""
    rag_store.clear()
    rag_store.enabled = True
    rag_store.query_mode = "multi"
    rag_store.hyde_enabled = False
    rag_store.HYDE_GATE_CHUNKS = 50
    yield
    rag_store.clear()
    rag_store.hyde_enabled = False
    rag_store.HYDE_GATE_CHUNKS = 50


def test_global_situation_text_excludes_per_agent():
    gs = global_situation_text(PERCEPTION)
    assert "全域路況" in gs
    assert "各車當前狀況" not in gs
    assert "黃俊祺" not in gs


def test_global_situation_text_fallback():
    # 沒有區塊標記時回整段（保底）
    assert global_situation_text("隨便一段文字") == "隨便一段文字"
    assert global_situation_text("") == ""


def test_q_persona_aggregates():
    s = rag_query.q_persona(PROFILE)
    assert "大學生" in s and "上班族" in s
    assert "機車" in s


def test_q_persona_malformed_degrades_to_empty():
    assert rag_query.q_persona("這不是合法 json {{{") == ""
    assert rag_query.q_persona("") == ""
    assert rag_query.q_persona('{"agents":[]}') == ""


def test_build_subqueries_keys():
    subs = rag_query.build_subqueries(PERCEPTION, PROFILE)
    assert set(subs) == {"路況", "任務", "人格"}
    # 無 persona 時人格子查詢應略過
    subs2 = rag_query.build_subqueries(PERCEPTION, "")
    assert "人格" not in subs2 and "任務" in subs2


def test_retrieve_multi_empty_store():
    subs = rag_query.build_subqueries(PERCEPTION, PROFILE)
    assert rag_store.retrieve_multi(subs) == []


def test_retrieve_multi_returns_provenance():
    rag_store.add_text("通勤調查.csv", "台南大學生與上班族多以機車通勤，偏好省錢停車與依現場人潮調整路線。")
    subs = rag_query.build_subqueries(PERCEPTION, PROFILE)
    hits = rag_store.retrieve_multi(subs)
    assert hits
    h = hits[0]
    assert set(h) >= {"chunk", "source", "idx", "via", "rrf", "scores"}
    assert h["source"] == "通勤調查.csv"
    assert isinstance(h["via"], list) and h["via"]


def test_rrf_boosts_multi_query_hit():
    # docA 同時匹配兩條子查詢(任務+人格)，docB 只匹配一條 → docA 應排前
    rag_store.add_text("docA", "壅塞時選擇交通方式與車種，大學生機車通勤偏好省錢避開壅塞改道")
    rag_store.add_text("docB", "大學生機車通勤")
    subs = rag_query.build_subqueries(PERCEPTION, PROFILE)
    hits = rag_store.retrieve_multi(subs)
    assert hits
    # 被多條子查詢撈到的塊 via 較多、rrf 較高、排前
    top = hits[0]
    assert len(top["via"]) >= 1
    rrfs = [h["rrf"] for h in hits]
    assert rrfs == sorted(rrfs, reverse=True)  # 已依 rrf 由高到低排序


def test_disabled_returns_empty():
    rag_store.add_text("docA", "壅塞改道分流")
    rag_store.enabled = False
    subs = rag_query.build_subqueries(PERCEPTION, PROFILE)
    assert rag_store.retrieve_multi(subs) == []
    assert rag_store.retrieve("壅塞") == []


def test_dedupe_provenance_merges_batches():
    """engine 的多批 provenance 去重：同 (source,idx) 留 rrf 高者、依 rrf 排序。"""
    from llm_abm_simulator.simulation.engine import _dedupe_provenance
    prov = [
        {"source": "a.txt", "idx": 2, "rrf": 0.01, "via": ["路況"]},
        {"source": "a.txt", "idx": 2, "rrf": 0.03, "via": ["路況", "人格"]},  # 同塊更高
        {"source": "b.csv", "idx": 5, "rrf": 0.02, "via": ["人格"]},
    ]
    d = _dedupe_provenance(prov)
    assert len(d) == 2
    assert d[0]["rrf"] == 0.03 and d[0]["via"] == ["路況", "人格"]  # 排序+留高者
    assert _dedupe_provenance([]) == []
    assert _dedupe_provenance([prov[0]] * 10, cap=3)  # cap 生效不爆


def test_hyde_gate():
    """HyDE 僅在開關開 且 語料塊數 > gate 時啟用（短語料不划算）。"""
    assert rag_store.hyde_active() is False           # 預設關
    rag_store.hyde_enabled = True
    rag_store.HYDE_GATE_CHUNKS = 3
    rag_store.add_text("small", "壅塞分流。")          # 1 塊 ≤ 3
    assert rag_store.hyde_active() is False
    # 加入夠長文字（~2000 字 → 數塊），超過 gate=3
    rag_store.add_text("big", "".join(f"管制計畫第{i}條：散場時相關幹道實施單向分流與替代道路引導。" for i in range(120)))
    assert rag_store.chunk_count() > 3
    assert rag_store.hyde_active() is True


def test_hyde_expand_degrades_on_failure():
    """生成失敗/空/空查詢 → 降級回原 query（不串接、等同無擴充）。"""
    def boom(prompt, **kw):
        raise RuntimeError("LLM down")

    assert rag_query.hyde_expand("整體壅塞", boom) == "整體壅塞"          # 例外降級
    assert rag_query.hyde_expand("整體壅塞", lambda p, **k: "") == "整體壅塞"  # 空生成降級
    assert rag_query.hyde_expand("整體壅塞", lambda p, **k: "   ") == "整體壅塞"  # 全空白降級
    assert rag_query.hyde_expand("", lambda p, **k: "x") == ""           # 空查詢


def test_hyde_expand_concatenates_and_keeps_query():
    """Query2Doc 式：成功時「串接」原查詢（重複 n 次）+ pseudo-document，不取代。"""
    q, doc = "整體壅塞", "散場應啟動替代道路分流。"
    out = rag_query.hyde_expand(q, lambda p, **k: doc)
    assert doc in out                                       # 含 pseudo-document
    assert q in out                                         # 保留原查詢詞（關鍵性質）
    assert out.count(q) == rag_query.HYDE_QUERY_REPEAT       # 原查詢重複 n 次（權重平衡）
    assert out != doc                                       # 不再是「取代」舊行為


def test_hyde_concat_retrieval_keeps_query_and_adds_expansion():
    """檢索層證明串接同時保留『原查詢訊號』與加上『pseudo-doc 擴充』。

    塊A 只含原查詢詞彙、塊B 只含 pseudo-doc 詞彙：
      純查詢→只中A、純 pseudo（舊取代行為）→只中B、串接（新行為）→A、B 都中。
    """
    A, B = "整體壅塞情勢嚴重", "替代道路分流引導"
    rag_store.add_text("A", A)
    rag_store.add_text("B", B)
    q, doc = "整體壅塞", "散場替代道路分流"
    query_only = set(rag_store.retrieve(q, k=5))
    pseudo_only = set(rag_store.retrieve(doc, k=5))         # 舊「取代」行為
    concat = set(rag_store.retrieve(rag_query.hyde_expand(q, lambda p, **k: doc), k=5))
    assert A in query_only and B not in query_only          # 純查詢漏掉 B
    assert B in pseudo_only and A not in pseudo_only         # 純 pseudo（舊行為）漏掉 A
    assert A in concat and B in concat                       # 串接：A、B 都召回


# ---- agent profile 生成接 RAG（共用同一 rag_store + profile 專屬查詢）----

def test_build_profile_subqueries_keys():
    subs = rag_query.build_profile_subqueries()
    assert set(subs) == {"人口社經", "運具行為", "活動情境"}
    assert all(isinstance(v, str) and v for v in subs.values())


def test_profile_rag_context_disabled_degrades():
    """無庫/未啟用 → ('', [])（降級，不影響生成；不呼叫 LLM）。"""
    from llm_server import agent_profile
    rag_store.enabled = False
    ctx, prov = agent_profile.build_profile_rag_context()
    assert ctx == "" and prov == []
    # 空庫（啟用但無 docs）也降級
    rag_store.enabled = True
    ctx2, prov2 = agent_profile.build_profile_rag_context()
    assert ctx2 == "" and prov2 == []


def test_profile_rag_context_injects_and_prompt_contains():
    """有語料時：檢索到 provenance、rag_ctx 含 chunk，且注入 build_user_prompt。"""
    from llm_server import agent_profile
    rag_store.add_text("人口調查.csv", "台南市居民汽機車持有率高，通勤多以機車為主；族群以大學生與上班族居多。")
    ctx, prov = agent_profile.build_profile_rag_context()
    assert prov                                       # 有檢索到（共用同一 rag_store）
    assert "Reference population/behavior knowledge" in ctx and "持有率" in ctx   # rag_ctx 含 RAG 標頭 + 檢索到的內容
    p = agent_profile.build_user_prompt(10, ctx)
    assert "Reference population/behavior knowledge" in p and "Generate 10" in p   # prompt 注入了 RAG 段
    # 不傳 rag_ctx（降級/未檢索）時，prompt 不含 RAG 標頭（用標頭判據，不受 base prompt 內容影響）
    assert "Reference population/behavior knowledge" not in agent_profile.build_user_prompt(10)
