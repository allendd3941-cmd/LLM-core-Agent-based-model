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