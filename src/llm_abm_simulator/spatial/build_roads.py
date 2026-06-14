"""build_roads.py — 重新下載並 bundle 真實 OSM 路網的 CLI。

用途（可重現）：

    python -m llm_abm_simulator.spatial.build_roads

會用 OSMnx 依**縣界（預設全台南市 37 區）**下載 drivable 道路、取最大強連通分量，存成
``data/tainan_roads.graphml``。此檔已 gitignore（大檔不進 repo）：首次啟動會自動建，
要強制重建就刪檔再啟動、或直接跑本 CLI。

若無法上網或不想用 OSM，可加 ``--synthetic`` 改產生確定性合成路網。
"""

from __future__ import annotations

import argparse
import logging

from .. import config
from . import road_network as rn


def main() -> None:
    parser = argparse.ArgumentParser(description="建立並 bundle 臺南交通模擬路網")
    parser.add_argument("--synthetic", action="store_true", help="改用確定性合成路網（不連網）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.synthetic:
        graph = rn.build_synthetic_graph(config.DEFAULT_CONFIG)
    else:
        graph = rn.build_osm_graph()

    rn.save_graphml(graph, config.ROAD_GRAPHML)
    print(f"已寫出 {config.ROAD_GRAPHML}（{graph.number_of_nodes()} 節點 / {graph.number_of_edges()} 邊）")


if __name__ == "__main__":
    main()
