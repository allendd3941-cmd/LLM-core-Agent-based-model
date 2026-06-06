"""events.py — 模擬生命週期狀態列舉。

對齊 GAML vehicle.route_status 的字串值，但收斂成 enum 以便型別檢查。
GAML 中出現過的值：created / waiting_for_api_origin / spawned_from_api /
moving / arrived / error。這裡保留相同語意。
"""

from __future__ import annotations

from enum import Enum


class RouteStatus(str, Enum):
    """車輛 agent 的路徑狀態。繼承 str 讓它可直接序列化成 JSON 字串。"""

    CREATED = "created"                       # 剛建立、尚未指派起點
    WAITING = "waiting_for_api_origin"        # 等待 LLM/init 回傳 origin
    SPAWNED = "spawned_from_api"              # 已依 origin 放到路網上
    MOVING = "moving"                         # 沿路徑移動中
    ARRIVED = "arrived"                       # 已抵達目的地
    ERROR = "error"                           # 無法規劃路徑等錯誤狀態

    def __str__(self) -> str:  # 讓 f-string / CSV 輸出為純值
        return self.value
