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

# 記住「不支援 thinking」的模型：第一次被 Ollama 以 400 拒絕後加入，
# 之後對同模型就不再帶 think（避免每次都先失敗一次）。
_NO_THINK_MODELS: set[str] = set()


def generate(
    prompt: str,
    system: str = "",
    options: dict[str, Any] | None = None,
    think: str | None = None,
    model: str | None = None,
    label: str = "llm",
    timeout: float | None = None,
    fmt: dict[str, Any] | None = None,
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
        fmt:      結構化輸出 JSON schema（限制模型只能吐合法 JSON、綁住輸出長度）。
                  Ollama → ``format``；vLLM → ``extra_body.guided_json``。None＝不限制（同原行為）。
    """
    is_ollama = llm_config.LLM_BACKEND != "vllm"
    if not is_ollama:
        url, payload, parse = _build_vllm(prompt, system, options, model, fmt)
    else:
        url, payload, parse = _build_ollama(prompt, system, options, think, model, fmt)

    @time_counter
    def _post(u: str, p: dict[str, Any]) -> requests.Response:
        return requests.post(u, json=p, timeout=timeout)

    resp = _post(url, payload, file_name=label)

    # think 容錯：模型不支援 thinking → Ollama 回 400「does not support thinking」。
    # 拿掉 think 重試一次，並記住該模型，之後不再帶 think。
    if (is_ollama and resp.status_code == 400 and "think" in payload
            and "does not support thinking" in resp.text):
        _NO_THINK_MODELS.add(payload.get("model", ""))
        payload.pop("think", None)
        resp = _post(url, payload, file_name=label)

    if resp.status_code >= 400:
        # 把後端的錯誤內文帶出來（原本 raise_for_status 會吞掉，難以診斷）
        raise RuntimeError(
            f"LLM 後端回 {resp.status_code}：{resp.text[:500]}"
            f"（url={url}, model={payload.get('model')}）"
        )
    return parse(resp.json())


# ---------------------------------------------------------------------------
# Ollama 原生 /api/generate（保留原行為）
# ---------------------------------------------------------------------------
def _build_ollama(prompt, system, options, think, model, fmt=None):
    url = f"{llm_config.OLLAMA_URL}{llm_config.OLLAMA_MODE}"
    model_name = model or llm_config.OLLAMA_MODEL
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system
    # 已知不支援 thinking 的模型就不帶 think（避免每次都先 400 一次）
    if think and model_name not in _NO_THINK_MODELS:
        payload["think"] = think
    if fmt:  # 結構化輸出：Ollama 用 format=<json schema> 做受限解碼
        payload["format"] = fmt
    # options：合併使用者 options + runtime num_ctx（讓 ollama 真的吃選定的 context，否則預設 ~2k 截斷）
    if options or llm_config.OLLAMA_NUM_CTX:
        opts = dict(options or {})
        if llm_config.OLLAMA_NUM_CTX:
            opts.setdefault("num_ctx", llm_config.OLLAMA_NUM_CTX)
        payload["options"] = opts

    def parse(body: dict[str, Any]) -> str:
        return body["response"]

    return url, payload, parse


# ---------------------------------------------------------------------------
# vLLM / OpenAI 相容 /v1/chat/completions
# ---------------------------------------------------------------------------
def _build_vllm(prompt, system, options, model, fmt=None):
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
    if fmt:  # 結構化輸出：vLLM 用 guided_json 做受限解碼
        payload.setdefault("extra_body", {})["guided_json"] = fmt

    def parse(body: dict[str, Any]) -> str:
        return body["choices"][0]["message"]["content"]

    return url, payload, parse
