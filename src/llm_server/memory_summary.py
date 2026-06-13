"""memory_summary.py — 用小模型批次生成 agent **單一 memory** 的 ``summary``。

設計：把每個 agent 已經算好的「結構化事實」（出發/目的地、塞過的點、換策略次數、整趟順暢度、
已行進時間、目前狀態）批次包成一個 prompt，呼叫 Ollama/vLLM 上的模型，回傳
``{agent_id: 一句繁體中文摘要}``。只負責「把事實寫成一句話」，不得新增事實。

由 simulation/engine.py 的 ``_summarize_memory`` 在 **LLM 核心模式下、該 agent「重新決策」時**
呼叫（記憶恰好在做決定的當下最新，也省 LLM）；任何失敗都回空 dict，讓引擎保留既有的模板摘要
（fallback）。與 perception/decision_making 共用同一個模型（前端所選）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from . import json_utils, llm_client

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "memory_prompt.txt"

with open(PROMPT_PATH, "r", encoding="utf-8") as f:
    PROMPT = f.read()


def run_memory_summary(facts: list[dict[str, Any]], model: str, timeout: float = 120.0) -> dict[str, str]:
    """批次生成摘要。facts 每筆需含 ``agent_id``。回傳 {agent_id: summary}；失敗回 {}。"""
    if not facts:
        return {}
    user_prompt = f"{PROMPT}\n\n以下是各 agent 的事實（JSON）：\n{json.dumps(facts, ensure_ascii=False)}"
    # 註：不傳 "think"。think 只有 reasoning 模型（如 gpt-oss）支援；對 llama3.1:8b 等
    # 非 thinking 模型會 400。摘要是小任務，本就不需要 thinking。
    try:
        text = llm_client.generate(
            user_prompt, options={"seed": 42, "temperature": 0},
            model=model, label="memory_summary", timeout=timeout)
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("memory_summary 呼叫失敗（model=%s）：%s", model, e)
        return {}
    return _parse(text)


def _parse(text: str) -> dict[str, str]:
    """從 LLM 原始文字抽出 {agent_id: summary}。強韌解析：結構壞掉/截斷也盡量救回。"""
    if not text:
        return {}
    obj = json_utils.loads_lenient(text)
    # 形式一：{agent_id: "摘要"} 直接對應
    if isinstance(obj, dict) and "summaries" not in obj:
        mapping = {str(k): str(v).strip() for k, v in obj.items()
                   if isinstance(v, str) and v.strip()}
        if mapping:
            return mapping
    # 形式二：{"summaries":[{agent_id, trip_summary}]}，或截斷時逐物件救回
    rows = json_utils.salvage_objects(text)
    out: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("agent_id"):
            s = row.get("trip_summary") or row.get("summary") or ""
            if s:
                out[str(row["agent_id"])] = str(s).strip()
    return out
