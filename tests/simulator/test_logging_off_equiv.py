"""OOM 修法驗收：關掉 UXsim 每步軌跡記錄（vehicle_logging_interval=-1 + reduce_memory_route_pref）
**不影響偵測器與模組邏輯**。

做法：同一模擬跑兩次——logging ON（interval=1, reduce=False，舊行為）vs OFF（-1, reduce=True，新預設）——
比對：① 每車位置/狀態指紋 ② 進入邊序列 ③ 偵測器累計計數，三者**逐一相等**。
原理：logging 只是觀測記錄、不影響物理；log_t_link（換 link 才記）兩種設定下都照樣產生 → 偵測器輸入不變。

⚠ 刻意在 **dev-crop 4km 小網路**上跑（非全台南）：此不變式與網路大小無關，小網路驗結論一樣，
但避免「建兩個全網路 World（各 ~9GB route_pref/dist/next）」在記憶體有限機器 OOM。4km 仍含足夠相機 +
車流經過（見下方 V2 守護斷言），對拍有實質內容。
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine
from llm_abm_simulator.spatial import gis_loader

pytest.importorskip("uxsim")

STEPS = 4
CROP_KM = 4.0   # 含 5km 內大半相機 + 球場周邊路網；夠對拍、又遠小於全網路（避免 9GB OOM）


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


def _detector_total(det) -> int:
    """偵測器累計計數總和（V2：確認對拍有實質內容、非空過）。"""
    return sum(v for _, a, b in det for (_, v) in list(a) + list(b))


def _run(interval: int):
    orig = config.UXSIM_CONFIG
    try:
        # dev-crop 4km：小網路 → route_pref/dist/next 由 ~9GB 降到 MB 級（不 OOM、不需 gc）。
        config.UXSIM_CONFIG = dataclasses.replace(
            orig, vehicle_logging_interval=interval,
            reduce_memory_route_pref=(interval == -1), dev_crop_km=CROP_KM)
        config.set_runtime_ambient_count(250)
        cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=180, max_steps=STEPS,
                                  step_minutes=5, use_llm=False, seed=7)
        eng = UXsimEngine(cfg)
        eng.set_detectors(gis_loader.load_default_detectors())   # 載入預設相機（否則偵測器=0、對拍空過）
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
        det = _detector_counts(eng)
        return pos.hexdigest(), edges.hexdigest(), det, entered_total, len(eng._detectors)
    finally:
        config.UXSIM_CONFIG = orig


def test_logging_off_does_not_affect_detectors_or_logic():
    pos_on, edges_on, det_on, ent_on, ndet_on = _run(1)        # logging ON（舊行為）
    pos_off, edges_off, det_off, ent_off, ndet_off = _run(-1)  # logging OFF（新預設）

    # V2 守護有實質內容（否則「比較兩個空的」會假性通過）：crop 內有相機、有車經過、有進入邊。
    assert ndet_off >= 1, "crop 內無偵測器 → 對拍無意義（放大 CROP_KM）"
    assert _detector_total(det_off) > 0, "偵測器計數為 0 → 沒車經過相機，對拍空過（放大 CROP_KM）"
    assert ent_off > 0, "logging 關掉後進入邊為空 → log_t_link 沒運作"

    # 核心等價：logging on/off 下，偵測器輸入(進入邊)、輸出(計數)、車輛位置/狀態 全部逐一相等。
    assert ent_on == ent_off
    assert edges_on == edges_off, "進入邊序列在 logging on/off 下不一致"
    assert det_on == det_off, "偵測器計數在 logging on/off 下不一致"
    assert pos_on == pos_off, "車輛位置/狀態在 logging on/off 下不一致"
