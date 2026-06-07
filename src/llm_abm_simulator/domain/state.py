"""state.py — 模擬狀態快照（給 WebSocket 廣播與測試斷言用）。

這些是「輸出用」的精簡 dict-friendly 資料結構，與前端協定一致。
座標一律為 WGS84（lat/lng），由 spatial 層在組裝時轉換完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentSnapshot:
    """單一 agent 在某 cycle 的對外狀態。"""

    agent_id: str
    profile_name: str
    lat: float
    lng: float
    route_status: str
    active_mode: str
    vehicle_type: str
    speed_kmh: float
    congestion_proxy: float
    distance_to_destination: float
    nearby_agent_count: int
    origin_town: str
    destination_town: str
    current_town: str
    current_road_id: str
    trip_summary: str = ""          # long_term_memory 的整趟旅次摘要（前端點擊 agent 顯示）

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RoadSnapshot:
    """單一道路在某 cycle 的對外狀態（前端上色用）。"""

    road_id: str
    flow: int
    capacity: float
    congestion_proxy: float
    color: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SimulationState:
    """單一 cycle 的完整對外狀態（WebSocket state_update 訊息主體）。"""

    cycle: int
    elapsed_minutes: int
    max_steps: int
    running: bool
    finished: bool
    decision_source: str                              # "mock" | "llm"
    agents: list[AgentSnapshot] = field(default_factory=list)
    roads: list[RoadSnapshot] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    mode_distribution: dict[str, int] = field(default_factory=dict)
    status_distribution: dict[str, int] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """轉成 WebSocket JSON 訊息。"""
        return {
            "type": "state_update",
            "cycle": self.cycle,
            "elapsed_minutes": self.elapsed_minutes,
            "max_steps": self.max_steps,
            "running": self.running,
            "finished": self.finished,
            "decision_source": self.decision_source,
            "agents": [a.to_dict() for a in self.agents],
            "roads": [r.to_dict() for r in self.roads],
            "metrics": self.metrics,
            "mode_distribution": self.mode_distribution,
            "status_distribution": self.status_distribution,
        }
