"""town.py — 行政區（TOWN_MOI polygon）資料模型。

欄位對齊 GAML ``species town``（TOWN_MOI 的 DBF 欄位）。
只保留臺南市的行政區由 spatial.gis_loader 負責篩選。
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry


@dataclass
class Town:
    """單一鄉鎮市區。"""

    town_name: str                      # 對齊 TOWNNAME，例如「東區」
    county_name: str = ""               # 對齊 COUNTYNAME，例如「臺南市」
    town_id: str = ""
    town_code: str = ""
    town_eng: str = ""
    county_id: str = ""
    county_code: str = ""
    population: float = 0.0             # 人口（供重力模型需求生成加權；0＝無資料）

    # 公尺投影座標（EPSG:3826）下的幾何與形心；距離/點落區判斷皆用此座標系。
    geometry_metric: BaseGeometry | None = None
    centroid_metric: Point | None = None

    def contains_point(self, point: Point) -> bool:
        """點是否落在此行政區內（公尺座標）。對齊 GAML ``town overlapping self``。"""
        if self.geometry_metric is None:
            return False
        return self.geometry_metric.covers(point)
