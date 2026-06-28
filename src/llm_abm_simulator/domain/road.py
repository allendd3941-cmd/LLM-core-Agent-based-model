"""road.py — 道路（ROADLINK edge）資料模型。

欄位對齊 GAML ``species road`` 與其 ``build_road_payload``，
讓 Python 路網具備與原 GAMA 道路 agent 相同的可用屬性。

一條 Road 對應路網圖上的一條 edge（node_a → node_b）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString


@dataclass
class Road:
    """單一道路路段。"""

    # === 拓樸與識別（對齊 ROADLINK index / a / b / NAME / highway）===
    road_id: str
    node_a: str
    node_b: str
    length: float                       # 公尺
    highway: str = ""                   # OSM highway tag（primary/secondary/...）
    highway_type: str = ""              # 對齊 GAML highway_ty
    road_name: str = ""                 # 對齊 ROADLINK NAME

    # === 速度與容量（依 highway type 推估，對齊 GAML speed_car/speed_moto/lanes/capacity）===
    speed_car: float = 45.0             # km/h
    speed_moto: float = 35.0            # km/h
    lanes: float = 1.0
    capacity: float = 30.0              # 同時容納車輛數的代理值

    # === 旅行時間（對齊 GAML time_car / time_moto / time）===
    time_car: float = 0.0              # 秒（length / speed_car）
    time_moto: float = 0.0            # 秒
    travel_time: float = 0.0          # 秒（代表性旅行時間）

    # === 動態狀態（每 step 更新）===
    current_flow: int = 0              # 目前在此 edge 上的車輛數
    congestion_proxy: float = 0.0      # flow / capacity，截斷於 [0, 1]
    weight: float = 1.0                # 路徑規劃用動態權重

    # === 幾何（WGS84，前端 GeoJSON 用；以 (lng, lat) 序列）===
    geometry_wgs84: LineString | None = None

    def __post_init__(self) -> None:
        # 補算旅行時間（若來源未提供）
        if self.time_car <= 0.0 and self.speed_car > 0.0:
            self.time_car = self.length / (self.speed_car / 3.6)
        if self.time_moto <= 0.0 and self.speed_moto > 0.0:
            self.time_moto = self.length / (self.speed_moto / 3.6)
        if self.travel_time <= 0.0:
            self.travel_time = self.time_car
        # 初始靜態權重 = 長度（對齊 GAML weight <- max([perimeter, 1.0])）
        self.weight = max(self.length, 1.0)

    # ------------------------------------------------------------------
    # 動態更新
    # ------------------------------------------------------------------

    def update_flow(self, flow: int, capacity_fallback: float, flow_multiplier: float) -> None:
        """依當前 flow 重算 congestion_proxy 與動態 weight。

        鏡像 GAML road.update_flow：
            congestion_proxy = min(1, flow / capacity)（capacity<=0 時用 fallback）
            weight = max(length,1) * (1 + flow * flow_multiplier)
        """
        self.current_flow = max(0, flow)
        denom = self.capacity if self.capacity > 0.0 else capacity_fallback
        self.congestion_proxy = min(1.0, self.current_flow / denom) if denom > 0 else 0.0
        self.weight = max(self.length, 1.0) * (1.0 + self.current_flow * flow_multiplier)

    def speed_limit_for(self, vehicle_type: str) -> float:
        """回傳該車種在此路段的速限（km/h）。對齊 GAML perceive_environment。"""
        return self.speed_moto if vehicle_type == "motorcycle" else self.speed_car

    def to_payload(self) -> dict:
        """對齊 GAML build_road_payload，供 LLM / 除錯使用。"""
        return {
            "road_id": self.road_id,
            "NAME": self.road_name,
            "highway": self.highway,
            "highway_ty": self.highway_type,
            "length": self.length,
            "lanes": self.lanes,
            "capacity": self.capacity,
            "speed_car": self.speed_car,
            "speed_moto": self.speed_moto,
            "time_car": self.time_car,
            "time_moto": self.time_moto,
            "time": self.travel_time,
            "current_flow": self.current_flow,
            "congestion_proxy": self.congestion_proxy,
        }
