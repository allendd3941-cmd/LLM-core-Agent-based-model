"""metrics.py — 壅塞/分佈指標累積（前端圖表與模擬後分析用）。

提供：
- 整體環境摘要 build_overall_environment_payload（active_road_count / crowded_road_count /
  average_congestion_proxy）。
- mode / status 分佈。
- per-cycle 指標累積（``MetricsRecorder.history``）→ 前端圖表與 ``build_analysis`` 都讀記憶體中的
  history（不落地任何 CSV）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import config
from ..domain.agent import VehicleAgent
from ..domain.road import Road


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
    """負責 per-cycle 指標累積（記憶體中的 history，供前端圖表與模擬後分析）。"""

    def __init__(self, cfg: config.SimulationConfig) -> None:
        self.cfg = cfg
        self.history: list[dict[str, Any]] = []

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
            "signal_waiting": env.get("signal_waiting", 0),
            "event_on_network": env.get("event_on_network", 0),     # 事件車在網路上的台數（路網層）
            "ambient_on_network": env.get("ambient_on_network", 0),  # 背景車在網路上的台數（路網層）
            "mode_distribution": mode_dist,
            "status_distribution": status_dist,
        }
        self.history.append(entry)
        return entry

    def reset(self) -> None:
        self.history.clear()
