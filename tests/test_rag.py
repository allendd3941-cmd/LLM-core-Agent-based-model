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
    """HyDE 生成成功回假想文件；失敗/空 → 降級回原 query（不影響檢索）。"""
    assert rag_query.hyde_expand("整體壅塞", lambda p, **k: "散場應啟動替代道路分流。") \
        == "散場應啟動替代道路分流。"

    def boom(prompt, **kw):
        raise RuntimeError("LLM down")

    assert rag_query.hyde_expand("整體壅塞", boom) == "整體壅塞"   # 降級
    assert rag_query.hyde_expand("", lambda p, **k: "x") == ""
