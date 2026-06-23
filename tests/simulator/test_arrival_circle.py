"""球場「抵達圈」多閘門終點測試。

驗證：① 抵達圈節點都在半徑內、非空 ② 啟用時事件車終點分散到多個節點(解單點 funnel)
③ 半徑 0 → 回退單一球場終點節點。
"""

from __future__ import annotations

import dataclasses
import math

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.engine import SimulationEngine


def _engine(radius_m: float, n: int = 60):
    cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=n, max_steps=4,
                              use_llm=False, seed=7, arrival_radius_m=radius_m)
    eng = SimulationEngine(cfg)
    eng.initialize()
    return eng


def test_arrival_nodes_within_radius():
    eng = _engine(800)
    assert eng._arrival_nodes, "抵達圈應有節點"
    sx, sy = eng._stadium_xy
    for n in eng._arrival_nodes:
        x, y = eng.network.node_xy(n)
        assert math.hypot(x - sx, y - sy) <= 800 + 1e-6   # 全在半徑內


def test_event_dests_spread_with_circle():
    eng = _engine(800, n=60)
    event = [a for a in eng.agents if a.role == "event"]
    assert event
    dests = {a.destination_node for a in event}
    assert len(dests) > 1, "抵達圈啟用時事件車終點應分散到多個節點（非全擠單一球場節點）"
    # 每個終點都在抵達圈節點集內
    assert dests <= set(eng._arrival_nodes)


def test_fallback_radius_zero_single_dest():
    eng = _engine(0, n=60)
    assert eng._arrival_nodes == [eng._dest_node]          # 半徑0 → 只有球場節點
    event = [a for a in eng.agents if a.role == "event"]
    dests = {a.destination_node for a in event}
    assert dests == {eng._dest_node}, "半徑 0 → 回退單一球場終點（舊行為）"
