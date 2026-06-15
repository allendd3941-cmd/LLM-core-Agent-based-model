"""llm_config.py — LLM「連線/基礎設施」設定的 typed 存取器。

本專案的設定刻意分成各司其職的三處（非重複，勿合併）：

  - ``.env``（+ ``.env.example`` 範本）：**連線/基礎設施**——Ollama/vLLM 的 URL、後端選擇、
    endpoint 模型名。隨「跑在哪台機器」而變、可能含密鑰，故 gitignore、不進 repo。
  - ``llm_config.py``（本檔）：只是把上面的環境變數**讀成 typed Python 常數**（含合理預設），
    供 ``llm_client.py`` 與各 pipeline 共用。不是另一套設定，是 ``.env`` 的讀取層。
  - ``config/simulation.toml``：**模擬模型參數**（agent/感知/active_mode/號誌…），committed、可重現。

原則：``.env`` 管「連到哪、怎麼連」；``.toml`` 管「模擬做什麼」。兩者不重疊。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# 缺 .env 時退回對「本機 Ollama」的合理預設，避免組出 "NoneNone" 這種無效 URL。
# 仍可用 .env 覆寫（連到別台機器/GPU 或換 endpoint）。
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_MODE = os.getenv("OLLAMA_MODE", "/api/generate")

# LLM 後端切換（見 llm_client.py / docs/SCALING_zh-TW.md）。
#   "ollama"（預設）：原生 /api/generate，行為與原本一致。
#   "vllm"          ：OpenAI 相容 /v1/chat/completions，高並行（continuous batching）。
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")          # "ollama" | "vllm"
VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:8001")  # vLLM server（vllm serve --port 8001）
VLLM_MODEL = os.getenv("VLLM_MODEL", "")                   # vLLM 用的 HF 模型名

# --- runtime 可變的「目前選用」狀態（前端可即時覆寫；上面的 .env 值為啟動預設）---
# 整套 LLM 用途（agent_profile / decision_making）都讀這裡的目前模型。
OLLAMA_NUM_CTX: int | None = None   # ollama 呼叫帶的 context 長度（None＝用 ollama 預設）


def set_runtime_llm(backend: str | None = None, model: str | None = None,
                    num_ctx: int | None = None) -> None:
    """前端選模型時呼叫：即時切換後端/模型/context（llm_client 在呼叫時讀這些值）。"""
    global LLM_BACKEND, OLLAMA_MODEL, VLLM_MODEL, OLLAMA_NUM_CTX
    if backend in ("ollama", "vllm"):
        LLM_BACKEND = backend
    if model:
        if LLM_BACKEND == "vllm":
            VLLM_MODEL = model
        else:
            OLLAMA_MODEL = model
    if num_ctx is not None:
        OLLAMA_NUM_CTX = num_ctx


def current_model() -> str:
    """目前後端實際使用的模型名（整套 LLM 用途共用）。"""
    return VLLM_MODEL if LLM_BACKEND == "vllm" else OLLAMA_MODEL


def ollama_base_url() -> str:
    """從 OLLAMA_URL 推出 scheme://host（給 /api/tags 等列模型用）。"""
    from urllib.parse import urlsplit
    p = urlsplit(OLLAMA_URL)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return "http://127.0.0.1:11434"