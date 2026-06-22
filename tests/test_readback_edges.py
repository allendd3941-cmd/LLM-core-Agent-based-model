"""A1：readback 進入邊抽取等價測試。

驗證「直接讀 veh.log_t_link 的 Link 序列」與舊版「veh.traveled_route()[0].links」**逐元素等價**
（含一步跨多條 link 的情況）。這是 uxsim_engine._readback 去 traveled_route() 優化的正確性保證。
"""

from __future__ import annotations

import pytest

uxsim = pytest.importorskip("uxsim")


def _build_world():
    """小路網 + 一條會跨多 link 的需求（A→B→C→D），讓車一步走過數條 link。"""
    W = uxsim.World(name="t", deltan=1, tmax=2000, print_mode=0, save_mode=0,
                    show_mode=0, random_seed=0, hard_deterministic_mode=True)
    coords = {"A": (0, 0), "B": (1000, 0), "C": (2000, 0), "D": (3000, 0), "E": (1000, 1000)}
    for nm, (x, y) in coords.items():
        W.addNode(nm, x, y)
    for a, b in [("A", "B"), ("B", "C"), ("C", "D"), ("A", "E"), ("E", "C")]:
        W.addLink(f"{a}{b}", a, b, length=1000, free_flow_speed=20, number_of_lanes=1)
    W.adddemand("A", "D", 0, 300, volume=12)
    W.adddemand("A", "C", 0, 300, volume=6)
    return W


def _seq_new(veh):
    """新法：直接讀 log_t_link 的 Link 序列名字（uxsim_engine._readback 用的同一式子）。"""
    return [e[1].name for e in veh.log_t_link if not isinstance(e[1], str)]


def _seq_old(veh):
    """舊法：traveled_route()[0].links 名字。未啟動/無 log → None（跳過比對）。"""
    try:
        return [l.name for l in veh.traveled_route()[0].links]
    except Exception:  # noqa: BLE001
        return None


def test_edge_extraction_equivalent_every_step():
    W = _build_world()
    saw_multilink = False
    prev_len = {}
    for _ in range(20):
        W.exec_simulation(duration_t=100)   # 100s × free_flow 20m/s ÷ 1000m → ~2 link/步（跨多 link）
        for veh in W.VEHICLES.values():
            new = _seq_new(veh)
            old = _seq_old(veh)
            if old is None:
                continue
            assert new == old, f"veh {veh.name}: new={new} old={old}"   # 逐元素等價
            # 確認真的有「一步跨多 link」發生（A2）
            pl = prev_len.get(veh.name, 0)
            if len(new) - pl >= 2:
                saw_multilink = True
            prev_len[veh.name] = len(new)
    assert saw_multilink, "測試情境應包含一步跨多條 link 的車（否則沒測到 A2）"


def test_empty_log_safe():
    """剛建、未啟動的車：新法回空、不丟例外。"""
    W = _build_world()
    # 不推進，直接看尚未進入網路的車
    for veh in W.VEHICLES.values():
        assert _seq_new(veh) == []   # 無 Link 條目 → 空（與舊法的空一致）
