"""routing.py — 路徑規劃（加權最短路徑 + 動態權重）。

對齊 GAML：
- ``path_between`` / ``goto on: road_network`` → ``find_path``。
- ``with_weights`` 依 flow 動態調整權重，使壅塞路段較不易被選 → 權重函式讀 Road 即時狀態。
- ``goto recompute_path: is_crowded`` → 引擎在 agent crowded 時呼叫 ``find_path`` 重算。

權重結合「道路即時壅塞」與「agent 的 active_mode 偏好」（time/distance/comfort/capacity）。
"""

from __future__ import annotations

import logging
from typing import Any

import networkx as nx

from .road_network import RoadNetwork

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS = {"time": 0.45, "distance": 0.25, "comfort": 0.20, "capacity": 0.10}


def find_path(
    network: RoadNetwork,
    origin_node: str,
    dest_node: str,
    weights: dict[str, float] | None = None,
) -> list[str]:
    """加權最短路徑；找不到時回傳空 list（對齊 GAML 找不到 path 的情況）。

    Args:
        weights: active_mode 的 time/distance/comfort/capacity 權重。
    """
    if origin_node == dest_node:
        return [origin_node]

    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    graph = network.graph

    def _weight(u: Any, v: Any, _data: dict) -> float:
        road = network.road_between(u, v)
        if road is None:
            return 1.0
        length = max(road.length, 1.0)
        speed = max(road.speed_car, 1.0)
        congestion = road.congestion_proxy
        time_factor = length / speed                # 越慢成本越高
        comfort_factor = 1.0 + congestion * 1.5     # 壅塞降低舒適
        congestion_factor = 1.0 + congestion * 2.0  # 直接反映壅塞（鏡像 GAML flow 加權）
        cost = length * (
            w["time"] * time_factor / 100.0
            + w["distance"] * 1.0
            + w["comfort"] * comfort_factor
            + w["capacity"] * congestion_factor
        )
        return max(cost, 1e-3)

    try:
        return list(nx.shortest_path(graph, source=origin_node, target=dest_node, weight=_weight))
    except (nx.NetworkXNoPath, nx.NodeNotFound) as e:
        logger.debug("找不到路徑 %s → %s: %s", origin_node, dest_node, e)
        return []


def path_length_m(network: RoadNetwork, path: list[str]) -> float:
    """整條路徑的公尺長度。"""
    total = 0.0
    for a, b in zip(path, path[1:]):
        road = network.road_between(a, b)
        total += road.length if road else 0.0
    return total
