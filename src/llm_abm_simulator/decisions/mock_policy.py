"""mock_policy.py — 規則式（rule-based）決策核心（無需 LLM）。

這是兩個可選決策核心之一（另一個是 LLM 認知核心，見 llm_adapter.py；登錄於 registry.py）。
提供 demo 預設的即時決策，並作為 paper 的對照基線（baseline）；同一個 seed 必產生相同結果（可重現）。
背景常態車流（ambient）也一律由本核心驅動（不吃 LLM、不存記憶）。
所有隨機都走注入的 RNG。init 階段指派名稱/車種/起點/初始 mode；step 階段依壅塞與距離選 action_mode。
核心 key（``name``）為 ``"rule"``，會成為 engine.last_decision_source 並下發前端顯示。
"""

from __future__ import annotations

import random
from typing import Any

from ..config import SimulationConfig
from ..domain.agent import VehicleAgent
from .base import InitAssignment, StepDecision

_NAME_POOL = (
    "陳大明", "林小華", "王志偉", "張美玲", "李建宏", "黃雅芳", "吳家豪", "劉淑娟",
    "蔡宗翰", "楊佩琪", "鄭凱文", "許雅婷", "謝明哲", "郭怡君", "洪俊傑", "蕭淑芬",
    "賴冠廷", "曾惠玲", "盧志明", "葉雅琪", "呂文傑", "施佳穎", "朱建國", "潘慧如",
    "廖宗瑋", "趙美珍", "周志豪", "高淑惠", "沈彥廷", "邱雅琳",
)

# 初始 action_mode 的說明(出場時尚無路況→以「初始計畫」陳述);之後每步/壅塞重決會覆寫成實際理由。
_INIT_REASON = {
    "fast": "Starting out: aiming to reach the destination quickly.",
    "avoid_congestion": "Starting out: planning to route around congestion.",
    "tolerate_congestion": "Starting out: will keep the route and tolerate some congestion.",
}


class MockDecisionPolicy:
    """規則式（rule-based）決策來源。"""

    name = "rule"

    def __init__(self, cfg: SimulationConfig, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng

    # ------------------------------------------------------------------
    def initialize_agents(
        self, agents: list[VehicleAgent], available_towns: list[str]
    ) -> dict[str, InitAssignment]:
        from ..config import ACTION_MODES, VEHICLE_TYPES

        towns = available_towns or [self.cfg.default_origin_town]
        result: dict[str, InitAssignment] = {}
        for agent in agents:
            # 維持原 RNG 抽取順序(name→town→vehicle→mode),確保 seed 可重現/golden 指紋不變;reason 不耗 RNG。
            profile_name = _NAME_POOL[self.rng.randrange(len(_NAME_POOL))]
            origin_town = towns[self.rng.randrange(len(towns))]
            vehicle_type = self.rng.choice(VEHICLE_TYPES)
            mode = self.rng.choice(ACTION_MODES)
            result[agent.agent_id] = InitAssignment(
                agent_id=agent.agent_id,
                profile_name=profile_name,
                origin_town=origin_town,
                vehicle_type=vehicle_type,
                action_mode=mode,
                reason=_INIT_REASON.get(mode, ""),
            )
        return result

    # ------------------------------------------------------------------
    def decide_step(
        self,
        agents: list[VehicleAgent],
        environment: dict[str, Any],
        cycle: int,
    ) -> dict[str, StepDecision]:
        """依壅塞/距離/車種選 action_mode（確定性規則，不用隨機）。"""
        decisions: dict[str, StepDecision] = {}
        for agent in agents:
            congestion = agent.congestion_proxy
            distance = agent.distance_to_destination
            if congestion > 0.7:
                mode, reason = "avoid_congestion", "Heavy congestion; taking a smoother route"
            elif congestion > 0.4:
                mode, reason = "tolerate_congestion", "Moderate traffic; staying the course"
            elif distance < 2000:
                mode, reason = "fast", "Close to destination; heading straight there"
            elif agent.vehicle_type == "car":
                mode, reason = "fast", "Clear roads; want to arrive quickly"
            else:
                mode, reason = "tolerate_congestion", "Riding straight through, no detour"
            decisions[agent.agent_id] = StepDecision(
                agent_id=agent.agent_id, action_mode=mode, reason=reason)
        return decisions
