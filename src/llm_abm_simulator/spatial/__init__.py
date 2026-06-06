"""spatial — GIS 載入、路網建構、路徑規劃與 GeoJSON 輸出。

此層封裝所有 geopandas / networkx / shapely 的細節，對外提供乾淨的查詢介面：
- ``gis_loader``   載入 TOWN_MOI（篩臺南市）、球場 point、研究範圍，含 CRS 轉換。
- ``road_network`` 建立路網（OSM bundle / synthetic fallback）與空間查詢。
- ``routing``      加權最短路徑與動態權重。
- ``geojson``      towns / roads / agents → GeoJSON（WGS84）供前端使用。
"""

from __future__ import annotations
