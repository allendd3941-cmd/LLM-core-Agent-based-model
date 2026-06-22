"""OOM 修法驗收：關掉 UXsim 每步軌跡記錄（vehicle_logging_interval=-1 + reduce_memory_route_pref）
**不影響偵測器與模組邏輯**。

做法：同一模擬跑兩次——logging ON（interval=1, reduce=False，舊行為）vs OFF（-1, reduce=True，新預設）——
比對：① 每車位置/狀態指紋 ② 進入邊序列 ③ 偵測器累計計數，三者**逐一相等**。
原理：logging 只是觀測記錄、不影響物理；log_t_link（換 link 才記）兩種設定下都照樣產生 → 偵測器輸入不變。
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine

pytest.importorskip("uxsim")

STEPS = 4


def _detector_counts(eng):
    """所有偵測器 a/b 方向的累計計數（攤平成可比對的排序 tuple）。"""
    out = []
    for d in eng._detectors:
        a = d.get("a", {}) or {}
        b = d.get("b", {}) or {}
        out.append((d["id"],
                    tuple(sorted((k, int(v)) for k, v in a.items())),
                    tuple(sorted((k, int(v)) for k, v in b.items()))))
    return sorted(out)


def _run(interval: int):
    orig = config.UXSIM_CONFIG
    try:
        config.UXSIM_CONFIG = dataclasses.replace(
            orig, vehicle_logging_interval=interval, reduce_memory_route_pref=(interval == -1))
        config.set_runtime_ambient_count(250)
        cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=180, max_steps=STEPS,
                                  step_minutes=5, use_llm=False, seed=7)
        eng = UXsimEngine(cfg)
        eng.initialize()
        eng.resume()
        pos = hashlib.sha256()
        edges = hashlib.sha256()
        entered_total = 0
        for _ in range(STEPS):
            eng.step()
            for a in sorted(eng.agents, key=lambda x: x.agent_id):
                pos.update(f"{a.agent_id}|{round(a.x, 2)}|{round(a.y, 2)}|"
                           f"{a.route_status}|{a.selected_action}\n".encode())
            for aid in sorted(eng._step_entered_edges):
                rids = eng._step_entered_edges[aid]
                edges.update(f"{aid}|{','.join(rids)}\n".encode())
                entered_total += len(rids)
        return pos.hexdigest(), edges.hexdigest(), _detector_counts(eng), entered_total
    finally:
        config.UXSIM_CONFIG = orig


def test_logging_off_does_not_affect_detectors_or_logic():
    pos_on, edges_on, det_on, ent_on = _run(1)     # logging ON（舊行為）
    pos_off, edges_off, det_off, ent_off = _run(-1)  # logging OFF（新預設）

    # 機制有運作：關掉每步 log 後，進入邊仍非空（log_t_link 存活）→ 偵測器有東西可數
    assert ent_off > 0, "logging 關掉後進入邊為空 → log_t_link 沒運作"
    # 進入邊序列完全相同（偵測器的「輸入」不受 logging 影響）
    assert ent_on == ent_off
    assert edges_on == edges_off, "進入邊序列在 logging on/off 下不一致"
    # 偵測器累計計數完全相同（偵測器「輸出」不受影響）
    assert det_on == det_off, "偵測器計數在 logging on/off 下不一致"
    # 車輛位置/狀態完全相同（物理/路由/模組邏輯不受影響）
    assert pos_on == pos_off, "車輛位置/狀態在 logging on/off 下不一致"
