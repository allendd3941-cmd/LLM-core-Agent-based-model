"""metrics.py — 壅塞/分佈指標與 CSV 輸出（取代 GAMA 的 CSV）。

對齊 GAML：
- 整體環境摘要 build_overall_environment_payload（active_road_count / crowded_road_count /
  average_congestion_proxy）。
- mode / status 分佈。
- per-cycle 指標（前端圖表與匯出用）。
- agent_memory.csv / road_flow.csv 欄位完全對齊 GAML save 語句。

CSV 寫到 ``output/``（已被 .gitignore 忽略）。
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .. import config
from ..domain.agent import VehicleAgent
from ..domain.road import Road

# 欄位順序對齊 GAML save 語句（Traffic_ABM_LLM_complete_v2.gaml init 區）
AGENT_MEMORY_HEADER = [
    "cycle", "agent_id", "origin_town", "destination_town",
    "current_town", "current_road_id", "next_road_id",
    "active_mode", "vehicle_type", "speed_kmh", "distance_moved_m",
    "nearby_agent_count", "route_status", "api_status", "warning",
]
ROAD_FLOW_HEADER = [
    "cycle", "road_id", "road_name", "highway", "highway_type",
    "current_flow", "capacity", "congestion_proxy",
]


def overall_environment(roads: list[Road], cfg: config.SimulationConfig,
                        agent_count: int) -> dict[str, Any]:
    """整體環境摘要（對齊 GAML build_overall_environment_payload）。"""
    active = 0
    crowded = 0
    congestion_sum = 0.0
    for r in roads:
        if r.current_flow >= cfg.active_road_min_flow:
            active += 1
        if r.congestion_proxy >= cfg.crowded_road_threshold:
            crowded += 1
        congestion_sum += r.congestion_proxy
    avg = (congestion_sum / active) if active > 0 else 0.0
    return {
        "cycle": None,  # 由呼叫端補
        "agent_count": agent_count,
        "destination_town": cfg.destination_town_name,
        "active_road_count": active,
        "crowded_road_count": crowded,
        "average_congestion_proxy": round(avg, 4),
    }


def distributions(agents: list[VehicleAgent]) -> tuple[dict[str, int], dict[str, int]]:
    """mode 分佈與 status 分佈。"""
    mode = Counter(a.active_mode for a in agents)
    status = Counter(str(a.route_status) for a in agents)
    return dict(mode), dict(status)


class MetricsRecorder:
    """負責 per-cycle 指標累積與 CSV 寫出。"""

    def __init__(self, cfg: config.SimulationConfig) -> None:
        self.cfg = cfg
        self.history: list[dict[str, Any]] = []
        self._agent_csv: Path = config.AGENT_MEMORY_CSV
        self._road_csv: Path = config.ROAD_FLOW_CSV

    # ---- CSV 初始化（rewrite header，對齊 GAML init save rewrite:true）----
    def init_csv(self) -> None:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with self._agent_csv.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(AGENT_MEMORY_HEADER)
        with self._road_csv.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(ROAD_FLOW_HEADER)

    def append_agent_rows(self, cycle: int, agents: list[VehicleAgent]) -> None:
        with self._agent_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for a in agents:
                w.writerow([
                    cycle, a.agent_id, a.origin_town, a.destination_town,
                    a.current_town, a.current_road_id, a.next_road_id,
                    a.active_mode, a.vehicle_type, round(a.speed_kmh, 2),
                    round(a.distance_moved_last_step, 2), a.nearby_agent_count,
                    str(a.route_status), a.api_status, a.warning_message,
                ])

    def append_road_rows(self, cycle: int, roads: list[Road]) -> None:
        with self._road_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for r in roads:
                # 只輸出有流量的道路，避免每步寫出數萬列（對齊 active road 概念）
                if r.current_flow <= 0:
                    continue
                w.writerow([
                    cycle, r.road_id, r.road_name, r.highway, r.highway_type,
                    r.current_flow, r.capacity, round(r.congestion_proxy, 4),
                ])

    def record_cycle(self, cycle: int, env: dict[str, Any],
                     mode_dist: dict[str, int], status_dist: dict[str, int]) -> dict[str, Any]:
        entry = {
            "cycle": cycle,
            "elapsed_minutes": cycle * self.cfg.step_minutes,
            "active_road_count": env["active_road_count"],
            "crowded_road_count": env["crowded_road_count"],
            "average_congestion_proxy": env["average_congestion_proxy"],
            "arrived": status_dist.get("arrived", 0),
            "moving": status_dist.get("moving", 0),
            "mode_distribution": mode_dist,
            "status_distribution": status_dist,
        }
        self.history.append(entry)
        return entry

    def reset(self) -> None:
        self.history.clear()
