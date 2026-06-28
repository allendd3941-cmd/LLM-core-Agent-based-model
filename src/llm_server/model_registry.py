"""model_registry.py — vLLM 可選模型登錄表（非 gated、可直接串、適合本專案）。

每個項目：id（HF 模型名）/ label（前端顯示）/ max_context（模型架構 context 天花板）/
params / note。選模型時 max_model_len = min(8192, max_context)，並對齊你 `vllm serve --max-model-len`。
已排除 gated（Mistral-7B-v0.3）與非 Apache（Qwen2.5-3B＝Research 授權）。
"""

from __future__ import annotations

VLLM_MODELS: list[dict] = [
    {"id": "Qwen/Qwen2.5-1.5B-Instruct", "label": "Qwen2.5-1.5B-Instruct",
     "max_context": 32768, "params": "1.5B", "note": "fastest, high concurrency"},
    {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B-Instruct (recommended)",
     "max_context": 32768, "params": "7B", "note": "best Chinese + JSON balance"},
    {"id": "Qwen/Qwen2.5-14B-Instruct", "label": "Qwen2.5-14B-Instruct",
     "max_context": 32768, "params": "14B", "note": "higher quality, needs a larger GPU"},
    {"id": "microsoft/Phi-3.5-mini-instruct", "label": "Phi-3.5-mini-instruct",
     "max_context": 131072, "params": "3.8B", "note": "long context, lightweight"},
    {"id": "internlm/internlm2_5-7b-chat", "label": "internlm2.5-7b-chat",
     "max_context": 32768, "params": "7B", "note": "strong-Chinese 7B alternative"},
]

# 專案夠用的 context 上限（不超過模型天花板）：避免 vLLM 開太大吃 VRAM、減少並行。
PROJECT_CONTEXT_CAP = 8192


def max_context_for(model_id: str) -> int:
    for m in VLLM_MODELS:
        if m["id"] == model_id:
            return int(m["max_context"])
    return PROJECT_CONTEXT_CAP


def suggested_max_model_len(model_id: str) -> int:
    return min(PROJECT_CONTEXT_CAP, max_context_for(model_id))
