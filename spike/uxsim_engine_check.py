"""Phase 3 驗證：UXsimEngine（rule/DUO 核心）在小裁切網路上 init→step→snapshot→reset。

驗證 facade 契約：step() 回合法 SimulationState（agents 有座標、會移動/抵達；roads 有流量）。
全市規模 + LLM 屬 server/LLM 驗證。

跑法：  UXSIM_DEV_CROP_KM=8 uv run python spike/uxsim_engine_check.py
"""

from __future__ import annotations

import dataclasses
import os
from collections import Counter

os.environ.setdefault("UXSIM_DEV_CROP_KM", "8")   # 本機開發裁切

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine


def main():
    config.set_runtime_ambient_count(0)   # 先關背景車，專注事件車 ingress
    cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=60, max_steps=8,
                              step_minutes=5, use_llm=False)
    eng = UXsimEngine(cfg)
    eng.initialize()
    print(f"== init ==  agents={len(eng.agents)}  vehicles_added={len(eng._veh)}  "
          f"network={eng.network.graph.number_of_nodes()} nodes")
    assert eng.is_initialized
    assert len(eng._veh) > 0, "沒有任何車被加入 World（裁切/放置問題）"

    eng.resume()
    last_msg = None
    for _ in range(cfg.max_steps):
        st = eng.step()
        last_msg = st.to_message()
        status = Counter(str(a.route_status).split(".")[-1] for a in eng.agents)
        arrived = sum(1 for a in eng.agents if a.arrival_cycle is not None)
        print(f"  step {st.cycle}: msg_agents={len(last_msg['agents'])} "
              f"roads={len(last_msg['roads'])} status={dict(status)} arrived={arrived} "
              f"avg_cong={last_msg['metrics']['average_congestion_proxy']:.3f}")

    # --- 驗證 SimulationState 契約 ---
    assert last_msg["type"] == "state_update"
    for key in ("cycle", "agents", "roads", "metrics", "mode_distribution",
                "status_distribution", "decision_health"):
        assert key in last_msg, f"缺少 {key}"
    if last_msg["agents"]:
        a0 = last_msg["agents"][0]
        for key in ("agent_id", "lat", "lng", "route_status", "speed_kmh"):
            assert key in a0, f"agent 缺少 {key}"
        assert -90 <= a0["lat"] <= 90 and 100 <= a0["lng"] <= 130, f"座標不合理: {a0['lat']},{a0['lng']}"
    moved = any(a.route_status.name in ("MOVING", "ARRIVED") for a in eng.agents)
    assert moved, "沒有任何車移動/抵達"
    print("  PASS: SimulationState 契約合法、agents 有合理座標、車有移動/抵達")

    # --- reset ---
    eng.reset()
    assert eng._world is None and eng.scheduler.cycle == 0
    print("  PASS: reset 清空 world 與 scheduler")
    print("\nPhase 3 核心（rule/DUO + ingress）本機驗證通過。LLM/散場/全市規模 待 server/LLM 驗證。")


if __name__ == "__main__":
    main()
