"""build_signals.py — 由號誌點位 shapefile 建立「路網節點 → 號誌相位設定」的精簡 artifact。

用途（可重現，比照 build_roads.py）：

    python -m llm_abm_simulator.spatial.build_signals

流程：
1. 讀 bundle 路網 ``data/tainan_roads.graphml`` 取得節點（= 路口）公尺座標。
2. 讀號誌點位 shapefile（``data/traffic_light/shapefile/traffic_light_points.shp``），轉成
   EPSG:3826，**snap 到最近的路網節點**：節點 ``snap_threshold_m`` 內有任一號誌點 → 該節點為號誌路口。
   這同時是「最專業的去重」——同一路口的多個號誌頭自然收斂到同一個網路節點。
3. 對每個號誌路口，用「進場邊的方位角」分兩個相位組（一條路軸 vs 垂直路軸），
   一組綠時另一組紅（方向相位）。並指派一個**確定性 offset**（由節點 id 雜湊）讓號誌不同步。
4. 輸出精簡 ``data/tainan_signals.json``（僅數千節點，commit 進 repo，runtime 不碰 148MB dbf）。

⚠ 台南**無真實時相秒數**（phase_records 只有臺北/澎湖且 ID 對不上），故 cycle/yellow 為
   **可設定的合成值**，於 config ``[signals]`` 調整；本檔只烤入「哪些節點是號誌 + 相位軸 + offset」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math

import networkx as nx
import numpy as np

from .. import config

logger = logging.getLogger(__name__)

# 號誌點位 shapefile（EPSG:4326）
SIGNAL_SHP = config.DATA_DIR / "traffic_light" / "shapefile" / "traffic_light_points.shp"
SIGNALS_JSON = config.DATA_DIR / "tainan_signals.json"


def _bearing_deg(x0: float, y0: float, x1: float, y1: float) -> float:
    """從 (x0,y0) 指向 (x1,y1) 的方位角（度，0..360；公尺座標下用 atan2）。"""
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0


def _node_offset_s(node_id: str, cycle_s: int) -> int:
    """由節點 id 確定性雜湊出一個 [0, cycle_s) 的 offset 秒數（讓相鄰號誌不同步、可重現）。"""
    h = int(hashlib.md5(node_id.encode("utf-8")).hexdigest(), 16)
    return h % max(1, cycle_s)


def build_signals(snap_threshold_m: float = 40.0, base_cycle_s: int = 90) -> dict:
    """建立號誌 artifact dict。"""
    import geopandas as gpd
    from scipy.spatial import cKDTree

    # ---- 路網節點 ----
    graph = nx.read_graphml(str(config.ROAD_GRAPHML))
    node_ids = list(graph.nodes())
    xy = np.array([[float(graph.nodes[n]["x_m"]), float(graph.nodes[n]["y_m"])] for n in node_ids])
    latlng = {n: (float(graph.nodes[n]["lat"]), float(graph.nodes[n]["lng"])) for n in node_ids}
    logger.info("路網節點 %d", len(node_ids))

    # ---- 號誌點位（只取落在路網 bbox + 緩衝內的，省記憶體）----
    logger.info("讀號誌點位：%s", SIGNAL_SHP)
    pts = gpd.read_file(str(SIGNAL_SHP))
    pts = pts.to_crs(config.CRS_METRIC)
    minx, miny = xy.min(axis=0) - snap_threshold_m
    maxx, maxy = xy.max(axis=0) + snap_threshold_m
    px = pts.geometry.x.to_numpy()
    py = pts.geometry.y.to_numpy()
    in_bbox = (px >= minx) & (px <= maxx) & (py >= miny) & (py <= maxy)
    sig_xy = np.column_stack([px[in_bbox], py[in_bbox]])
    logger.info("bbox 內號誌點 %d / %d", len(sig_xy), len(pts))

    # ---- snap：每個路網節點找最近號誌點，<= 門檻 → 號誌節點 ----
    if len(sig_xy) == 0:
        raise RuntimeError("bbox 內沒有號誌點，請確認 shapefile 與路網範圍一致")
    tree = cKDTree(sig_xy)
    dist, _ = tree.query(xy, k=1)
    signalized_idx = [i for i, d in enumerate(dist) if d <= snap_threshold_m]
    logger.info("號誌路口節點 %d（門檻 %.0fm）", len(signalized_idx), snap_threshold_m)

    # ---- 每個號誌節點：相位軸（ref_axis）+ offset ----
    signals: dict[str, dict] = {}
    skipped_minor = 0
    for i in signalized_idx:
        node = node_ids[i]
        nx_m, ny_m = xy[i]
        # 進場邊方位角（mod 180 → 對向同路算同軸）
        axes: list[float] = []
        for u in graph.predecessors(node):
            ux, uy = float(graph.nodes[u]["x_m"]), float(graph.nodes[u]["y_m"])
            axes.append(_bearing_deg(ux, uy, nx_m, ny_m) % 180.0)
        # 也納入出場方向，確保 degree 計算穩健
        if not axes:
            for w in graph.successors(node):
                wx, wy = float(graph.nodes[w]["x_m"]), float(graph.nodes[w]["y_m"])
                axes.append(_bearing_deg(nx_m, ny_m, wx, wy) % 180.0)
        if not axes:
            skipped_minor += 1
            continue
        ref_axis = float(axes[0])
        # 是否為真正的兩相位路口：存在與 ref 夾角 > 30° 的approach（不同路軸）
        two_phase = any(_circ_dist_180(a, ref_axis) > 30.0 for a in axes)
        lat, lng = latlng[node]
        signals[node] = {
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "ax": round(ref_axis, 1),       # 相位組 0 的路軸方位（度，0..180）；組 1 為其垂直方向
            "off": _node_offset_s(node, base_cycle_s),  # offset 秒
            "two": bool(two_phase),         # 兩相位路口（False＝單向/匝道型，車輛恆綠、僅示意）
        }

    artifact = {
        "meta": {
            "snap_threshold_m": snap_threshold_m,
            "base_cycle_s": base_cycle_s,
            "count": len(signals),
            "two_phase_count": sum(1 for s in signals.values() if s["two"]),
            "synthetic_timing": True,
            "note": "台南無真實號誌時相；cycle/yellow 為 config[signals] 合成值，僅烤入節點/相位軸/offset",
        },
        "signals": signals,
    }
    logger.info("號誌 artifact：%d 節點（兩相位 %d，跳過無邊 %d）",
                len(signals), artifact["meta"]["two_phase_count"], skipped_minor)
    return artifact


def _circ_dist_180(a: float, b: float) -> float:
    """mod 180 的環狀角度差（0..90）。"""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def main() -> None:
    parser = argparse.ArgumentParser(description="建立號誌相位 artifact（tainan_signals.json）")
    parser.add_argument("--threshold", type=float, default=40.0, help="號誌 snap 到節點的距離門檻（公尺）")
    parser.add_argument("--cycle", type=int, default=90, help="offset 雜湊基準週期秒（不影響 runtime 週期）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    artifact = build_signals(snap_threshold_m=args.threshold, base_cycle_s=args.cycle)
    SIGNALS_JSON.parent.mkdir(parents=True, exist_ok=True)
    SIGNALS_JSON.write_text(json.dumps(artifact, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已寫出 {SIGNALS_JSON}（{artifact['meta']['count']} 號誌節點，"
          f"兩相位 {artifact['meta']['two_phase_count']}）")


if __name__ == "__main__":
    main()
