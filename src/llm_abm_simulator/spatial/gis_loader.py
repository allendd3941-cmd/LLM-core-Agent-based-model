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
from .. import scenarios
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


def _load_population(path=None) -> dict[str, float]:
    """讀 population csv → {town_name: population}。檔案不存在/壞掉回 {}（重力模型會 fallback）。"""
    path = path or config.TOWN_POPULATION_CSV
    if not path.exists():
        logger.info("找不到人口檔 %s，重力模型將回退（面積代理或停用）。", path)
        return {}
    try:
        import csv
        pops: dict[str, float] = {}
        with path.open("r", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or row[0].lstrip().startswith("#") or row[0].strip() == "town_name":
                    continue
                if len(row) >= 2:
                    try:
                        pops[row[0].strip()] = float(row[1])
                    except ValueError:
                        continue
        logger.info("載入人口資料 %d 區", len(pops))
        return pops
    except OSError as e:
        logger.warning("讀取人口檔失敗：%s", e)
        return {}


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

    # 只保留目前場景指定的縣市（TOWN_MOI 為全台；county_filter 由 scenario 決定）
    scn = scenarios.active()
    if county_col is not None:
        gdf = gdf[gdf[county_col].astype(str).str.contains(scn.county_filter, na=False)].copy()
    logger.info("篩出行政區 %d 個（county_filter=%s）", len(gdf), scn.county_filter)

    populations = _load_population(scn.population_csv)
    towns: list[Town] = []
    for _, row in gdf.iterrows():
        geom: BaseGeometry = row.geometry
        name = str(row[town_col])
        towns.append(
            Town(
                town_name=name,
                county_name=str(row[county_col]) if county_col else cfg.county_name,
                town_id=str(row.get("TOWNID", "")),
                town_code=str(row.get("TOWNCODE", "")),
                town_eng=str(row.get("TOWNENG", "")),
                county_id=str(row.get("COUNTYID", "")),
                county_code=str(row.get("COUNTYCODE", "")),
                population=populations.get(name, 0.0),
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
        gdf = gdf[gdf[county_col].astype(str).str.contains(scenarios.active().county_filter, na=False)].copy()
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
    場景指定 dest_lat/lng 時用之（換事件地點）；否則用內建球場 shapefile（預設台南場景）。
    """
    scn = scenarios.active()
    if scn.dest_lat is not None and scn.dest_lng is not None:
        from pyproj import Transformer
        to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
        x, y = to_m.transform(scn.dest_lng, scn.dest_lat)
        logger.info("場景目的地：%s (%.5f, %.5f)", scn.name, scn.dest_lat, scn.dest_lng)
        return Point(x, y), (scn.dest_lat, scn.dest_lng)

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
