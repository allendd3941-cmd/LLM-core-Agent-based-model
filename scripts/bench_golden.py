"""bench_golden.py — readback/並行優化的「黃金指紋」+ 效能基準(讀-only 量測，不進 CI)。

用途：
- `fingerprint(...)`：跑固定情境，把每 cycle 的 (agent 狀態 + road 壅塞) 串成 SHA256
  → 「不改結果」的逐位元裁判。改動前後、B-on/B-off 都該得到同一個值。
- `bench(...)`：輸出每 phase 計時 + _readback cProfile 熱點(改前/改後對比 readback 是否變快)。

用法：
    uv run python scripts/bench_golden.py            # 印 H0 指紋 + phase 計時
    UXSIM_DEV_CROP_KM=8 uv run python scripts/bench_golden.py
"""

from __future__ import annotations

import cProfile
import dataclasses
import hashlib
import io
import pstats
import time

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine

# 固定情境（對齊：step 5min=300s、duo_update_time 600s → route_search 邊界落在每 2 步的起點）
SCENARIO = dict(n_event=1500, n_amb=2000, steps=10, step_minutes=5, seed=7)


def _make_engine(scn=SCENARIO):
    config.set_runtime_ambient_count(scn["n_amb"])
    cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=scn["n_event"], max_steps=scn["steps"],
                              step_minutes=scn["step_minutes"], use_llm=False, seed=scn["seed"])
    eng = UXsimEngine(cfg)
    eng.initialize()
    eng.resume()
    return eng


def fingerprint(scn=SCENARIO) -> str:
    """跑固定情境，回傳每 cycle (agent 狀態 + road 壅塞) 的 SHA256。確定性 → 不改結果就同值。"""
    eng = _make_engine(scn)
    h = hashlib.sha256()
    for _ in range(scn["steps"]):
        eng.step()
        for a in sorted(eng.agents, key=lambda x: x.agent_id):
            h.update(f"{a.agent_id}|{round(a.x, 2)}|{round(a.y, 2)}|"
                     f"{a.route_status}|{a.action_mode}|{a.selected_action}\n".encode())
        for rid in sorted(eng._roads_by_id):
            h.update(f"{rid}|{eng._roads_by_id[rid].congestion_proxy}\n".encode())
    return h.hexdigest()


def bench(scn=SCENARIO):
    """phase 計時 + _readback cProfile 熱點。"""
    eng = _make_engine(scn)
    phase = {}
    for _ in range(scn["steps"]):
        t0 = time.perf_counter()
        eng.step()
        phase.setdefault("step_total", []).append(time.perf_counter() - t0)
    # _readback 內部熱點
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(3):
        eng._readback(eng.scheduler.cycle)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(8)
    avg = sum(phase["step_total"]) / len(phase["step_total"])
    print(f"[bench] 每步平均 {avg:.2f}s（{scn['steps']} 步）")
    print("[bench] _readback cProfile（tottime 前8）：")
    print(s.getvalue())


if __name__ == "__main__":
    t0 = time.perf_counter()
    fp = fingerprint()
    print(f"GOLDEN_FINGERPRINT = {fp}")
    print(f"（指紋計算耗時 {time.perf_counter() - t0:.1f}s）")
    bench()
