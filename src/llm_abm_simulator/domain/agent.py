"""agent.py — 車輛 agent 資料模型。

欄位與行為對齊 GAML ``species vehicle skills:[moving]``：
identity / active_mode 偏好 / 旅程狀態 / 感知狀態 / API & memory。

設計取捨：本類別只持有「狀態」與「狀態轉換」（套用 active_mode、記錄 memory、
組 payload），不直接依賴 networkx。實際的路徑規劃與移動由 ``simulation.engine``
搭配 ``spatial`` 驅動，使 domain 層維持可純單元測試。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import SimulationConfig
from .events import RouteStatus


@dataclass
class VehicleAgent:
    """單一車輛 agent。"""

    # === identity（GAML: agent_id / profile_agent_name）===
    agent_id: str
    profile_name: str = ""

    # === 旅程起訖（GAML: origin_town / destination_town / vehicle_type）===
    origin_town: str = ""
    destination_town: str = ""
    vehicle_type: str = "汽車"

    # === active_mode（GAML: mode_name + 一組移動偏好權重）===
    active_mode: str = "fast"
    desired_speed: float = 40.0          # km/h
    speed_car_preference: float = 45.0
    speed_moto_preference: float = 35.0
    road_type_preference: list[str] = field(default_factory=list)
    route_randomness: float = 0.15
    comfort_weight: float = 0.20
    time_weight: float = 0.45
    distance_weight: float = 0.25
    capacity_weight: float = 0.10
    custom_params: dict[str, Any] = field(default_factory=dict)

    # === 旅程狀態（GAML: route_status / waiting_for_origin / next_road_id）===
    route_status: RouteStatus = RouteStatus.CREATED
    waiting_for_origin: bool = False
    next_road_id: str = "calculating"

    # === 路網位置（公尺座標 EPSG:3826）===
    x: float = 0.0
    y: float = 0.0
    current_node: str | None = None
    destination_node: str | None = None
    current_path: list[str] = field(default_factory=list)
    path_index: int = 0
    edge_progress: float = 0.0          # 已在「目前這條邊」上前進的公尺（支援跨步在長邊上推進）
    current_road_id: str = ""
    current_town: str = ""

    # === 感知與移動狀態（GAML: speed / perception_radius / is_crowded / ...）===
    speed_kmh: float = 40.0
    perception_radius: float = 300.0
    is_crowded: bool = False
    distance_moved_last_step: float = 0.0
    distance_to_destination: float = 0.0
    nearby_agent_count: int = 0
    congestion_proxy: float = 0.0
    selected_action: str = "none"

    # === API & memory（GAML: travel_memory / api_status / warning_message）===
    travel_memory: list[dict[str, Any]] = field(default_factory=list)
    api_status: str = "not_sent"
    warning_message: str = ""

    # ------------------------------------------------------------------
    # 工廠：以 config 預設值建立
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, agent_id: str, cfg: SimulationConfig) -> "VehicleAgent":
        return cls(
            agent_id=agent_id,
            vehicle_type=cfg.default_vehicle_type,
            destination_town=cfg.destination_town_name,
            desired_speed=cfg.default_desired_speed_kmh,
            speed_car_preference=cfg.default_speed_car_kmh,
            speed_moto_preference=cfg.default_speed_moto_kmh,
            road_type_preference=list(cfg.default_road_type_preference),
            route_randomness=cfg.default_route_randomness,
            comfort_weight=cfg.default_comfort_weight,
            time_weight=cfg.default_time_weight,
            distance_weight=cfg.default_distance_weight,
            capacity_weight=cfg.default_capacity_weight,
            perception_radius=cfg.perception_radius_m,
            speed_kmh=cfg.default_desired_speed_kmh,
        )

    # ------------------------------------------------------------------
    # active_mode 套用（鏡像 GAML apply_active_mode）
    # ------------------------------------------------------------------
    def apply_active_mode(self, payload: dict[str, Any] | str | None) -> None:
        """從決策回應套用 active_mode；缺少的欄位保留原值。

        接受兩種形式：
        - 字串：直接視為 mode 名稱（例如 "fast"）。
        - dict：含 mode_name / move_speed / 各權重等（GAML active_mode map）。
        """
        if payload is None:
            return
        if isinstance(payload, str):
            self.active_mode = payload or self.active_mode
            return

        if "mode_name" in payload:
            self.active_mode = str(payload["mode_name"])
        elif "mode" in payload:
            self.active_mode = str(payload["mode"])

        _set = self._maybe_set_float
        _set(payload, "move_speed", "desired_speed")
        _set(payload, "speed_car", "speed_car_preference")
        _set(payload, "speed_moto", "speed_moto_preference")
        _set(payload, "route_randomness", "route_randomness")
        _set(payload, "comfort_weight", "comfort_weight")
        _set(payload, "time_weight", "time_weight")
        _set(payload, "distance_weight", "distance_weight")
        _set(payload, "capacity_weight", "capacity_weight")
        if "custom_params" in payload and isinstance(payload["custom_params"], dict):
            self.custom_params = dict(payload["custom_params"])

    def _maybe_set_float(self, payload: dict[str, Any], key: str, attr: str) -> None:
        if key in payload:
            try:
                setattr(self, attr, float(payload[key]))
            except (TypeError, ValueError):
                pass

    def apply_vehicle_type(self, requested: str) -> None:
        """套用車種；只允許「汽車」/「機車」（鏡像 GAML normalize_vehicle_type）。"""
        if not requested:
            return
        if "機車" in requested:
            self.vehicle_type = "機車"
        elif "汽車" in requested:
            self.vehicle_type = "汽車"

    # ------------------------------------------------------------------
    # 權重輸出（給 routing 用）
    # ------------------------------------------------------------------
    def routing_weights(self) -> dict[str, float]:
        """回傳給路徑規劃使用的權重（time/distance/comfort/capacity）。"""
        return {
            "time": self.time_weight,
            "distance": self.distance_weight,
            "comfort": self.comfort_weight,
            "capacity": self.capacity_weight,
        }

    # ------------------------------------------------------------------
    # payload（對齊 GAML build_api_agent_payload / build_active_mode_payload）
    # ------------------------------------------------------------------
    def build_active_mode_payload(self) -> dict[str, Any]:
        return {
            "mode_name": self.active_mode,
            "move_speed": self.desired_speed,
            "speed_car": self.speed_car_preference,
            "speed_moto": self.speed_moto_preference,
            "road_type_preference": list(self.road_type_preference),
            "route_randomness": self.route_randomness,
            "comfort_weight": self.comfort_weight,
            "time_weight": self.time_weight,
            "distance_weight": self.distance_weight,
            "capacity_weight": self.capacity_weight,
            "custom_params": dict(self.custom_params),
        }

    def build_environment_payload(self) -> dict[str, Any]:
        """對齊 GAML build_environment_payload（agent 局部環境）。"""
        return {
            "current_town": self.current_town,
            "current_road_id": self.current_road_id,
            "route_status": str(self.route_status),
            "nearby_agent_count": self.nearby_agent_count,
            "congestion_proxy": self.congestion_proxy,
            "distance_to_destination_m": self.distance_to_destination,
        }

    def build_api_payload(self) -> dict[str, Any]:
        """對齊 GAML build_api_agent_payload（每 step 送給 /from-gama）。"""
        return {
            "agent_id": self.agent_id,
            "agent_name": self.profile_name,
            "origin_town": self.origin_town,
            "destination_town": self.destination_town,
            "active_mode": self.active_mode,
            "vehicle_type": self.vehicle_type,
            "environment": self.build_environment_payload(),
            "memory": self.travel_memory,
        }

    def build_memory_entry(self, cycle: int) -> dict[str, Any]:
        """對齊 GAML build_memory_entry。"""
        return {
            "cycle": cycle,
            "current_town": self.current_town,
            "current_road_id": self.current_road_id,
            "active_mode": self.active_mode,
            "vehicle_type": self.vehicle_type,
            "route_status": str(self.route_status),
            "nearby_agent_count": self.nearby_agent_count,
            "congestion_proxy": self.congestion_proxy,
            "distance_to_destination_m": self.distance_to_destination,
        }
