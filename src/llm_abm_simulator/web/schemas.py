"""schemas.py — WebSocket 控制訊息結構（文件用途為主）。

前端送來的控制訊息格式：

    {"type": "control", "action": <action>, "value": <optional>}

action 一覽：
    start / pause / resume / step / reset
    set_speed   value: float（0.5 ~ 5.0 倍速）
    set_agents  value: int（重設前可調 agent 數）
    set_mode    value: "mock" | "llm"

伺服器回送訊息 type：
    status        {"type":"status","message":str}
    init          初始化 GeoJSON（見 SimulationEngine.init_payload）
    state_update  每 cycle 狀態（見 domain.state.SimulationState.to_message）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ControlMessage(BaseModel):
    type: str = "control"
    action: str
    value: Any = Field(default=None)
