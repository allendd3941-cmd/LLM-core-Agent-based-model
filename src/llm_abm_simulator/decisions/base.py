"""base.py — 決策來源的共同介面。

引擎只透過 ``DecisionPolicy`` 與決策來源互動（mock 或 LLM），符合計畫的
「LLM 整合是 adapter，不是核心」原則。兩個生命週期方法：

- ``initialize_agents``：產生每個 agent 的起點/車種/初始 mode/名稱（對齊 GAML init_agents）。
- ``decide_step``：每 step 回傳各 agent 的 active_mode（與可選 vehicle_type）更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..domain.agent import VehicleAgent


@dataclass
class InitAssignment:
    """init 階段對單一 agent 的指派。"""

    agent_id: str
    profile_name: str = ""
    origin_town: str = ""
    vehicle_type: str = ""
    active_mode: str = ""


@dataclass
class StepDecision:
    """每 step 對單一 agent 的決策。"""

    agent_id: str
    active_mode: str = ""
    vehicle_type: str = ""


@runtime_checkable
class DecisionPolicy(Protocol):
    """決策來源協定。"""

    name: str  # "mock" | "llm"

    def initialize_agents(
        self, agents: list[VehicleAgent], available_towns: list[str]
    ) -> dict[str, InitAssignment]:
        """回傳 agent_id → InitAssignment。"""
        ...

    def decide_step(
        self,
        agents: list[VehicleAgent],
        environment: dict[str, Any],
        cycle: int,
    ) -> dict[str, StepDecision]:
        """回傳 agent_id → StepDecision。"""
        ...
