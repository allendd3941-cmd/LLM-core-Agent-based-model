"""routing.py — 路徑規劃（加權最短路徑 + 動態權重）。

對齊 GAML：
- ``path_between`` / ``goto on: road_network`` → ``find_path``。
- ``with_weights`` 依 flow 動態調整權重，使壅塞路段較不易被選 → 權重函式讀 Road 即時狀態。
- ``goto recompute_path: is_crowded`` → 引擎在 agent crowded 時呼叫 ``find_path`` 重算。

權重結合「道路即時壅塞」與「agent 的 active_mode 偏好」（time/distance/comfort/capacity）。
"""

from __future__ import annotations

import logging
import zlib
from typing import Any

import networkx as nx

from .road_network import RoadNetwork

logger = logging.getLogger(__name__)

_DEFAULT_WEIGHTS = {"time": 0.45, "distance": 0.25, "comfort": 0.20, "capacity": 0.10}

# road_class_bias 用的路型分類（幹道 vs 小路）。
_MAJOR_HIGHWAYS = {"motorway", "trunk", "primary", "secondary",
                   "motorway_link", "trunk_link", "primary_link", "secondary_link"}
_MINOR_HIGHWAYS = {"residential", "service", "living_street", "unclassified", "track", "road"}
_AVOID_MULTIPLIER = 25.0   # congestion > avoid_threshold 的邊：成本放大此倍數（近乎封路）


def _edge_jitter(u: str, v: str, seed: int, salt: str, randomness: float) -> float:
    """每邊的確定性微擾係數 ∈ [1-r, 1+r]。

    用穩定 hash（zlib.crc32，跨進程一致；不可用 Python hash() 因字串 hash 有 salt）
    從 (u, v, seed, salt) 推出，確保「同 seed 同軌跡」且不同 agent（salt=agent_id）走散。
    """
    key = f"{u}|{v}|{seed}|{salt}".encode("utf-8")
    frac = (zlib.crc32(key) % 10_000) / 10_000.0       # [0, 1)
    return 1.0 + (frac * 2.0 - 1.0) * randomness        # [1-r, 1+r]


def find_path(
    network: RoadNetwork,
    origin_node: str,
    dest_node: str,
    strategy: dict[str, Any] | None = None,
    seed: int = 0,
) -> list[str]:
    """加權最短路徑；找不到時回傳空 list（對齊 GAML 找不到 path 的情況）。

    Args:
        strategy: active_mode 的路徑策略。除四個權重 time/distance/comfort/capacity 外，
            可選旗標（皆有「關閉」預設，省略時行為等同原最短路徑）：
            ``congestion_penalty``（額外壅塞懲罰倍率）、``avoid_threshold``（硬避開門檻）、
            ``road_class_bias``（偏好幹道）、``randomness``（每邊微擾）、``salt``（分散用，通常 agent_id）。
        seed: 與 randomness 搭配的可重現亂數種子。
    """
    if origin_node == dest_node:
        return [origin_node]

    s = strategy or {}
    w_time = s.get("time", _DEFAULT_WEIGHTS["time"])
    w_distance = s.get("distance", _DEFAULT_WEIGHTS["distance"])
    w_comfort = s.get("comfort", _DEFAULT_WEIGHTS["comfort"])
    w_capacity = s.get("capacity", _DEFAULT_WEIGHTS["capacity"])
    congestion_penalty = float(s.get("congestion_penalty", 0.0))
    avoid_threshold = float(s.get("avoid_threshold", 1.0))
    road_class_bias = float(s.get("road_class_bias", 0.0))
    randomness = float(s.get("randomness", 0.0))
    salt = str(s.get("salt", ""))
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
            w_time * time_factor / 100.0
            + w_distance * 1.0
            + w_comfort * comfort_factor
            + w_capacity * congestion_factor
        )
        # --- active_mode 路徑策略旗標 ---
        if congestion_penalty:                       # 額外壅塞懲罰（avoid 用）
            cost *= 1.0 + congestion_penalty * congestion
        if congestion > avoid_threshold:             # 硬避開高壅塞邊（近乎封路）
            cost *= _AVOID_MULTIPLIER
        if road_class_bias:                          # 偏好幹道、懲罰小路（comfortable 用）
            hw = road.highway.split(",")[0].strip().strip("[]'\" ")
            if hw in _MAJOR_HIGHWAYS:
                cost *= max(1e-3, 1.0 - road_class_bias)
            elif hw in _MINOR_HIGHWAYS:
                cost *= 1.0 + road_class_bias
        if randomness:                               # 確定性微擾，分散車流
            cost *= _edge_jitter(u, v, seed, salt, randomness)
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
