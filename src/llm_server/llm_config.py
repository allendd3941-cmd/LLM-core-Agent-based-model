"""llm_config.py — LLM「連線/基礎設施」設定的 typed 存取器（vLLM）。

本專案的設定刻意分成各司其職的三處（非重複，勿合併）：

  - ``.env``（+ ``.env.example`` 範本）：**連線/基礎設施**——vLLM 的 URL 與 endpoint 模型名。
    隨「跑在哪台機器/GPU」而變，故 gitignore、不進 repo。
  - ``llm_config.py``（本檔）：把上面的環境變數**讀成 typed Python 常數**（含合理預設），
    供 ``llm_client.py`` 與各 pipeline 共用。是 ``.env`` 的讀取層，不是另一套設定。
  - ``config/simulation.toml``：**模擬模型參數**（agent/感知/action_mode/號誌…），committed、可重現。

原則：``.env`` 管「連到哪、怎麼連」；``.toml`` 管「模擬做什麼」。兩者不重疊。
LLM 後端統一為 **vLLM**（OpenAI 相容 ``/v1/chat/completions``、continuous batching 高並行）。
"""

import os

from dotenv import load_dotenv

load_dotenv()

# vLLM server（在 Linux/GPU 上 `vllm serve <HF模型> --port 8001`）。
# ⚠️ VLLM_MODEL 必填（無預設）：未設則 LLM 呼叫會用空模型名失敗，請於 .env 指定。
VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:8001")
VLLM_MODEL = os.getenv("VLLM_MODEL", "")


def set_runtime_llm(model: str | None = None) -> None:
    """前端選模型時呼叫：即時切換「目前對齊的 vLLM 模型名」（llm_client 呼叫時讀此值）。

    注意：vLLM 一機一模型、無法熱切換真正的 server；此處只改 client 對齊的模型名，
    實際仍要 ``vllm serve`` 對應模型（前端模型下拉＝「對齊目標」）。
    """
    global VLLM_MODEL
    if model:
        VLLM_MODEL = model


def current_model() -> str:
    """目前使用的 vLLM 模型名（整套 LLM 用途共用）。"""
    return VLLM_MODEL
