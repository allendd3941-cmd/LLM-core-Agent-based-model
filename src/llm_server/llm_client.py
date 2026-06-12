"""llm_client.py — 統一的 LLM 呼叫 adapter（後端可切換）。

由 ``llm_config.LLM_BACKEND`` 決定後端：
- ``"ollama"``（預設）：走 Ollama 原生 ``/api/generate``。**與原本各檔手刻的呼叫行為完全一致**
  （保留 system / think / options / 單一 prompt），切到此後端輸出不變、零回歸。
- ``"vllm"``：走 OpenAI 相容 ``/v1/chat/completions``（vLLM continuous batching 高並行用）。
  system→system message、prompt→user message，並把 Ollama options 映射成 OpenAI 參數。

所有 prompt 內容不變；本檔只集中「怎麼把 prompt 送出去、怎麼把回應取回來」這層 transport，
讓 perception / decision_making / agent_profile / memory_summary 共用同一個入口、且能一鍵換後端。
"""

from __future__ import annotations

from typing import Any

import requests

from . import llm_config
from .timer import time_counter


def generate(
    prompt: str,
    system: str = "",
    options: dict[str, Any] | None = None,
    think: str | None = None,
    model: str | None = None,
    label: str = "llm",
    timeout: float | None = None,
) -> str:
    """送一段 prompt 給 LLM，回傳純文字回應。後端由 llm_config.LLM_BACKEND 決定。

    Args:
        prompt:   使用者 prompt（單一字串，與原本相同）。
        system:   system prompt（可空）。
        options:  Ollama 風格 options（seed / temperature / top_k / num_predict…）；
                  vLLM 後端會自動映射成 OpenAI 參數。
        think:    Ollama reasoning 旗標（如 "low"）；非 thinking 模型/ vLLM 後端會忽略。
        model:    覆寫模型名（預設用該後端的設定模型）。
        label:    計時 log 用標籤。
        timeout:  HTTP 逾時秒數（None＝不設限，保留原行為）。
    """
    if llm_config.LLM_BACKEND == "vllm":
        url, payload, parse = _build_vllm(prompt, system, options, model)
    else:
        url, payload, parse = _build_ollama(prompt, system, options, think, model)

    @time_counter
    def _post(u: str, p: dict[str, Any]) -> requests.Response:
        r = requests.post(u, json=p, timeout=timeout)
        r.raise_for_status()
        return r

    resp = _post(url, payload, file_name=label)
    return parse(resp.json())


# ---------------------------------------------------------------------------
# Ollama 原生 /api/generate（保留原行為）
# ---------------------------------------------------------------------------
def _build_ollama(prompt, system, options, think, model):
    url = f"{llm_config.OLLAMA_URL}{llm_config.OLLAMA_MODE}"
    payload: dict[str, Any] = {
        "model": model or llm_config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system
    if think:
        payload["think"] = think
    if options:
        payload["options"] = options

    def parse(body: dict[str, Any]) -> str:
        return body["response"]

    return url, payload, parse


# ---------------------------------------------------------------------------
# vLLM / OpenAI 相容 /v1/chat/completions
# ---------------------------------------------------------------------------
def _build_vllm(prompt, system, options, model):
    url = f"{llm_config.VLLM_URL.rstrip('/')}/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "model": model or llm_config.VLLM_MODEL,
        "messages": messages,
        "stream": False,
    }
    # Ollama options → OpenAI 參數映射
    if options:
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "seed" in options:
            payload["seed"] = options["seed"]
        if "num_predict" in options:           # Ollama 的輸出長度上限
            payload["max_tokens"] = options["num_predict"]
        if "top_k" in options:                 # vLLM 擴充參數（非 OpenAI 標準）
            payload.setdefault("extra_body", {})["top_k"] = options["top_k"]

    def parse(body: dict[str, Any]) -> str:
        return body["choices"][0]["message"]["content"]

    return url, payload, parse
