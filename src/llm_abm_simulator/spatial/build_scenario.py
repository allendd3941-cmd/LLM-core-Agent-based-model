"""build_scenario.py — 由「縣市 + 事件目的地」產生一個新場景 bundle（可抽換圖層的『輸入新圖層』路徑）。

用法（需要 OSMnx + 網路；大城市路網可能很大，注意算力）：

    python -m llm_abm_simulator.spatial.build_scenario \
        --key kaohsiung_arena --name "高雄巨蛋（示範）" \
        --county 高雄 --dest-lat 22.6699 --dest-lng 120.3025 --dest-town 左營區

流程：
1. 讀全台 TOWN_MOI、依 --county 篩出該縣市 → union 成多邊形（WGS84）。
2. OSMnx 依該多邊形下載 drivable 路網 → 取最大強連通分量 → 存 data/scenarios/<key>_roads.graphml。
3. 寫 data/scenarios/<key>.json（manifest，啟動時 scenarios._load_manifests 會自動註冊）。

⚠ 人口：manifest 預設指向現有 town_population.csv（只有台南）。換縣市請另外準備該縣市的
   `town_name,population` CSV，用 --population 指定，否則重力需求生成會 fallback。
"""

from __future__ import annotations

import argparse
import json
import logging

import geopandas as gpd

from .. import config
from ..scenarios import SCENARIOS_DIR
from . import road_network as rn
from .gis_loader import _COUNTYNAME_COLS, _first_col

logger = logging.getLogger(__name__)


def _county_polygon(county: str):
    gdf = gpd.read_file(str(config.TOWN_SHP), encoding="utf-8")
    if gdf.crs is None:
        gdf = gdf.set_crs(config.CRS_METRIC, allow_override=True)
    col = _first_col(gdf, _COUNTYNAME_COLS)
    if col is None:
        raise ValueError("TOWN_MOI 找不到縣市欄位")
    sub = gdf[gdf[col].astype(str).str.contains(county, na=False)].copy()
    if sub.empty:
        raise ValueError(f"找不到縣市：{county}")
    return sub.to_crs(config.CRS_WGS84).geometry.union_all()


def main() -> None:
    p = argparse.ArgumentParser(description="產生新場景 bundle（縣市 + 目的地 → 路網 + manifest）")
    p.add_argument("--key", required=True, help="場景 key（英數，檔名用）")
    p.add_argument("--name", required=True, help="場景顯示名稱")
    p.add_argument("--county", required=True, help="縣市關鍵字（如 高雄 / 臺北）")
    p.add_argument("--dest-lat", type=float, required=True)
    p.add_argument("--dest-lng", type=float, required=True)
    p.add_argument("--dest-town", required=True, help="目的地所在區（標籤用）")
    p.add_argument("--population", default="", help="該縣市人口 CSV 路徑（town_name,population）")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import osmnx as ox
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    graphml_path = SCENARIOS_DIR / f"{args.key}_roads.graphml"

    logger.info("下載 %s 的 drivable 路網（OSMnx）…", args.county)
    poly = _county_polygon(args.county)
    g_osm = ox.graph_from_polygon(poly, network_type="drive")
    g = rn._largest_scc(rn._convert_osm_graph(g_osm))
    rn.save_graphml(g, graphml_path)

    manifest = {
        "key": args.key, "name": args.name, "county_filter": args.county,
        "road_graphml": str(graphml_path),
        "population_csv": args.population or str(config.TOWN_POPULATION_CSV),
        "dest_lat": args.dest_lat, "dest_lng": args.dest_lng, "dest_town": args.dest_town,
        "center_lat": args.dest_lat, "center_lng": args.dest_lng, "zoom": 12,
    }
    (SCENARIOS_DIR / f"{args.key}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已建立場景 {args.key}：{g.number_of_nodes()} 節點 / {g.number_of_edges()} 邊")
    print(f"manifest → {SCENARIOS_DIR / (args.key + '.json')}（重啟伺服器即出現在場景下拉）")
    if not args.population:
        print("⚠ 未指定 --population，重力需求生成將 fallback（請準備該縣市人口 CSV）")


if __name__ == "__main__":
    main()
