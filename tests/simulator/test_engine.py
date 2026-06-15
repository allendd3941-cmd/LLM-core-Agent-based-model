"""engine 整合測試：生命週期、determinism、抵達、max_steps、輸出、GeoJSON。

這些測試會載入真實 GIS 與 bundle 的 OSM 路網（data/tainan_roads.graphml）。
"""

from __future__ import annotations

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


def test_metrics_history_recorded(small_config):
    eng, _ = _run(small_config)
    # 每步指標累積在記憶體 history（前端圖表與 build_analysis 都讀它，不落地 CSV）
    assert len(eng.recorder.history) == small_config.max_steps
    entry = eng.recorder.history[-1]
    for key in ("cycle", "elapsed_minutes", "active_road_count", "average_congestion_proxy"):
        assert key in entry


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
