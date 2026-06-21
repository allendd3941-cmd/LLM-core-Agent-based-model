"""registry.py — 可選「決策核心」的具名登錄表（前端選擇器用）。

引擎透過 ``DecisionPolicy`` 抽象與決策核心互動（見 base.py）。本登錄表把可選核心做成
具名項目，供前端下拉與 init payload 下發；未來要加第三種核心（例如不同啟發式）只要在此
註冊，引擎與前端不必再改。這也對齊 demo paper 的對照：**LLM 認知核心 vs 規則式核心**，
跑同一張路網、同一波事件車流，比抵達曲線 / 繞道行為 / 壅塞。

注意：核心 key 同時是 ``engine.last_decision_source`` 的字串（"rule" | "llm"），
會下發前端顯示（見 simulation.js 的 CORE_LABEL）。
"""

from __future__ import annotations

CORES: dict[str, dict[str, str]] = {
    "rule": {
        "key": "rule",
        "label": "規則式（Rule-based）",
        "desc": "確定性啟發式決策核心，零 LLM 成本、即時流暢；作為對照基線（baseline）。",
    },
    "llm": {
        "key": "llm",
        "label": "LLM 認知核心",
        "desc": "本進程直呼 llm_server pipeline，依人格 persona 與環境感知做決策（需 vLLM）。",
    },
}

DEFAULT_CORE = "rule"


def is_valid(key: str) -> bool:
    return key in CORES


def summaries() -> list[dict[str, str]]:
    """給前端決策核心下拉的清單。"""
    return list(CORES.values())
