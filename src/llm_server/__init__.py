"""llm_server — GAMA/模擬器共用的 LLM 決策伺服器（FastAPI + Ollama pipeline）。

原本散落在專案根目錄的 LLM pipeline（server / agent_profile / perception /
decision_making / RAG / od_converter / output_engine / timer / llm_config）已整理進此套件。
對外行為不變：FastAPI app 仍提供 ``/from-gama`` 端點。

啟動方式（需先讓 src/ 在 import path 上，或 pip install -e .）：

    uvicorn llm_server.server:app --host 127.0.0.1 --port 8000 --reload
"""

from __future__ import annotations
