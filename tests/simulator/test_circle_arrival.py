"""距離抵達（踏進抵達圈即算抵達 + 安全移除車輛）測試。

T1/T2/T6：抵達車都在圈內、移動中車都在圈外（進圈就抵達）。
T3：抵達車的 UXsim Vehicle 已安全移出路網（不在 RUNNING/LIVING/任何 link.vehicles）、不崩、他車車速合理。
T4：距離抵達 vs 節點抵達 → 抵達數不減（解塞）。
T5：旗標關 → 節點抵達（位置 snap 到終點節點）。
T8：抵達車仍在 agents（畫面不消失）。
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine

pytest.importorskip("uxsim")


def _run(circle_entry: bool, steps: int = 8, crop: float = 4.0, n: int = 400, ambient: int = 300):
    orig = config.UXSIM_CONFIG
    try:
        config.UXSIM_CONFIG = dataclasses.replace(orig, dev_crop_km=crop)
        config.set_runtime_ambient_count(ambient)
        cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=n, max_steps=steps, step_minutes=5,
                                  use_llm=False, seed=7, arrival_on_circle_entry=circle_entry)
        eng = UXsimEngine(cfg)
        eng.initialize()
        eng._apply_step_decisions = lambda *a, **k: None   # 不跑 LLM/規則決策，純看物理+抵達
        eng.resume()
        for _ in range(steps):
            eng.step()
        return eng
    finally:
        config.UXSIM_CONFIG = orig


@pytest.fixture(scope="module")
def eng_on():
    return _run(True)


@pytest.fixture(scope="module")
def eng_off():
    return _run(False)


def _event(eng):
    return [a for a in eng.agents if a.role == "event"]


def test_arrived_inside_circle_and_moving_outside(eng_on):
    from llm_abm_simulator.domain.events import RouteStatus
    sx, sy = eng_on._stadium_xy
    R = eng_on.cfg.arrival_radius_m
    arrived = [a for a in _event(eng_on) if a.route_status == RouteStatus.ARRIVED]
    assert arrived, "應有車抵達"
    for a in arrived:                                   # T1/T2：抵達車都在圈內（且綠點位置在圈內）
        assert math.hypot(a.x - sx, a.y - sy) <= R + 1e-6
    # T6：真正在 link 上行駛的車都在圈外（一進圈就被判定抵達）。排除「建車失敗(o==d)、卡在起點、無 veh」
    # 的退化車（它們不在路網上、不影響車流）。
    for a in _event(eng_on):
        if a.route_status != RouteStatus.MOVING:
            continue
        veh = eng_on._veh.get(a.agent_id)
        if veh is None or getattr(veh, "link", None) is None:
            continue
        assert math.hypot(a.x - sx, a.y - sy) > R - 1e-6


def test_arrived_agents_stay_visible(eng_on):
    from llm_abm_simulator.domain.events import RouteStatus
    arrived = [a for a in _event(eng_on) if a.route_status == RouteStatus.ARRIVED]
    assert arrived
    assert all(a in eng_on.agents for a in arrived)     # T8：抵達車仍在 agents（畫面不消失）


def test_arrived_vehicles_removed_from_network(eng_on):
    from llm_abm_simulator.domain.events import RouteStatus
    W = eng_on._world
    arrived = [a for a in _event(eng_on) if a.route_status == RouteStatus.ARRIVED]
    assert arrived
    removed_names = set()
    for a in arrived:                                   # T3：抵達車已移出 RUNNING/LIVING、link=None
        veh = eng_on._veh.get(a.agent_id)
        if veh is None:
            continue
        removed_names.add(veh.name)
        assert veh.name not in W.VEHICLES_RUNNING
        assert veh.name not in W.VEHICLES_LIVING
        assert getattr(veh, "link", None) is None
    for link in W.LINKS:                                # 不在任何 link.vehicles
        for v in link.vehicles:
            assert v.name not in removed_names
    moving = [a for a in _event(eng_on) if a.route_status == RouteStatus.MOVING]
    for a in moving:                                    # 他車車速合理（物理沒被破壞）
        assert 0.0 <= a.speed_kmh <= 200.0


def test_circle_arrival_not_fewer_than_node(eng_on, eng_off):
    arr_on = sum(1 for a in _event(eng_on) if a.arrival_cycle is not None)
    arr_off = sum(1 for a in _event(eng_off) if a.arrival_cycle is not None)
    assert arr_on >= arr_off, f"距離抵達不應比節點抵達少（解塞）：on={arr_on} off={arr_off}"


def test_flag_off_node_arrival_snapped(eng_off):
    from llm_abm_simulator.domain.events import RouteStatus
    arrived = [a for a in _event(eng_off) if a.route_status == RouteStatus.ARRIVED]
    for a in arrived[:30]:                              # T5：旗標關→節點抵達，位置 snap 到終點節點
        nx, ny = eng_off.network.node_xy(a.destination_node)
        assert math.hypot(a.x - nx, a.y - ny) < 1.0
