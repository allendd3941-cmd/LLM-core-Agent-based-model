"""llm_abm_simulator — Python-native 交通 ABM 模擬器。

本套件以 Python 完整取代原專案中由 GAMA 承擔的交通 Agent-Based Model 模擬責任，
並提供一個薄的 FastAPI / WebSocket web 層做為 localhost demo。

分層架構：

- ``domain``     — 純資料模型（agent / road / town / state / events），無任何 I/O。
- ``spatial``    — GIS 載入、路網建構、路徑規劃、GeoJSON 輸出（依賴 geopandas / networkx）。
- ``decisions``  — 決策來源（mock 規則 / 既有 LLM ``/from-gama`` adapter）與回應解析。
- ``simulation`` — 模擬引擎、排程、指標、情境設定、亂數種子。
- ``web``        — FastAPI app、WebSocket handler、請求/回應 schema（薄層，不含模擬邏輯）。

原則：模擬狀態由本套件擁有；LLM 整合是 adapter，不是核心；同一個 seed 必可重現。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
