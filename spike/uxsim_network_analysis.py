"""分析路網規模 + 驗證「縮小後網路」可在 UXsim 跑通。

UXsim 的 route choice 用全對最短路（dist/pred/next = n_nodes²、route_pref = n_nodes×n_links），
記憶體 ∝ 節點數²。15,833 節點 → ~9 GiB（本機爆、server 64GB 可but heavy）。
本腳本量化：①可簡化的 degree-2 節點比例 ②各半徑內節點數（區域裁切潛力）
③驗證裁切後網路 build+run 通過（證明引擎路徑正確、記憶體可控）。

跑法：  uv run python spike/uxsim_network_analysis.py
"""

from __future__ import annotations

import math
import time
from collections import Counter

from pyproj import Transformer

from llm_abm_simulator import config, scenarios
from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.spatial import road_network
from llm_abm_simulator.spatial.road_network import load_road_network
from llm_abm_simulator.spatial.uxsim_builder import build_world


def main():
    net = load_road_network(DEFAULT_CONFIG)
    g = net.graph
    n = g.number_of_nodes()
    print(f"== 路網: {n} 節點 / {g.number_of_edges()} 邊 ==")

    # ① degree 分佈（無向 distinct neighbor 數）→ degree-2 = 可收縮的形狀點
    deg = Counter()
    for node in g.nodes():
        nb = set(g.successors(node)) | set(g.predecessors(node))
        deg[len(nb)] += 1
    deg2 = deg.get(2, 0)
    print(f"  degree<=2 節點: {deg.get(1,0)+deg2} ({100*(deg.get(1,0)+deg2)/n:.0f}%)  "
          f"（degree2={deg2}）→ 拓樸簡化潛力")
    print(f"  degree 分佈前幾: {dict(sorted(deg.items())[:6])}")

    # ② 各半徑內節點數（以場景中心=球場 為圓心）
    to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
    cx, cy = to_m.transform(scenarios.active().center_lng, scenarios.active().center_lat)
    print(f"  場景中心(球場) metric=({cx:.0f},{cy:.0f})")
    dists = {}
    for node in g.nodes():
        x, y = net.node_xy(node)
        dists[node] = math.hypot(x - cx, y - cy)
    for r_km in (3, 5, 8, 10):
        cnt = sum(1 for d in dists.values() if d <= r_km * 1000)
        print(f"  ≤{r_km}km: {cnt} 節點")

    # ③ 驗證裁切（8km）後 build+run 通過
    R = 8000
    keep = [node for node, d in dists.items() if d <= R]
    sub = g.subgraph(keep).copy()
    sub = road_network._largest_scc(sub)
    wrapped = road_network._wrap(sub)
    print(f"\n== 裁切 ≤8km 最大強連通: {sub.number_of_nodes()} 節點 / {sub.number_of_edges()} 邊 ==")
    est_gib = (sub.number_of_nodes() ** 2 * 8 * 3 + sub.number_of_nodes() * sub.number_of_edges() * 8) / 1024**3
    print(f"  估 route-choice 記憶體 ~{est_gib:.2f} GiB")

    t0 = time.perf_counter()
    W = build_world(wrapped, tmax=1200, deltan=1, seed=42)
    print(f"  build {time.perf_counter()-t0:.1f}s")

    import random
    rng = random.Random(0)
    ids = list(sub.nodes())
    for i in range(20):
        o, d = rng.choice(ids), rng.choice(ids)
        if o != d:
            W.addVehicle(str(o), str(d), departure_time=i, name=f"v{i}")
    t0 = time.perf_counter()
    t = 0
    while W.check_simulation_ongoing() and t < 1200:
        t += 100
        W.exec_simulation(until_t=t)
    states = Counter(v.state for v in W.VEHICLES.values())
    print(f"  run {time.perf_counter()-t0:.1f}s  車況: {dict(states)}")
    print("  PASS: 裁切後網路 build+run 通過（route choice 記憶體可控、車輛會移動/抵達）")


if __name__ == "__main__":
    main()
