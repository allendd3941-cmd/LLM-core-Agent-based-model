"""decisions — 決策來源與回應解析。

兩種決策模式（對齊計畫：mock 預設、可切換 LLM）：
- ``mock_policy``    確定性規則決策（無需 LLM，demo 即時流暢）。
- ``llm_adapter``    呼叫既有 FastAPI ``/from-gama``（保留原 LLM pipeline 相容）。

``response_parser`` 提供與 GAML 等價的 robust 解析，兩種模式共用。
"""

from __future__ import annotations

from .base import DecisionPolicy, InitAssignment, StepDecision
from .llm_adapter import LLMDecisionPolicy
from .mock_policy import MockDecisionPolicy

__all__ = [
    "DecisionPolicy",
    "InitAssignment",
    "StepDecision",
    "MockDecisionPolicy",
    "LLMDecisionPolicy",
]
