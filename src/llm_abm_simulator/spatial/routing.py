"""routing.py — 路徑規劃（加權最短路徑 + 動態權重）。

對齊 GAML：
- ``path_between`` / ``goto on: road_network`` → ``find_path``。
- ``with_weights`` 依 flow 動態調整權重，使壅塞路段較不易被選 → 權重函式讀 Road 即時狀態。
- ``goto recompute_path: is_crowded`` → 引擎在 agent crowded 時呼叫 ``find_path`` 重算。

權重結合「道路即時壅塞」與「agent 的 action_mode 偏好」（time/distance/comfort/capacity）。
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
    avoid_circles: list[tuple[float, float, float]] | None = None,
) -> list[str]:
    """加權最短路徑；找不到時回傳空 list（對齊 GAML 找不到 path 的情況）。

    Args:
        strategy: action_mode 的路徑策略。除四個權重 time/distance/comfort/capacity 外，
            可選旗標（皆有「關閉」預設，省略時行為等同原最短路徑）：
            ``congestion_penalty``（額外壅塞懲罰倍率）、``avoid_threshold``（硬避開門檻）、
            ``road_class_bias``（偏好幹道）、``randomness``（每邊微擾）、``salt``（分散用，通常 agent_id）。
        seed: 與 randomness 搭配的可重現亂數種子。
    """
    if origin_node == dest_node:
        return [origin_node]

    s = strategy or {}
    graph = network.graph

    def _weight(u: Any, v: Any, _data: dict) -> float:
        road = network.road_between(u, v)
        if road is None:
            return 1.0
        return _edge_cost(network, u, v, road, s, seed, avoid_circles)   # 與終點樹共用同一成本公式

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


# ---------------------------------------------------------------------------
# 終點樹路由（城市尺度 init 用）：同一 action_mode 用一張靜態權重圖，對每個終點只算一次
# 反向 Dijkstra（scipy C 層），每車沿前驅讀路徑 → 取代逐車 networkx 搜尋。
# 語意：同 mode+終點的車走相同路徑（無 per-car jitter）；congestion=0 時與 find_path 等價（salt 除外）。
# ---------------------------------------------------------------------------
def strategy_signature(s: dict[str, Any]) -> tuple:
    """同一 action_mode 的策略簽章（不含 per-agent salt）——終點樹分組用。"""
    return (
        s.get("time", _DEFAULT_WEIGHTS["time"]),
        s.get("distance", _DEFAULT_WEIGHTS["distance"]),
        s.get("comfort", _DEFAULT_WEIGHTS["comfort"]),
        s.get("capacity", _DEFAULT_WEIGHTS["capacity"]),
        float(s.get("congestion_penalty", 0.0)),
        float(s.get("avoid_threshold", 1.0)),
        float(s.get("road_class_bias", 0.0)),
        float(s.get("randomness", 0.0)),
    )


def _edge_cost(network: RoadNetwork, u: str, v: str, road, s: dict[str, Any],
               seed: int, avoid_circles=None) -> float:
    """單邊成本公式：含「當前壅塞」(road.congestion_proxy) + action_mode 旗標 + jitter + avoid_circles。

    find_path 與終點樹**共用同一個公式** → 兩者等價(終點樹只是把 salt 固定成 ""＝不分車、無 per-car jitter)。
    init 時各路段 congestion=0 → 自然退化成自由流成本。
    """
    length = max(road.length, 1.0)
    speed = max(road.speed_car, 1.0)
    congestion = road.congestion_proxy
    time_factor = length / speed
    comfort_factor = 1.0 + congestion * 1.5
    congestion_factor = 1.0 + congestion * 2.0
    cost = length * (
        s.get("time", _DEFAULT_WEIGHTS["time"]) * time_factor / 100.0
        + s.get("distance", _DEFAULT_WEIGHTS["distance"]) * 1.0
        + s.get("comfort", _DEFAULT_WEIGHTS["comfort"]) * comfort_factor
        + s.get("capacity", _DEFAULT_WEIGHTS["capacity"]) * congestion_factor
    )
    congestion_penalty = float(s.get("congestion_penalty", 0.0))
    if congestion_penalty:
        cost *= 1.0 + congestion_penalty * congestion
    if congestion > float(s.get("avoid_threshold", 1.0)):
        cost *= _AVOID_MULTIPLIER
    road_class_bias = float(s.get("road_class_bias", 0.0))
    if road_class_bias:
        hw = road.highway.split(",")[0].strip().strip("[]'\" ")
        if hw in _MAJOR_HIGHWAYS:
            cost *= max(1e-3, 1.0 - road_class_bias)
        elif hw in _MINOR_HIGHWAYS:
            cost *= 1.0 + road_class_bias
    randomness = float(s.get("randomness", 0.0))
    if randomness:
        cost *= _edge_jitter(u, v, seed, str(s.get("salt", "")), randomness)
    if avoid_circles:
        vx, vy = network.node_xy(v)
        for cx, cy, r in avoid_circles:
            if (vx - cx) ** 2 + (vy - cy) ** 2 <= r * r:
                cost *= _AVOID_MULTIPLIER
                break
    return max(cost, 1e-3)


class DestinationTrees:
    """某 action_mode 的反向最短路樹群：建一次靜態權重 CSR，對多個終點重用。

    用法：``DestinationTrees(net, strategy, seed).paths_to(dest, origins)``。
    對每個終點在「轉置圖」上以終點為源跑一次 ``scipy.csgraph.dijkstra``（得到「各點→終點」的樹），
    每個起點沿前驅讀出路徑。無法到達回 ``[]``（呼叫端可退回逐車 find_path）。
    """

    def __init__(self, network: RoadNetwork, strategy: dict[str, Any], seed: int = 0,
                 avoid_circles=None) -> None:
        import numpy as np
        from scipy.sparse import csr_matrix

        self._idx = network._id_to_idx
        self._node_ids = network._node_ids
        s = {**strategy, "salt": ""}   # 樹不分車：固定 salt → 去掉 per-car jitter
        n = len(self._node_ids)
        rows: list[int] = []
        cols: list[int] = []
        data: list[float] = []
        for (u, v), road in network.roads.items():
            ui = self._idx.get(u)
            vi = self._idx.get(v)
            if ui is None or vi is None:
                continue
            rows.append(ui)
            cols.append(vi)
            data.append(_edge_cost(network, u, v, road, s, seed, avoid_circles))
        csr = csr_matrix(
            (np.asarray(data, dtype=float), (np.asarray(rows), np.asarray(cols))),
            shape=(n, n),
        )
        # 轉置：之後以「終點為源」跑 → 得到每個節點「到該終點」的最短路樹。
        self._csr_t = csr.T.tocsr()
        self._pred_cache: dict[int, Any] = {}

    def _predecessors(self, dest_idx: int):
        from scipy.sparse.csgraph import dijkstra

        pred = self._pred_cache.get(dest_idx)
        if pred is None:
            _, pred = dijkstra(self._csr_t, directed=True, indices=dest_idx,
                               return_predecessors=True)
            self._pred_cache[dest_idx] = pred
        return pred

    def paths_to(self, dest_node: str, origins) -> dict[str, list[str]]:
        """回 ``{origin: [origin..dest]}``；終點/起點不在圖內或無法到達 → 該 origin 為 ``[]``。"""
        d = self._idx.get(dest_node)
        if d is None:
            return {o: [] for o in set(origins)}
        pred = self._predecessors(d)
        out: dict[str, list[str]] = {}
        lim = len(self._node_ids) + 1
        for o in set(origins):
            oi = self._idx.get(o)
            if oi is None:
                out[o] = []
                continue
            if oi == d:
                out[o] = [o]
                continue
            path = [o]
            cur = oi
            ok = True
            guard = 0
            while cur != d:
                p = int(pred[cur])
                if p < 0:                     # 無法到達
                    ok = False
                    break
                cur = p
                path.append(self._node_ids[cur])
                guard += 1
                if guard > lim:               # 防護（理論上不會發生）
                    ok = False
                    break
            out[o] = path if ok else []
        return out
