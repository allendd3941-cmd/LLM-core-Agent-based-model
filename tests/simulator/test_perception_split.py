"""perception 文字切分測試（Option A 前綴快取：全域路況 vs 各車狀況分開）。"""

from __future__ import annotations

from llm_server.perception import (
    run_perception,
    global_situation_text,
    agents_situation_text,
)


def _payload():
    return {
        "environment": {
            "overall_traffic": "順暢",
            "congestion_trend": "持平",
            "destination_town": "安定區",
            "congestion_hotspots": [{"town": "永康區", "level": "高", "crowded_roads": 3}],
        },
        "agents_status": [
            {"agent_name": "車A", "active_mode": "fast",
             "environment": {"current_town": "東區"}, "memory": {}},
            {"agent_name": "車B", "active_mode": "comfortable",
             "environment": {"current_town": "南區"}, "memory": {}},
        ],
    }


def test_perception_global_agents_split():
    text = run_perception(_payload())
    g = global_situation_text(text)
    a = agents_situation_text(text)
    # 全域區塊：含全域路況、不含各車狀況
    assert "【全域路況】" in g
    assert "【各車當前狀況】" not in g
    assert "永康區" in g                      # 熱點屬於全域
    # 各車區塊：含各車狀況與車名、不含全域標題
    assert "【各車當前狀況】" in a
    assert "車A" in a and "車B" in a
    assert "【全域路況】" not in a


def test_perception_split_is_byte_stable_per_env():
    """同 env、不同批的車 → 全域區塊 byte 相同（這是前綴快取能命中的前提）。"""
    p1 = _payload()
    p2 = _payload()
    p2["agents_status"] = [p2["agents_status"][0]]   # 不同批：車數不同
    g1 = global_situation_text(run_perception(p1))
    g2 = global_situation_text(run_perception(p2))
    assert g1 == g2
