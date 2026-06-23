"""bench_arrival_spread.py — 抵達圈 K-nearest 分流的分佈量測（讀-only，不進 CI）。

比較不同 arrival_gates_per_car（K）下，事件車終點在抵達圈節點上的分佈集中度：
用到幾個節點、top-1 / top-3 佔比、每節點最大車數。用來挑 K + 佐證分流改善。

    UXSIM_DEV_CROP_KM=8 uv run python scripts/bench_arrival_spread.py
"""

from __future__ import annotations

import dataclasses
from collections import Counter

from llm_abm_simulator import config
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.uxsim_engine import UXsimEngine

N = 2000


def measure(k: int, radius: float = 600.0):
    config.set_runtime_ambient_count(0)
    cfg = dataclasses.replace(DEFAULT_CONFIG, nb_agents=N, max_steps=1, step_minutes=5,
                              use_llm=False, seed=7, arrival_radius_m=radius,
                              arrival_gates_per_car=k)
    eng = UXsimEngine(cfg)
    eng.initialize()
    ev = [a for a in eng.agents if a.role == "event"]
    c = Counter(a.destination_node for a in ev)
    counts = sorted(c.values(), reverse=True)
    tot = sum(counts)
    return {"k": k, "avail": len(eng._arrival_nodes), "used": len(c),
            "top1": counts[0] / tot, "top3": sum(counts[:3]) / tot, "maxn": counts[0]}


if __name__ == "__main__":
    for k in (1, 3, 5, 8):
        d = measure(k)
        print(f"RES K={d['k']}: 用 {d['used']}/{d['avail']} 節點 | "
              f"top1={d['top1']:.0%} top3={d['top3']:.0%} max/節點={d['maxn']}")
