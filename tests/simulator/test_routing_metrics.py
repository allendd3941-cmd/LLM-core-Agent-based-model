"""routing 與 metrics 的單元測試（用 synthetic 小路網，不依賴 OSM/網路）。"""

from __future__ import annotations

import dataclasses

from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.domain.agent import VehicleAgent
from llm_abm_simulator.simulation import metrics
from llm_abm_simulator.spatial import routing
from llm_abm_simulator.spatial.road_network import build_synthetic_graph, _wrap


def _synthetic_network():
    cfg = dataclasses.replace(DEFAULT_CONFIG, synthetic_grid_size=5)
    return _wrap(build_synthetic_graph(cfg))


def test_find_path_connected():
    net = _synthetic_network()
    nodes = list(net.graph.nodes())
    path = routing.find_path(net, nodes[0], nodes[-1])
    assert len(path) >= 2
    assert path[0] == nodes[0] and path[-1] == nodes[-1]


def test_find_path_same_node():
    net = _synthetic_network()
    n = list(net.graph.nodes())[0]
    assert routing.find_path(net, n, n) == [n]


def test_find_path_unknown_node():
    net = _synthetic_network()
    n = list(net.graph.nodes())[0]
    assert routing.find_path(net, n, "does_not_exist") == []


def test_path_length_positive():
    net = _synthetic_network()
    nodes = list(net.graph.nodes())
    path = routing.find_path(net, nodes[0], nodes[-1])
    assert routing.path_length_m(net, path) > 0


def _path_cost(net, path, strategy, seed=0, avoid_circles=None):
    total = 0.0
    for a, b in zip(path, path[1:]):
        road = net.road_between(a, b)
        assert road is not None, f"邊不存在: {a}->{b}"
        total += routing._edge_cost(net, a, b, road, strategy, seed, avoid_circles)
    return total


def test_destination_tree_matches_find_path():
    """終點樹路徑：connected 到終點，且 randomness=0 時與 find_path 同為最小成本路徑。"""
    net = _synthetic_network()
    nodes = list(net.graph.nodes())
    strategy = {"time": 0.45, "distance": 0.25, "comfort": 0.20, "capacity": 0.10, "randomness": 0.0}
    dest = nodes[-1]
    origins = [nodes[0], nodes[1], nodes[2]]
    trees = routing.DestinationTrees(net, strategy, seed=42)
    paths = trees.paths_to(dest, origins)
    for o in origins:
        tp = paths[o]
        fp = routing.find_path(net, o, dest, strategy, seed=42)
        assert tp and tp[0] == o and tp[-1] == dest        # 樹路徑連通到終點
        assert fp and fp[0] == o and fp[-1] == dest
        assert abs(_path_cost(net, tp, strategy) - _path_cost(net, fp, strategy)) < 1e-6


def test_destination_tree_matches_find_path_with_avoid_circles():
    """終點樹吃 avoid_circles（取代逐車退回）：與 find_path 在同樣避讓圈下仍是同成本最短路。"""
    net = _synthetic_network()
    nodes = list(net.graph.nodes())
    strategy = {"time": 0.45, "distance": 0.25, "comfort": 0.20, "capacity": 0.10, "randomness": 0.0}
    o, dest = nodes[0], nodes[-1]
    mid = nodes[len(nodes) // 2]
    mx, my = net.node_xy(mid)
    circles = [(mx, my, 1.0)]   # 在中間節點放一個避讓圈（進入該節點的邊 ×懲罰）
    tp = routing.DestinationTrees(net, strategy, seed=42, avoid_circles=circles).paths_to(dest, [o])[o]
    fp = routing.find_path(net, o, dest, strategy, seed=42, avoid_circles=circles)
    assert tp and tp[0] == o and tp[-1] == dest
    assert fp and fp[0] == o and fp[-1] == dest
    assert abs(_path_cost(net, tp, strategy, avoid_circles=circles)
               - _path_cost(net, fp, strategy, avoid_circles=circles)) < 1e-6


def test_destination_tree_edge_cases():
    net = _synthetic_network()
    nodes = list(net.graph.nodes())
    trees = routing.DestinationTrees(net, {"randomness": 0.0}, seed=0)
    assert trees.paths_to(nodes[0], [nodes[0]])[nodes[0]] == [nodes[0]]   # 起點＝終點
    assert trees.paths_to("nope", [nodes[0]])[nodes[0]] == []             # 終點不在圖內
    assert trees.paths_to(nodes[0], ["nope"])["nope"] == []              # 起點不在圖內


def test_overall_environment_counts():
    net = _synthetic_network()
    roads = net.all_roads()
    roads[0].update_flow(20, 10.0, 2.0)   # 製造一條壅塞道路
    env = metrics.overall_environment(roads, DEFAULT_CONFIG, agent_count=3)
    assert env["active_road_count"] >= 1
    assert env["crowded_road_count"] >= 1
    assert env["average_congestion_proxy"] > 0


def test_distributions():
    a1 = VehicleAgent.from_config("v1", DEFAULT_CONFIG); a1.action_mode = "fast"
    a2 = VehicleAgent.from_config("v2", DEFAULT_CONFIG); a2.action_mode = "fast"
    mode, status = metrics.distributions([a1, a2])
    assert mode["fast"] == 2
    assert sum(status.values()) == 2
