"""抵達圈 K-nearest 分流測試。

A：正確性不變式（合法節點、就近 K 內、K=1 回歸、確定性、fallback）。
B：分流改善（K=5 相對 K=1：top-1 佔比↓、用到節點數↑、每節點 max↓）。
"""

from __future__ import annotations

import dataclasses
from collections import Counter

import numpy as np

from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.engine import SimulationEngine


def _engine(k: int, radius: float = 800.0, n: int = 300):
    cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=n, max_steps=2, use_llm=False,
                              seed=7, arrival_radius_m=radius, arrival_gates_per_car=k)
    eng = SimulationEngine(cfg)
    eng.initialize()
    return eng


def _event(eng):
    return [a for a in eng.agents if a.role == "event"]


# ---- A：正確性 ----

def test_assigned_in_arrival_nodes():
    eng = _engine(5)
    nodes = set(eng._arrival_nodes)
    assert all(a.destination_node in nodes for a in _event(eng))   # A1


def test_assigned_within_k_nearest():
    eng = _engine(5)
    k = 5
    coords = eng._arrival_coords
    keff = min(k, len(eng._arrival_nodes))
    for a in _event(eng):
        ox, oy = eng.network.node_xy(a.current_node)   # init 時 current_node = origin
        d2 = (coords[:, 0] - ox) ** 2 + (coords[:, 1] - oy) ** 2
        knearest = {eng._arrival_nodes[i] for i in np.argsort(d2)[:keff]}
        assert a.destination_node in knearest          # A2：只在最近 K 內（就近、不失真）


def test_k1_equals_nearest():
    eng = _engine(1)
    for a in _event(eng):
        assert a.destination_node == eng._nearest_arrival_node(a.current_node)   # A3：回歸錨點


def test_deterministic():
    d1 = {a.agent_id: a.destination_node for a in _event(_engine(5))}
    d2 = {a.agent_id: a.destination_node for a in _event(_engine(5))}
    assert d1 == d2                                     # A4


def test_radius_zero_single_dest():
    eng = _engine(5, radius=0)
    assert eng._arrival_nodes == [eng._dest_node]
    assert {a.destination_node for a in _event(eng)} == {eng._dest_node}   # A5


def test_k_exceeds_available_no_crash():
    eng = _engine(100000)                               # K >> 可用節點 → 用現有全部、不崩
    nodes = set(eng._arrival_nodes)
    assert all(a.destination_node in nodes for a in _event(eng))   # A6


# ---- B：分流改善（K=5 vs K=1）----

def test_spread_improves_distribution():
    n = 400
    c1 = Counter(a.destination_node for a in _event(_engine(1, n=n)))
    c5 = Counter(a.destination_node for a in _event(_engine(5, n=n)))
    tot = sum(c1.values())
    top1_1, top1_5 = max(c1.values()) / tot, max(c5.values()) / tot
    assert top1_5 <= top1_1 * 0.6          # B1：top-1 佔比相對↓≥40%
    assert len(c5) >= len(c1) * 2          # B2：用到節點數≥2×
    assert max(c5.values()) <= max(c1.values()) * 0.6   # B3：每節點 max 相對↓≥40%
