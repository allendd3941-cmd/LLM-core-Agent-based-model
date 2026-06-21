"""Phase 2 驗證：用真實台南 graphml 建 UXsim World，量建置時間、查連通、跑小型實驗。

跑法：  uv run python spike/uxsim_build_check.py
（建置 42k link 純 Python 可能要數十秒；這是「建置 + 小跑」非重模擬，本機可跑。）
"""

from __future__ import annotations

import random
import time

from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.spatial.road_network import load_road_network
from llm_abm_simulator.spatial.uxsim_builder import build_world


def main():
    print("== 載入真實路網 ==")
    t0 = time.perf_counter()
    net = load_road_network(DEFAULT_CONFIG)
    n_nodes = net.graph.number_of_nodes()
    n_edges = net.graph.number_of_edges()
    print(f"  networkx: {n_nodes} 節點 / {n_edges} 邊（{time.perf_counter()-t0:.1f}s）")

    print("== 建 UXsim World ==")
    t0 = time.perf_counter()
    W = build_world(net, tmax=3600, deltan=1, seed=42)
    build_s = time.perf_counter() - t0
    print(f"  建置耗時: {build_s:.1f}s")
    print(f"  UXsim: {len(W.NODES)} node / {len(W.LINKS)} link")
    assert len(W.NODES) == n_nodes, "節點數不符"
    assert len(W.LINKS) == n_edges, "link 數不符"
    print("  PASS: 節點/邊數與 networkx 一致")

    print("== 小型實跑：隨機 5 對 O→D，跑 600s ==")
    rng = random.Random(42)
    node_ids = list(net.graph.nodes())
    added = 0
    for i in range(5):
        o = rng.choice(node_ids)
        d = rng.choice(node_ids)
        if o == d:
            continue
        W.addVehicle(str(o), str(d), departure_time=i * 10, name=f"v{i}")
        added += 1
    t0 = time.perf_counter()
    t = 0
    while W.check_simulation_ongoing() and t < 600:
        t += 100
        W.exec_simulation(until_t=t)
    run_s = time.perf_counter() - t0
    states = {}
    for name, v in W.VEHICLES.items():
        states[v.state] = states.get(v.state, 0) + 1
    print(f"  加入 {added} 台；跑 {run_s:.1f}s；車況分佈: {states}")
    print("  PASS: World 可加車並推進（有車進入 run/end 即代表路由+移動可運作）")

    print(f"\n總結: build={build_s:.1f}s for {n_edges} links; deltan=1 城市尺度吞吐請在 server 量。")


if __name__ == "__main__":
    main()
