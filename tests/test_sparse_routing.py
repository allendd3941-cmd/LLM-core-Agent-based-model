"""稀疏 route_search 對拍測試：稀疏版的 route_pref 必須與 UXsim 原生 all-pairs 對「實際終點」逐元素相同。

這是 simulation/uxsim_sparse_routing.py 的核心正確性保證——只算得少、結果不變。
"""

from __future__ import annotations

import numpy as np
import pytest

uxsim = pytest.importorskip("uxsim")


def _build_small_world():
    """小路網 + 幾台車，跑一小段讓 traveltime_instant 有值。回傳 World（hard_deterministic → 無 rng、可對拍）。"""
    W = uxsim.World(name="t", deltan=1, tmax=600, print_mode=0, save_mode=0,
                    show_mode=0, random_seed=0, hard_deterministic_mode=True)
    coords = {"A": (0, 0), "B": (1000, 0), "C": (2000, 0), "D": (1000, 1000)}
    for nm, (x, y) in coords.items():
        W.addNode(nm, x, y)
    for a, b in [("A", "B"), ("B", "C"), ("A", "D"), ("D", "C"), ("B", "D")]:
        W.addLink(f"{a}{b}", a, b, length=1000, free_flow_speed=10, number_of_lanes=1)
        W.addLink(f"{b}{a}", b, a, length=1000, free_flow_speed=10, number_of_lanes=1)
    W.adddemand("A", "C", 0, 100, volume=5)
    W.adddemand("A", "B", 0, 100, volume=3)
    W.adddemand("D", "C", 0, 100, volume=2)
    W.exec_simulation(duration_t=120)   # 推進一小段，填 traveltime_instant
    return W


def test_sparse_route_pref_matches_full():
    from llm_abm_simulator.simulation import uxsim_sparse_routing as sr

    W = _build_small_world()
    rc = W.ROUTECHOICE
    t = W.T * W.DELTAT
    try:
        # 原生 all-pairs（確保未 patch；從全 0 route_pref 起 → DUO 確定性初始化）
        sr.disable_sparse_routing()
        rc.route_pref[:] = 0
        rc.route_search_all(t, noise=0)
        rc.homogeneous_DUO_update()
        full = rc.route_pref.copy()

        # 稀疏版（同樣從全 0 起）
        sr.enable_sparse_routing()
        rc.route_pref[:] = 0
        rc.route_search_all(t, noise=0)
        rc.homogeneous_DUO_update()
        sparse = rc.route_pref

        D = rc._sparse_dests
        assert len(D) >= 1                       # 有抓到實際終點（B、C）
        for k in D:
            assert np.array_equal(full[k], sparse[k]), f"終點 {k} 的 route_pref 與原生不一致"
    finally:
        sr.disable_sparse_routing()              # 還原，不污染其他測試


def test_active_dests_collects_vehicle_destinations():
    from llm_abm_simulator.simulation import uxsim_sparse_routing as sr

    W = _build_small_world()
    D = set(int(x) for x in sr._active_dest_ids(W))
    # 終點是 C 與 B（demand A→C、A→B、D→C）
    assert W.get_node("C").id in D
    assert W.get_node("B").id in D
    # A、D 不是任何車的終點 → 不在 active 集合（這正是稀疏的省力來源）
    assert W.get_node("A").id not in D
