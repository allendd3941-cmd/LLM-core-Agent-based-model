"""gis_export.py — 把模擬結果輸出成交通局可用的主題圖層 Shapefile（zip）。

每個圖層 = 路段(線) 或 監測器(點) + 屬性；欄名 ≤10 字元對齊 ESRI Shapefile DBF 限制。
CRS = EPSG:4326（WGS84），附 .prj/.cpg，QGIS/ArcGIS 可直接疊底圖、用屬性分類上色：
  - los：peak_los（A–F）、peak_vc、peak_prox
  - flow：tot_vol / car_vol / moto_vol / evt_vol / amb_vol（整趟累積通過量）
  - congestion：peak_prox（0–1）、peak_flow、peak_los
  - detectors（點）：放置的監測器 + 各類通過量

資料由 engine.gis_road_records() / gis_detector_records() 提供（被動量測，不改物理）。
"""

from __future__ import annotations

import logging
import uuid
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

logger = logging.getLogger(__name__)

# 各道路主題圖層的欄位（皆 ≤10 字元；road = 線圖層）
_ROAD_LAYERS: dict[str, dict[str, Any]] = {
    "los": {"label": "道路服務水準",
            "fields": ["road_id", "name", "highway", "lanes", "peak_prox", "peak_vc", "peak_los"]},
    "flow": {"label": "車流量",
             "fields": ["road_id", "name", "highway", "tot_vol", "car_vol", "moto_vol", "evt_vol", "amb_vol"]},
    "congestion": {"label": "壅塞程度",
                   "fields": ["road_id", "name", "highway", "peak_prox", "peak_flow", "capacity", "peak_los"]},
}
_DETECTOR_LAYER: dict[str, Any] = {
    "label": "車流監測器",
    "fields": ["det_id", "name", "tot_vol", "car_vol", "moto_vol", "evt_vol", "amb_vol",
               "dir_a_vol", "dir_b_vol"],
}

# 前端下拉可選的圖層鍵 + 中文標籤（"all" = 全部打包）
LAYER_LABELS: dict[str, str] = {k: v["label"] for k, v in _ROAD_LAYERS.items()}
LAYER_LABELS["detectors"] = _DETECTOR_LAYER["label"]
LAYER_KEYS: list[str] = list(_ROAD_LAYERS.keys()) + ["detectors"]


def _gdf_for(layer: str, engine):
    """建單一圖層的 GeoDataFrame（EPSG:4326）。"""
    import geopandas as gpd
    if layer in _ROAD_LAYERS:
        recs = engine.gis_road_records()
        cols = _ROAD_LAYERS[layer]["fields"]
    elif layer == "detectors":
        recs = engine.gis_detector_records()
        cols = _DETECTOR_LAYER["fields"]
    else:
        raise ValueError(f"未知圖層：{layer}")
    if not recs:
        return gpd.GeoDataFrame({c: [] for c in cols}, geometry=[], crs="EPSG:4326")
    geoms = [r["geometry"] for r in recs]
    data = {c: [r.get(c) for r in recs] for c in cols}
    return gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")


def _zip_shapefiles(src_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.glob("*.*")):
            zf.write(f, f.name)


def export_layer_zip(engine, layer: str, out_dir) -> Path:
    """建主題圖層的 Shapefile 並打包成 zip，回傳 zip 路徑。

    layer = 'all' 時把所有圖層（los/flow/congestion/detectors）寫進同一個 zip。
    檔名帶亂數 token 避免多連線/多次匯出互相覆蓋。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]

    if layer == "all":
        zip_path = out_dir / f"gis_layers_all_{token}.zip"
        with TemporaryDirectory() as td:
            tdp = Path(td)
            wrote = False
            for key in LAYER_KEYS:
                gdf = _gdf_for(key, engine)
                if len(gdf) == 0:
                    continue
                gdf.to_file(str(tdp / f"{key}.shp"), driver="ESRI Shapefile", encoding="UTF-8")
                wrote = True
            if not wrote:
                raise ValueError("沒有可匯出的圖層資料")
            _zip_shapefiles(tdp, zip_path)
        return zip_path

    if layer not in LAYER_KEYS:
        raise ValueError(f"未知圖層：{layer}")
    gdf = _gdf_for(layer, engine)
    if len(gdf) == 0:
        raise ValueError(f"圖層「{LAYER_LABELS.get(layer, layer)}」沒有資料可匯出")
    zip_path = out_dir / f"gis_{layer}_{token}.zip"
    with TemporaryDirectory() as td:
        tdp = Path(td)
        gdf.to_file(str(tdp / f"{layer}.shp"), driver="ESRI Shapefile", encoding="UTF-8")
        _zip_shapefiles(tdp, zip_path)
    return zip_path
