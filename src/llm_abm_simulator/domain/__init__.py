"""domain — 純資料模型層。

只放 dataclass / enum，不做任何 I/O、不依賴 geopandas / networkx，
以便快速單元測試並清楚表達 GAMA 模型對應的狀態。
"""

from __future__ import annotations

from .agent import VehicleAgent
from .events import RouteStatus
from .road import Road
from .state import AgentSnapshot, RoadSnapshot, SimulationState
from .town import Town

__all__ = [
    "VehicleAgent",
    "RouteStatus",
    "Road",
    "Town",
    "AgentSnapshot",
    "RoadSnapshot",
    "SimulationState",
]
