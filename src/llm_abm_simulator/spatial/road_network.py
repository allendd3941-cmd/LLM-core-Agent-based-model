"""road_network.py — 路網建構與空間查詢。

本專案沒有 ROADLINK 道路 shapefile（GAML 引用的 ``../data/ROADLINK.shp`` 不存在），
因此採三層 fallback 取得路網（對齊計畫決策：真實 OSM 為主、synthetic 為底）：

1. 讀取 bundle 的 ``data/tainan_roads.graphml``（真實 OSM 道路，commit 進 repo，離線可重現）。
2. 若不存在且允許下載 → 用 OSMnx 即時下載研究範圍內 drivable 道路並存成 graphml。
3. 再不行 → 產生確定性 synthetic 網格路網（依研究範圍 bounds）。

對齊 GAML 能力：
- ``as_edge_graph``      → networkx 有向圖（節點含公尺座標 + WGS84）。
- nearest-road / nearest-node、road-in-town、current town/road、nearby agents → 本檔查詢方法。
- road 動態 flow / congestion / weight → ``Road`` 物件 + ``update_flow``。

graphml 內只存純量屬性（節點 lat/lng/x_m/y_m；邊 length/highway/speed_*/lanes/capacity/wkt），
邊幾何以 WKT 字串保存，載入時還原成 shapely LineString 供前端 GeoJSON 使用。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import networkx as nx
import numpy as np
from shapely import wkt as shapely_wkt
from shapely.geometry import LineString, Point

from .. import config
from ..domain.road import Road
from ..domain.town import Town

logger = logging.getLogger(__name__)

# 依 OSM highway type 推估速度（km/h）/ 車道 / 容量代理值。
# 來源為 config/simulation.toml 的 [highway_specs]（缺檔/缺值時回退到 config 內建預設）。
# capacity 為 congestion_proxy = flow/capacity 的分母，刻意取較小值讓壅塞在 demo 中可見。
def _spec_for(highway: str) -> dict[str, float]:
    # OSM highway 可能是 list 的字串表示，取第一個關鍵字
    key = highway.split(",")[0].strip().strip("[]'\" ") if highway else ""
    return config.HIGHWAY_SPECS.get(key, config.DEFAULT_HIGHWAY_SPEC)


# ---------------------------------------------------------------------------
# RoadNetwork：對外的路網查詢介面
# ---------------------------------------------------------------------------
class RoadNetwork:
    """封裝有向路網圖與 Road 物件，提供模擬所需的空間查詢。"""

    def __init__(self, graph: nx.DiGraph, roads: dict[tuple[str, str], Road]) -> None:
        self.graph = graph
        # roads: (u, v) → Road；與 graph edge 一一對應
        self.roads = roads

        # 節點公尺座標快取（numpy 向量化最近節點查詢）
        self._node_ids: list[str] = list(graph.nodes())
        self._xy = np.array(
            [[graph.nodes[n]["x_m"], graph.nodes[n]["y_m"]] for n in self._node_ids],
            dtype=float,
        )
        self._id_to_idx = {n: i for i, n in enumerate(self._node_ids)}
        logger.info("路網就緒：%d 節點 / %d 邊", graph.number_of_nodes(), graph.number_of_edges())

    # ---- 節點 / 座標 ----
    def node_xy(self, node: str) -> tuple[float, float]:
        """節點公尺座標 (x, y)（EPSG:3826）。"""
        d = self.graph.nodes[node]
        return d["x_m"], d["y_m"]

    def node_latlng(self, node: str) -> tuple[float, float]:
        d = self.graph.nodes[node]
        return d["lat"], d["lng"]

    def nearest_node(self, x_m: float, y_m: float) -> str:
        """最近節點（公尺座標）。對齊 GAML ``closest_to`` 的 nearest 行為。"""
        d2 = (self._xy[:, 0] - x_m) ** 2 + (self._xy[:, 1] - y_m) ** 2
        return self._node_ids[int(np.argmin(d2))]

    # ---- road 查詢 ----
    def road_between(self, u: str, v: str) -> Road | None:
        return self.roads.get((u, v))

    def all_roads(self) -> list[Road]:
        return list(self.roads.values())

    # ---- town ↔ road ----
    def random_node_in_town(self, town: Town, rng) -> str:
        """在指定行政區內隨機挑一個路網節點（對齊 GAML choose_road_point_in_town）。

        先找落在 polygon 內的節點；若無，退而取離形心最近的節點（nearest-road fallback）。
        """
        if town.geometry_metric is not None:
            geom = town.geometry_metric
            inside = [n for n in self._node_ids
                      if geom.covers(Point(self.graph.nodes[n]["x_m"], self.graph.nodes[n]["y_m"]))]
            if inside:
                return inside[rng.randrange(len(inside))]
        # fallback：離形心最近的節點
        if town.centroid_metric is not None:
            return self.nearest_node(town.centroid_metric.x, town.centroid_metric.y)
        return self._node_ids[rng.randrange(len(self._node_ids))]

    def reset_flows(self) -> None:
        """重置所有道路 flow（對齊 GAML reset 每步重算 flow 前）。"""
        for road in self.roads.values():
            road.current_flow = 0
            road.congestion_proxy = 0.0
            road.weight = max(road.length, 1.0)


# ---------------------------------------------------------------------------
# 載入 / 建立路網
# ---------------------------------------------------------------------------
def load_road_network(cfg: config.SimulationConfig | None = None) -> RoadNetwork:
    """依三層 fallback 取得路網。"""
    cfg = cfg or config.DEFAULT_CONFIG

    if config.ROAD_GRAPHML.exists():
        logger.info("讀取 bundle 路網: %s", config.ROAD_GRAPHML)
        try:
            return _from_graphml(config.ROAD_GRAPHML)
        except Exception as e:  # noqa: BLE001
            logger.warning("bundle 路網讀取失敗（%s），改用其他來源", e)

    if cfg.allow_osm_download:
        try:
            graph = build_osm_graph()
            save_graphml(graph, config.ROAD_GRAPHML)
            return _wrap(graph)
        except Exception as e:  # noqa: BLE001
            logger.warning("OSM 下載失敗（%s），改用 synthetic 路網", e)

    logger.info("使用 synthetic 路網（fallback）")
    return _wrap(build_synthetic_graph(cfg))


# ---- OSM 建構 ----
def build_osm_graph() -> nx.DiGraph:
    """用 OSMnx 下載研究範圍內 drivable 道路，轉成本專案的有向圖。"""
    import osmnx as ox  # 延遲匯入：runtime 讀 bundle 時不需要 osmnx

    from . import gis_loader

    polygon = gis_loader.load_study_area_wgs84()
    logger.info("OSMnx 下載研究範圍 drivable 道路…")
    g_osm = ox.graph_from_polygon(polygon, network_type="drive")
    g = _convert_osm_graph(g_osm)
    return _largest_scc(g)


def _largest_scc(g: nx.DiGraph) -> nx.DiGraph:
    """只保留最大強連通分量，確保任一節點都能互相到達（避免 no-path）。"""
    if g.number_of_nodes() == 0:
        return g
    largest = max(nx.strongly_connected_components(g), key=len)
    sub = g.subgraph(largest).copy()
    logger.info("保留最大強連通分量：%d/%d 節點", sub.number_of_nodes(), g.number_of_nodes())
    return sub


def _convert_osm_graph(g_osm: nx.MultiDiGraph) -> nx.DiGraph:
    """osmnx MultiDiGraph → 本專案 DiGraph（節點含公尺+WGS84 座標、邊含 Road 屬性）。"""
    from pyproj import Transformer

    to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
    g = nx.DiGraph()

    for n, data in g_osm.nodes(data=True):
        lng, lat = float(data["x"]), float(data["y"])
        x_m, y_m = to_m.transform(lng, lat)
        g.add_node(str(n), lat=lat, lng=lng, x_m=x_m, y_m=y_m)

    for u, v, data in g_osm.edges(data=True):
        su, sv = str(u), str(v)
        if g.has_edge(su, sv):
            continue
        highway = data.get("highway", "")
        if isinstance(highway, list):
            highway = highway[0] if highway else ""
        spec = _spec_for(str(highway))
        length = float(data.get("length", 0.0)) or _node_dist(g, su, sv)
        # 邊幾何：OSM 提供則用之（曲線），否則用兩端點直線
        geom = data.get("geometry")
        if geom is None:
            geom = LineString([(g.nodes[su]["lng"], g.nodes[su]["lat"]),
                               (g.nodes[sv]["lng"], g.nodes[sv]["lat"])])
        name = data.get("name", "")
        if isinstance(name, list):
            name = name[0] if name else ""
        g.add_edge(
            su, sv,
            road_id=f"{su}_{sv}",
            length=length,
            highway=str(highway),
            road_name=str(name),
            speed_car=spec["speed_car"],
            speed_moto=spec["speed_moto"],
            lanes=spec["lanes"],
            capacity=spec["capacity"],
            wkt=geom.wkt,
        )
    logger.info("OSM 路網轉換完成：%d 節點 / %d 邊", g.number_of_nodes(), g.number_of_edges())
    return g


def _node_dist(g: nx.DiGraph, u: str, v: str) -> float:
    ux, uy = g.nodes[u]["x_m"], g.nodes[u]["y_m"]
    vx, vy = g.nodes[v]["x_m"], g.nodes[v]["y_m"]
    return math.hypot(ux - vx, uy - vy)


# ---- synthetic 建構 ----
def build_synthetic_graph(cfg: config.SimulationConfig) -> nx.DiGraph:
    """確定性 synthetic 網格路網（fallback）。

    在研究範圍 bounds 內鋪一張規則網格，雙向連邊。座標同時記錄公尺與 WGS84。
    完全確定性、無隨機，便於離線測試。
    """
    from pyproj import Transformer
    from . import gis_loader

    try:
        bounds = gis_loader.load_study_area_wgs84().bounds  # (minx,miny,maxx,maxy) in lng/lat
    except Exception:  # noqa: BLE001
        bounds = (120.10, 22.95, 120.30, 23.15)

    to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
    minx, miny, maxx, maxy = bounds
    n = max(3, cfg.synthetic_grid_size)
    lngs = np.linspace(minx, maxx, n)
    lats = np.linspace(miny, maxy, n)

    g = nx.DiGraph()
    ids = [[f"g_{i}_{j}" for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(n):
            lng, lat = float(lngs[j]), float(lats[i])
            x_m, y_m = to_m.transform(lng, lat)
            g.add_node(ids[i][j], lat=lat, lng=lng, x_m=x_m, y_m=y_m)

    spec = config.HIGHWAY_SPECS.get("secondary", config.DEFAULT_HIGHWAY_SPEC)

    def _link(a: str, b: str) -> None:
        length = _node_dist(g, a, b)
        geom = LineString([(g.nodes[a]["lng"], g.nodes[a]["lat"]),
                           (g.nodes[b]["lng"], g.nodes[b]["lat"])])
        for u, v in ((a, b), (b, a)):
            g.add_edge(u, v, road_id=f"{u}_{v}", length=length, highway="secondary",
                       road_name="synthetic", speed_car=spec["speed_car"],
                       speed_moto=spec["speed_moto"], lanes=spec["lanes"],
                       capacity=spec["capacity"], wkt=geom.wkt)

    for i in range(n):
        for j in range(n):
            if j + 1 < n:
                _link(ids[i][j], ids[i][j + 1])
            if i + 1 < n:
                _link(ids[i][j], ids[i + 1][j])
    logger.info("synthetic 路網完成：%d 節點 / %d 邊", g.number_of_nodes(), g.number_of_edges())
    return g


# ---- graphml 存取 ----
def save_graphml(graph: nx.DiGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, str(path))
    logger.info("路網已存成 graphml: %s", path)


def _from_graphml(path: Path) -> RoadNetwork:
    graph = nx.read_graphml(str(path))
    # graphml 讀回來節點屬性是字串，轉回數值
    for _, d in graph.nodes(data=True):
        for k in ("lat", "lng", "x_m", "y_m"):
            d[k] = float(d[k])
    return _wrap(graph)


def _wrap(graph: nx.DiGraph) -> RoadNetwork:
    """從 networkx 圖建立 Road 物件並包成 RoadNetwork。"""
    roads: dict[tuple[str, str], Road] = {}
    for u, v, data in graph.edges(data=True):
        geom = None
        w = data.get("wkt")
        if w:
            try:
                geom = shapely_wkt.loads(w)
            except Exception:  # noqa: BLE001
                geom = None
        roads[(u, v)] = Road(
            road_id=str(data.get("road_id", f"{u}_{v}")),
            node_a=u,
            node_b=v,
            length=float(data.get("length", 0.0)),
            highway=str(data.get("highway", "")),
            highway_type=str(data.get("highway", "")),
            road_name=str(data.get("road_name", "")),
            speed_car=float(data.get("speed_car", 45.0)),
            speed_moto=float(data.get("speed_moto", 35.0)),
            lanes=float(data.get("lanes", 1.0)),
            capacity=float(data.get("capacity", 30.0)),
            geometry_wgs84=geom,
        )
    return RoadNetwork(graph, roads)
