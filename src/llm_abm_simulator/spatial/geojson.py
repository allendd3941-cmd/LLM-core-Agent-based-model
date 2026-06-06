"""geojson.py — 將路網/道路轉成前端可用的 GeoJSON（WGS84）。

行政區邊界由 ``gis_loader.load_towns_geojson`` 直接產生；
此檔負責道路圖層（靜態幾何，前端初始化載入一次）。
"""

from __future__ import annotations

from typing import Any

from .road_network import RoadNetwork


# 前端底圖預設只畫主要道路，避免一次送出數萬條 polyline 拖垮瀏覽器；
# 完整路網仍用於路徑規劃。傳 only_major=False 可輸出全部道路。
_MAJOR_HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary",
                   "motorway_link", "trunk_link", "primary_link", "secondary_link"}


def roads_to_geojson(network: RoadNetwork, only_major: bool = True) -> dict[str, Any]:
    """道路 → GeoJSON FeatureCollection（LineString，WGS84）。

    幾何取自 Road.geometry_wgs84（OSM 曲線或 synthetic 直線）；
    properties 帶 road_id 供前端依 congestion 即時上色時對應。

    Args:
        only_major: True 時只輸出主要道路（demo 效能預設）；False 輸出全部。
    """
    features: list[dict[str, Any]] = []
    for road in network.all_roads():
        geom = road.geometry_wgs84
        if geom is None:
            continue
        if only_major and road.highway.split(",")[0].strip().strip("[]'\" ") not in _MAJOR_HIGHWAYS:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom.__geo_interface__,
            "properties": {
                "road_id": road.road_id,
                "highway": road.highway,
                "capacity": road.capacity,
            },
        })
    return {"type": "FeatureCollection", "features": features}
