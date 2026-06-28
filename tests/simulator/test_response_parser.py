"""response_parser 測試：涵蓋 GAML 全部 key 變體 + 真實 LLM 輸出 + 雜訊。"""

from __future__ import annotations

from llm_abm_simulator.decisions import response_parser as rp

TOWNS = ["佳里區", "永康區", "安平區", "南區", "鹽水區", "安南區", "東區"]


def test_real_llm_decision_output():
    body = (
        '{"agents":[{"agent name":"林志安","action mode":"short_distance",'
        '"residential_location":"佳里區","vehicle_type":"機車"}]}'
    )
    rows = rp.parse_rows(body, TOWNS, "東區")
    assert len(rows) == 1
    r = rows[0]
    assert r["profile_name"] == "林志安"
    assert r["action_mode"] == "short_distance"   # 空格 key 也要解析到
    assert r["origin_town"] == "佳里區"
    assert r["vehicle_type"] == "motorcycle"   # 中文輸入也正規化為英文 canonical


def test_underscore_and_chinese_keys_with_fence():
    messy = (
        "結果如下：\n```json\n"
        '{"agents":[{"agent_id":"v1","action_mode":"avoid_congestion",'
        '"vehicle_type":"汽車","出發點":"安南區"}]}\n```'
    )
    rows = rp.parse_rows(messy, TOWNS, "東區")
    assert rows[0]["agent_id"] == "v1"
    assert rows[0]["action_mode"] == "avoid_congestion"
    assert rows[0]["origin_town"] == "安南區"
    assert rows[0]["vehicle_type"] == "car"


def test_action_mode_as_map():
    body = {"agents": [{"agent_id": "v2", "action_mode": {"mode_name": "fast"}}]}
    rows = rp.parse_rows(body, TOWNS, "東區")
    assert rows[0]["action_mode"] == "fast"


def test_origin_key_aliases():
    for key in ("origin", "residential_location", "origin_town", "origin_taz", "起點"):
        rows = rp.parse_rows({"agents": [{key: "永康區"}]}, TOWNS, "東區")
        assert rows[0]["origin_town"] == "永康區", key


def test_decisions_list_key():
    body = {"decisions": [{"agent_id": "v3", "action mode": "avoid_congestion"}]}
    rows = rp.parse_rows(body, TOWNS, "東區")
    assert rows[0]["action_mode"] == "avoid_congestion"


def test_vehicle_type_normalization():
    # canonical 為英文 car/motorcycle；同時相容舊中文輸入與英文別名
    assert rp.normalize_vehicle_type("一輛機車") == "motorcycle"
    assert rp.normalize_vehicle_type("家用汽車") == "car"
    assert rp.normalize_vehicle_type("a scooter") == "motorcycle"
    assert rp.normalize_vehicle_type("sedan") == "car"
    assert rp.normalize_vehicle_type("unknown", default="car") == "car"


def test_unparseable_returns_empty():
    assert rp.parse_rows("這不是 JSON 也沒有 agents", TOWNS, "東區") == []
