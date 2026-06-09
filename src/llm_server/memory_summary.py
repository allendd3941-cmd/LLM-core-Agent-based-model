"""memory_summary.py — 用小模型（如 gemma）批次生成 agent 長期記憶的 trip_summary。

設計：把每個 agent 已經算好的「結構化事實」（出發/目的地、塞過的點、換策略次數、整趟順暢度、
已行進時間、目前狀態）批次包成一個 prompt，呼叫 Ollama 上的 ``summary_model``，回傳
``{agent_id: 一句繁體中文摘要}``。只負責「把事實寫成一句話」，不得新增事實。

由 simulation/engine.py 在開啟 ``[summary].use_llm_summary`` 時呼叫；任何失敗都回空 dict，
讓引擎保留各 agent 既有的模板摘要（fallback）。與 perception/decision_making 共用同一個 Ollama。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from . import json_utils
from .llm_config import OLLAMA_MODE, OLLAMA_URL

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "memory_prompt.txt"

with open(PROMPT_PATH, "r", encoding="utf-8") as f:
    PROMPT = f.read()


def run_memory_summary(facts: list[dict[str, Any]], model: str, timeout: float = 120.0) -> dict[str, str]:
    """批次生成摘要。facts 每筆需含 ``agent_id``。回傳 {agent_id: summary}；失敗回 {}。"""
    if not facts:
        return {}
    url = f"{OLLAMA_URL}{OLLAMA_MODE}"
    user_prompt = f"{PROMPT}\n\n以下是各 agent 的事實（JSON）：\n{json.dumps(facts, ensure_ascii=False)}"
    # 註：不傳 "think"。think 只有 reasoning 模型（如 gpt-oss）支援；對 llama3.1:8b 等
    # 非 thinking 模型，Ollama 會回 400 Bad Request。摘要是小任務，本就不需要 thinking。
    payload = {
        "model": model,
        "prompt": user_prompt,
        "options": {"seed": 42, "temperature": 0},
        "stream": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        text = resp.json()["response"]
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
