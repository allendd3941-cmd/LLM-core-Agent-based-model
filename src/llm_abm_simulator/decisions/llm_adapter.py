"""llm_adapter.py — 呼叫既有 LLM pipeline 的決策來源（in-process 直呼）。

本 adapter 組出與 GAML 等價的 init_agents / step_update payload（對齊
Traffic_ABM_LLM_complete_v2.gaml 的 send_initial_request / send_step_request），
直接在本進程呼叫 ``llm_server`` 的 pipeline 函式（run_agent_profile /
run_perception / run_decision_making），取得 LLM 原始文字後用 ``response_parser`` 解析。

設計為同步（直呼）；web 層會以 executor 在背景執行緒呼叫，避免阻塞事件迴圈。
pipeline 不可用或解析不到時，回傳空 dict 並設 ``last_call_ok=False``，由引擎 fallback 到 mock。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import SimulationConfig
from ..domain.agent import VehicleAgent
from . import response_parser as rp
from .base import InitAssignment, StepDecision

logger = logging.getLogger(__name__)

_MODEL_NAME = "TrafficABM_Tainan_LLM"


class LLMDecisionPolicy:
    """在本進程直呼 llm_server pipeline 的決策來源。"""

    name = "llm"

    def __init__(self, cfg: SimulationConfig) -> None:
        self.cfg = cfg
        self.available_towns: list[str] = []
        self.last_call_ok: bool = False

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """健康檢查：能否匯入 llm_server pipeline。"""
        import importlib.util
        try:
            return importlib.util.find_spec("llm_server") is not None
        except (ImportError, ValueError):
            return False

    # ------------------------------------------------------------------
    def initialize_agents(
        self, agents: list[VehicleAgent], available_towns: list[str]
    ) -> dict[str, InitAssignment]:
        self.available_towns = available_towns
        payload = self._build_init_payload(agents, available_towns)
        body = self._call_inproc(payload)
        if body is None:
            self.last_call_ok = False
            return {}

        rows = rp.parse_rows(body, available_towns, self.cfg.default_origin_town)
        self.last_call_ok = bool(rows)
        return self._rows_to_init(rows, agents)

    def decide_step(
        self, agents: list[VehicleAgent], environment: dict[str, Any], cycle: int
    ) -> dict[str, StepDecision]:
        payload = self._build_step_payload(agents, environment, cycle)
        body = self._call_inproc(payload)
        if body is None:
            self.last_call_ok = False
            return {}

        rows = rp.parse_rows(body, self.available_towns, self.cfg.default_origin_town)
        self.last_call_ok = bool(rows)
        return self._rows_to_step(rows, agents)

    # ------------------------------------------------------------------
    # payload 組裝（對齊 GAML）
    # ------------------------------------------------------------------
    def _build_init_payload(self, agents: list[VehicleAgent], towns: list[str]) -> dict[str, Any]:
        return {
            "request_type": "init_agents",
            "model": _MODEL_NAME,
            "model_name": _MODEL_NAME,
            "cycle": 0,
            "vehicles": len(agents),
            "step_minutes": self.cfg.step_minutes,
            "max_steps": self.cfg.max_steps,
            "available_towns": towns,
            "requested_agents": [
                {
                    "agent_id": a.agent_id,
                    "fallback_origin_town": a.origin_town or self.cfg.default_origin_town,
                    "fixed_destination_town": a.destination_town,
                    "active_mode": a.build_active_mode_payload(),
                    "vehicle_type": a.vehicle_type,
                }
                for a in agents
            ],
        }

    def _build_step_payload(
        self, agents: list[VehicleAgent], environment: dict[str, Any], cycle: int
    ) -> dict[str, Any]:
        return {
            "request_type": "step_update",
            "model": _MODEL_NAME,
            "model_name": _MODEL_NAME,
            "cycle": cycle,
            "environment": environment,
            "agents_status": [a.build_api_payload() for a in agents],
        }

    # ------------------------------------------------------------------
    def _rows_to_init(
        self, rows: list[dict[str, Any]], agents: list[VehicleAgent]
    ) -> dict[str, InitAssignment]:
        by_id = {a.agent_id: a for a in agents}
        unassigned = [a.agent_id for a in agents]
        result: dict[str, InitAssignment] = {}
        for row in rows:
            aid = row["agent_id"] if row["agent_id"] in by_id else (unassigned.pop(0) if unassigned else None)
            if aid is None:
                continue
            if aid in unassigned:
                unassigned.remove(aid)
            result[aid] = InitAssignment(
                agent_id=aid,
                profile_name=row["profile_name"],
                origin_town=row["origin_town"],
                vehicle_type=row["vehicle_type"],
                active_mode=row["active_mode"],
            )
        return result

    def _rows_to_step(
        self, rows: list[dict[str, Any]], agents: list[VehicleAgent]
    ) -> dict[str, StepDecision]:
        by_id = {a.agent_id: a for a in agents}
        # LLM 回應以 agent name 對應；建立 name → id 對照（GAML 用 profile_agent_name 比對）
        by_name = {a.profile_name: a.agent_id for a in agents if a.profile_name}
        result: dict[str, StepDecision] = {}
        ordered = [a.agent_id for a in agents]
        for idx, row in enumerate(rows):
            aid = row["agent_id"] if row["agent_id"] in by_id else by_name.get(row["profile_name"])
            if aid is None and idx < len(ordered):
                aid = ordered[idx]   # 最後手段：依順序對應
            if aid is None:
                continue
            result[aid] = StepDecision(
                agent_id=aid,
                active_mode=row["active_mode"],
                vehicle_type=row["vehicle_type"],
                reason=row.get("reason", ""),
            )
        return result

    # ------------------------------------------------------------------
    # 傳輸層：在本進程直呼 llm_server pipeline
    # ------------------------------------------------------------------
    def _call_inproc(self, payload: dict[str, Any]) -> Any | None:
        """直接在本進程跑 llm_server pipeline（persona 池 → perception → decision_making）。

        回傳 run_decision_making 的 LLM 原始文字，供下游 response_parser 解析。
        匯入或執行失敗時回 None，由上層 fallback 到 mock。
        """
        try:
            from llm_server.decision_making import run_decision_making
            from llm_server.perception import run_perception
        except ImportError as e:
            logger.warning("in-process LLM pipeline 匯入失敗（檢查 llm_server 是否可匯入）：%s", e)
            return None

        try:
            from .. import config
            from . import profile_pool
            # persona 池：穩定快取、按需切前 n 個；不會因為調 agent 數而一直重生（見 profile_pool）。
            agent_profile = profile_pool.ensure_and_slice(self.cfg.nb_agents, config.PROFILE_CONFIG.pool_size)

            perception = run_perception(payload, output=True)
            return run_decision_making(agent_profile, perception, output=True)
        except Exception as e:  # noqa: BLE001  pipeline 內可能丟各種錯，一律降級
            logger.warning("in-process LLM pipeline 執行失敗：%s", e)
            return None
