"""engine 整合測試：生命週期、determinism、抵達、max_steps、輸出、GeoJSON。

這些測試會載入真實 GIS 與 bundle 的 OSM 路網（data/tainan_roads.graphml）。
"""

from __future__ import annotations

import csv

from llm_abm_simulator import config
from llm_abm_simulator.simulation.engine import SimulationEngine


def _run(cfg):
    eng = SimulationEngine(cfg)
    eng.initialize()
    states = eng.run_to_completion()
    return eng, states


def test_lifecycle_and_max_steps(small_config):
    eng, states = _run(small_config)
    assert eng.is_initialized
    assert len(states) == small_config.max_steps
    assert states[-1].cycle == small_config.max_steps
    assert states[-1].finished is True
    assert len(eng.agents) == small_config.nb_agents


def test_determinism_same_seed(small_config):
    _, s1 = _run(small_config)
    _, s2 = _run(small_config)
    f1 = [(a.agent_id, a.lat, a.lng, a.route_status) for a in s1[-1].agents]
    f2 = [(a.agent_id, a.lat, a.lng, a.route_status) for a in s2[-1].agents]
    assert f1 == f2


def test_agents_make_progress(small_config):
    _, states = _run(small_config)
    # 至少有 agent 在最後距離終點比一開始近（有實際移動）
    first = {a.agent_id: a.distance_to_destination for a in states[0].agents}
    last = {a.agent_id: a.distance_to_destination for a in states[-1].agents}
    assert any(last[k] < first[k] for k in first)


def test_snapshot_shape(small_config):
    _, states = _run(small_config)
    msg = states[-1].to_message()
    assert msg["type"] == "state_update"
    for key in ("cycle", "agents", "roads", "metrics", "mode_distribution", "status_distribution"):
        assert key in msg
    a = msg["agents"][0]
    for key in ("agent_id", "lat", "lng", "route_status", "active_mode", "vehicle_type"):
        assert key in a


def test_csv_outputs_written(small_config):
    _run(small_config)
    assert config.AGENT_MEMORY_CSV.exists()
    assert config.ROAD_FLOW_CSV.exists()
    with config.AGENT_MEMORY_CSV.open(encoding="utf-8") as f:
        header = next(csv.reader(f))
    # 欄位需對齊 GAML save 語句
    assert header[:5] == ["cycle", "agent_id", "origin_town", "destination_town", "current_town"]


def test_init_payload_geojson(small_config):
    eng, _ = _run(small_config)
    payload = eng.init_payload()
    assert payload["towns_geojson"]["type"] == "FeatureCollection"
    assert payload["roads_geojson"]["type"] == "FeatureCollection"
    assert len(payload["towns_geojson"]["features"]) == 37   # 臺南市 37 區
    assert "lat" in payload["stadium"] and "lng" in payload["stadium"]


def test_reset_reinitializes(small_config):
    eng, _ = _run(small_config)
    eng.reset()
    assert eng.scheduler.cycle == 0
    assert eng.is_initialized
    assert len(eng.agents) == small_config.nb_agents
