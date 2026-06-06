"""gis_loader.py — 載入 GIS 來源資料（對齊 GAML init 的 shapefile 載入）。

職責：
- 載入 TOWN_MOI 行政區界並只保留臺南市（對齊 GAML ``ask town where county_name != 臺南市 die``）。
- 載入亞太棒球場固定終點 point（對齊 GAML destination_point）。
- 載入研究範圍 polygon（供 OSM 路網下載界定範圍）。
- 統一處理 CRS：對外同時提供公尺座標（EPSG:3826，距離運算）與 WGS84（前端地圖）。

所有 shapefile 以 UTF-8 讀取。
"""

from __future__ import annotations

import logging

import geopandas as gpd
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from .. import config
from ..domain.town import Town

logger = logging.getLogger(__name__)

# TOWN_MOI 可能的欄位名（不同來源版本大小寫不一）
_TOWNNAME_COLS = ("TOWNNAME", "TownName", "townname", "TOWN_NAME")
_COUNTYNAME_COLS = ("COUNTYNAME", "CountyName", "countyname", "COUNTY_NAM")


def _first_col(gdf: gpd.GeoDataFrame, candidates: tuple[str, ...]) -> str | None:
    for col in candidates:
        if col in gdf.columns:
            return col
    return None


def load_towns(cfg: config.SimulationConfig | None = None) -> list[Town]:
    """載入並篩選臺南市行政區，回傳 Town 物件清單（含公尺幾何與形心）。"""
    cfg = cfg or config.DEFAULT_CONFIG
    logger.info("載入行政區界: %s", config.TOWN_SHP)
    gdf = gpd.read_file(str(config.TOWN_SHP), encoding="utf-8")

    # 確保是公尺投影座標（EPSG:3826）；TOWN_MOI 本身即為此座標。
    if gdf.crs is None:
        gdf = gdf.set_crs(config.CRS_METRIC, allow_override=True)
    elif gdf.crs.to_epsg() != 3826:
        gdf = gdf.to_crs(config.CRS_METRIC)

    county_col = _first_col(gdf, _COUNTYNAME_COLS)
    town_col = _first_col(gdf, _TOWNNAME_COLS)
    if town_col is None:
        raise ValueError(f"TOWN_MOI 找不到行政區名稱欄位，現有欄位: {list(gdf.columns)}")

    # 只保留臺南市（對齊 GAML county_name == 臺南市）
    if county_col is not None:
        gdf = gdf[gdf[county_col].astype(str).str.contains("臺南|台南", na=False)].copy()
    logger.info("篩出臺南市行政區 %d 個", len(gdf))

    towns: list[Town] = []
    for _, row in gdf.iterrows():
        geom: BaseGeometry = row.geometry
        towns.append(
            Town(
                town_name=str(row[town_col]),
                county_name=str(row[county_col]) if county_col else cfg.county_name,
                town_id=str(row.get("TOWNID", "")),
                town_code=str(row.get("TOWNCODE", "")),
                town_eng=str(row.get("TOWNENG", "")),
                county_id=str(row.get("COUNTYID", "")),
                county_code=str(row.get("COUNTYCODE", "")),
                geometry_metric=geom,
                centroid_metric=geom.centroid,
            )
        )
    return towns


def load_towns_geojson() -> dict:
    """載入臺南市行政區並輸出 WGS84 GeoJSON（前端邊界圖層）。"""
    gdf = gpd.read_file(str(config.TOWN_SHP), encoding="utf-8")
    if gdf.crs is None:
        gdf = gdf.set_crs(config.CRS_METRIC, allow_override=True)
    county_col = _first_col(gdf, _COUNTYNAME_COLS)
    if county_col is not None:
        gdf = gdf[gdf[county_col].astype(str).str.contains("臺南|台南", na=False)].copy()
    town_col = _first_col(gdf, _TOWNNAME_COLS)
    # 只保留名稱欄位，避免 GeoJSON 帶出大量無用屬性
    keep = [c for c in (town_col, county_col) if c]
    gdf = gdf[keep + ["geometry"]].rename(columns={town_col: "town_name"})
    gdf = gdf.to_crs(config.CRS_WGS84)
    import json
    return json.loads(gdf.to_json())


def load_stadium_point() -> tuple[Point, tuple[float, float]]:
    """載入亞太棒球場 point。

    回傳 (公尺座標 Point[EPSG:3826], (lat, lng)[WGS84])，
    對齊 GAML fixed_destination_location。
    """
    logger.info("載入球場固定終點: %s", config.STADIUM_SHP)
    gdf = gpd.read_file(str(config.STADIUM_SHP), encoding="utf-8")
    if gdf.crs is None:
        gdf = gdf.set_crs(config.CRS_METRIC, allow_override=True)

    metric = gdf.to_crs(config.CRS_METRIC).geometry.iloc[0]
    wgs = gdf.to_crs(config.CRS_WGS84).geometry.iloc[0]
    return metric, (wgs.y, wgs.x)


def load_study_area_wgs84() -> BaseGeometry:
    """載入研究範圍 polygon（WGS84），供 OSM 路網下載界定範圍。"""
    logger.info("載入研究範圍: %s", config.STUDY_AREA_SHP)
    gdf = gpd.read_file(str(config.STUDY_AREA_SHP), encoding="utf-8")
    if gdf.crs is None:
        gdf = gdf.set_crs(config.CRS_WGS84, allow_override=True)
    gdf = gdf.to_crs(config.CRS_WGS84)
    return gdf.geometry.union_all()
