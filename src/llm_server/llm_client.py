"""llm_client.py — 統一的 LLM 呼叫 adapter（vLLM）。

走 OpenAI 相容 ``/v1/chat/completions``（vLLM continuous batching 高並行）：
system→system message、prompt→user message，options 映射成 OpenAI 參數，結構化輸出走 guided_json。

所有 prompt 內容不變；本檔只集中「怎麼把 prompt 送出去、怎麼把回應取回來」這層 transport，
讓 perception / decision_making / agent_profile / rag / sim_* 共用同一個入口。
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
    fmt: dict[str, Any] | None = None,
) -> str:
    """送一段 prompt 給 vLLM，回傳純文字回應。

    Args:
        prompt:   使用者 prompt（單一字串）。
        system:   system prompt（可空）。
        options:  Ollama 風格 options（seed / temperature / top_k / num_predict…）→ 映射成 OpenAI 參數。
        think:    保留以相容呼叫端；vLLM 無 reasoning flag → **忽略**。
        model:    覆寫模型名（預設用 ``llm_config.VLLM_MODEL``）。
        label:    計時 log 用標籤。
        timeout:  HTTP 逾時秒數（None＝不設限）。
        fmt:      結構化輸出 JSON schema → vLLM ``extra_body.guided_json``（受限解碼）。None＝不限制。
    """
    url = f"{llm_config.VLLM_URL.rstrip('/')}/v1/chat/completions"
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "model": model or llm_config.VLLM_MODEL,
        "messages": messages,
        "stream": False,
    }
    # Ollama 風格 options → OpenAI 參數映射（呼叫端介面不變）
    if options:
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "seed" in options:
            payload["seed"] = options["seed"]
        if "num_predict" in options:           # 輸出長度上限
            payload["max_tokens"] = options["num_predict"]
        if "top_k" in options:                 # vLLM 擴充參數（非 OpenAI 標準）
            payload.setdefault("extra_body", {})["top_k"] = options["top_k"]
    if fmt:  # 結構化輸出：vLLM 用 guided_json 做受限解碼
        payload.setdefault("extra_body", {})["guided_json"] = fmt

    @time_counter
    def _post(u: str, p: dict[str, Any]) -> requests.Response:
        return requests.post(u, json=p, timeout=timeout)

    resp = _post(url, payload, file_name=label)
    if resp.status_code >= 400:
        # 把後端錯誤內文帶出來（raise_for_status 會吞掉，難診斷）
        raise RuntimeError(
            f"vLLM 後端回 {resp.status_code}：{resp.text[:500]}"
            f"（url={url}, model={payload.get('model')}）"
        )
    return resp.json()["choices"][0]["message"]["content"]
