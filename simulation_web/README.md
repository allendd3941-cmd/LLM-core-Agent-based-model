# simulation_web — 交通 ABM 模擬器前端

本資料夾現在**只保留前端**（`frontend/`）。原本的 `backend/` 已重構為一個專業化、
可測試的 Python 套件：[`src/llm_abm_simulator/`](../src/llm_abm_simulator)。

完整說明（安裝、啟動、Mock/LLM 切換、Linux SSH 遠端展示、架構、測試）請見：

➡ **[`docs/PYTHON_SIMULATOR_zh-TW.md`](../docs/PYTHON_SIMULATOR_zh-TW.md)**

## 快速啟動（Mock 模式）

```powershell
# 專案根目錄
python -m pip install -r requirements.txt
uvicorn llm_abm_simulator.web.app:app --host 127.0.0.1 --port 8080
# 若未 pip install -e .，改用： $env:PYTHONPATH="src"; uvicorn llm_abm_simulator.web.app:app --port 8080
```

瀏覽器開 <http://localhost:8080>。

## 前端檔案

| 檔案 | 說明 |
|---|---|
| `frontend/index.html` | 版面：控制面板 / 地圖 / 圖表 |
| `frontend/index.css`  | 深色設計系統 |
| `frontend/app.js`     | WebSocket 連線管理與訊息分派 |
| `frontend/map.js`     | Leaflet 地圖（行政區界、道路上色、車輛、球場）|
| `frontend/charts.js`  | Chart.js 即時圖表 |
| `frontend/simulation.js` | 控制面板 UI、狀態顯示、agent 檢視 |

前端透過 `ws://<host>/ws` 與後端溝通，協定見 `src/llm_abm_simulator/web/schemas.py`。
