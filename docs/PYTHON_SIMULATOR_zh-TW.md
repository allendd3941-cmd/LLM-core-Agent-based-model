# Python-Native 交通 ABM 模擬器（取代 GAMA）

本模組以純 Python 完整取代原專案中由 **GAMA** 承擔的交通 Agent-Based Model 模擬責任，
並提供一個 localhost 互動式 web demo（Leaflet 地圖 + Chart.js 圖表 + WebSocket 即時更新），
用於 **ACM SIGSPATIAL Demo** 展示。既有的 LLM pipeline（`server.py` 的 `/from-gama`）
與所有 prompt/schema **完全不變**，新模擬器以 adapter 方式呼叫它。

- 套件原始碼：`src/llm_abm_simulator/`
- 前端：`simulation_web/frontend/`
- 路網 bundle：`data/tainan_roads.graphml`（真實 OSM 道路，已 commit，離線可重現）
- 執行輸出：`output/agent_memory.csv`、`output/road_flow.csv`（已被 `.gitignore` 忽略）

---

## 0. 參數設定（唯一真實來源：`config/simulation.toml`）

所有「你會想自行調」的參數都集中在 **`config/simulation.toml`**，改完存檔、重啟伺服器即生效。
不需要改任何 `.py`。整個檔缺失或某個 key 缺值時，會自動回退到 `config.py` 內的程式碼預設值。

| TOML 區段 | 內容 |
|---|---|
| `[time]` | `max_steps` / `step_minutes`（模擬時長）|
| `[agents]` | `nb_agents`（預設 agent 數）/ 起訖行政區 |
| `[perception]` | 感知半徑、抵達容差、`crowded_speed_factor`（壅塞降速）、壅塞門檻 |
| `[movement]` | agent 速度（`default_desired_speed_kmh` / `default_speed_car_kmh` / `default_speed_moto_kmh`）與預設路徑權重 |
| `[active_modes.*]` | 五種 active_mode 各自的數值權重與路徑策略（詳見 [`ACTIVE_MODES_zh-TW.md`](ACTIVE_MODES_zh-TW.md)）|
| `[roads]` | 車流→壅塞估計與權重、視覺化門檻 |
| `[llm]` | `use_llm`（LLM 決策一律在進程內直呼 pipeline） |
| `[memory]` | 旅次記憶 STM/LTM 質性門檻（見 `docs/MEMORY_zh-TW.md`） |
| `[perception_context]` | 送 LLM 的環境感知：熱點/前方路況取樣（見 `docs/ENVIRONMENT_zh-TW.md`） |
| `[summary]` | 長期記憶 trip_summary 的 LLM 摘要（`use_llm_summary` / `summary_model` / 頻率；見 `docs/MEMORY_zh-TW.md`）|
| `[profile]` | agent persona 池大小（`pool_size`；見下方「Persona 池」）|
| `[reproducibility]` | `seed`（同 seed → 同軌跡）|
| `[network]` | OSM 下載開關、合成路網大小 |
| `[ui]` | 前端 slider 範圍（速度 / agent 數）；**同時驅動後端 clamp 與前端 slider**，是兩者的單一來源 |
| `[highway_specs.*]` | 各 OSM 路型的速限/車道/容量 |

> **TOML key 名稱與 `config.py` 的 dataclass 欄位一一對應**；要新增參數＝在 dataclass 加欄位、TOML 加同名一行即可。
>
> ⚠ **`[highway_specs]` 例外**：改值只在「重建路網」時生效，因為 bundle 的 `data/tainan_roads.graphml`
> 已把速度/容量烤進每條邊。改完需重建：`python -m llm_abm_simulator.spatial.build_roads`
> （或 `--synthetic`）。`[movement]` 的 agent 速度則是即時生效（重設模擬即可）。

---

## 1. 環境需求

- Python 3.12+
- 已含於 `requirements.txt`：`geopandas / shapely / pyproj / networkx / fastapi /
  uvicorn / websockets`（`osmnx` 僅在「重新下載路網」時需要）。
- 不需要安裝 GAMA。

> **Windows 安裝提示**：`geopandas` 會帶入 GDAL/fiona。若用本 repo 的 `.venv` 已安裝完成，
> 直接使用即可。全新環境請 `pip install -r requirements.txt`。

---

## 2. 安裝

```powershell
# 專案根目錄
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# 可選：以 src layout 安裝，之後不必設 PYTHONPATH
python -m pip install -e .
```

Linux / macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

---

## 3. 啟動 web demo（Mock 模式，預設、無需 LLM）

```powershell
# 若已 pip install -e .
uvicorn llm_abm_simulator.web.app:app --host 127.0.0.1 --port 8080

# 若沒安裝套件，改用 PYTHONPATH
#   Windows PowerShell:
$env:PYTHONPATH = "src"; uvicorn llm_abm_simulator.web.app:app --port 8080
#   Linux/macOS:
#   PYTHONPATH=src uvicorn llm_abm_simulator.web.app:app --port 8080
```

瀏覽器開啟 <http://localhost:8080>，即可看到模擬儀表板：

| 控制 | 說明 |
|---|---|
| ▶ 開始 / ⏸ 暫停 / ⏭ 單步 / 🔄 重設 | 模擬生命週期 |
| 速度滑桿 | 0.5× ~ 5× 播放速度 |
| Agent 數量 | 重設前可調（5~80）|
| Mock / LLM 切換 | 切換決策來源 |

地圖會顯示：臺南市行政區界、主要道路（依壅塞即時上色：綠→黃→橘→紅）、
車輛 agent（汽車較大、機車較小；抵達轉綠）、紅色目的地球場標記。
點任一車輛可在左側「Agent 檢視」查看其狀態。右側圖表即時顯示壅塞趨勢、抵達進度與行為模式分佈。

---

## 4. 啟用 LLM 決策模式（可選，需 Ollama）

LLM 模式會跑既有的 LLM pipeline（`run_agent_profile` → `run_perception` →
`run_decision_making`）取得每個 agent 的起點 / active_mode / 車種。**需要 Ollama 在跑**
（模型見 `.env`）。

LLM 決策**一律在模擬器進程內直接呼叫** `llm_server` 的 pipeline 函式（in-process），
省掉 HTTP round-trip 與一層 JSON 序列化，單機 demo 最乾淨、延遲最低、好除錯。
`decisions/llm_adapter.py` 取得 LLM 原始文字後由 `response_parser` 解析；失敗時自動 fallback 到 Mock。

> 不需另開 `server.py`、也不需額外連接埠。`server.py`（`/from-gama`）仍保留為 GAMA 時代的
> standalone LLM 伺服器，但模擬器不再透過它連線。

### 啟動方式

只需啟動模擬器，毋須另開 LLM 伺服器：

```powershell
# 確保 Ollama 已在跑（模型見 .env），然後：
uvicorn llm_abm_simulator.web.app:app --port 8080
```

### 共通行為

在網頁右上把決策模式切到 **LLM**。

- **LLM 不可用時會自動 fallback 到 Mock，不會 crash**（介面決策來源欄會顯示實際使用的來源）。
  「不可用」包含 `llm_server` 無法匯入、Ollama 連不上、或 pipeline 執行丟錯。
- LLM 每步需呼叫一次推論，可能數秒~數十秒；現場 demo 建議用 Mock，需要展示 LLM 推理時再切換。

### 4c. Persona 池（agent 人物設定）

LLM 模式下，每個 agent 的人物設定（identity / traits）由 `agent_profile` 階段生成。為避免
「每次調 agent 數就重生、覆寫」的問題，採**穩定的 persona 池**（`decisions/profile_pool.py`）：

- **生成一次、存成穩定池檔**（`output/agent_profile_output_1.txt`，正規化 JSON）。
- 調整 agent 數**只是「取池裡前 n 個」**——在記憶體切片，不動池檔、不重生。
- agent 數**超過 `[profile].pool_size` 才自動補生**差額並追加進池（數量永遠對得上）。
- 要**整批換人**：前端按 **「👤 重新生成人物」**（清池 + 重新初始化，下次 LLM init 重生）。
- 池檔與 LLM 生成輸出都用**強韌 JSON 解析**（`llm_server/json_utils.py`）：尾逗號、Python 字面量、
  智慧引號、**陣列被截斷**等壞結構都會盡量救回**已完整的物件**，而不是整批作廢用預設值。

```toml
[profile]
pool_size = 30      # persona 池目標大小（agent 數超過才補生）
```

> 前端 inspect 點選 agent 會顯示其人物背景（年齡/職業/收入/態度/習慣…），即讀自此池，
> 以 `identity.name` 對應 `agent.profile_name`。

---

## 5. 重新下載 / 重建路網（可重現）

本專案沒有 ROADLINK 道路檔，路網改用真實 OSM 道路，已 bundle 成 `data/tainan_roads.graphml`。
要重新產生（例如研究範圍變更）：

```powershell
# 用 OSMnx 依研究範圍重新下載（需安裝 osmnx 與可上網）
python -m llm_abm_simulator.spatial.build_roads

# 或產生確定性合成路網（不連網、不需 osmnx）
python -m llm_abm_simulator.spatial.build_roads --synthetic
```

執行期路網來源採三層 fallback：① 讀 `data/tainan_roads.graphml` →
② 允許時用 OSMnx 即時下載 → ③ 確定性合成網格。

---

## 6. 在 Linux server 上以 SSH 遠端展示

情境：模擬器跑在遠端 Linux server，你在本機瀏覽器看 demo。

### 方式 A：SSH 連接埠轉送（最簡單、最安全，推薦）

伺服器只綁定本機，透過 SSH 通道把遠端 8080 轉到你本機 8080：

```bash
# 遠端 server 上
cd /path/to/LLM_abm_model
source .venv/bin/activate
PYTHONPATH=src uvicorn llm_abm_simulator.web.app:app --host 127.0.0.1 --port 8080
```

```bash
# 你的本機（另一個終端）
ssh -N -L 8080:localhost:8080 user@your-server
```

本機瀏覽器開 <http://localhost:8080> 即可。關閉 SSH 通道即停止對外存取，無需開防火牆。

> 長時間執行可用 `tmux` / `nohup` 讓伺服器在背景持續：
> `tmux new -s abm` → 啟動 uvicorn → `Ctrl-b d` 脫離。

### 方式 B：直接對外綁定（需自行管理防火牆）

```bash
# 遠端 server
PYTHONPATH=src uvicorn llm_abm_simulator.web.app:app --host 0.0.0.0 --port 8080
```

本機開 `http://<server-ip>:8080`。**注意**：`0.0.0.0` 會對外開放，請確認防火牆/資安政策
（例如僅允許特定來源 IP），demo 結束後關閉。

### LLM 模式於遠端

模擬器進程內直接跑 pipeline（in-process），遠端只要確保該機可連到 Ollama 即可，
毋須另開 `server.py`、也毋須額外連接埠轉送。

---

## 7. 測試

```powershell
pytest tests/simulator -q
```

涵蓋：response 解析（含 GAML 全部 key 變體與真實 LLM 輸出）、路徑規劃、壅塞/分佈指標、
引擎生命週期、**determinism（同 seed 兩次跑出完全相同軌跡）**、CSV 輸出欄位、GeoJSON 結構。

---

## 8. 架構

```text
config/simulation.toml     使用者可編輯的參數檔（唯一真實來源；見第 0 節）
src/llm_abm_simulator/
  config.py            參數的型別化 schema + tomllib 載入器（程式碼預設值＝fallback）
  domain/              純資料模型：agent / road / town / state / events
  spatial/             gis_loader / road_network / routing / geojson（geopandas+networkx）
  decisions/           base / mock_policy / llm_adapter / response_parser
  simulation/          engine / scheduler / metrics / scenario / random_seed
  web/                 app(FastAPI) / websocket / schemas（薄層，不含模擬邏輯）
simulation_web/frontend/   index.html / index.css / app.js / map.js / charts.js / simulation.js
data/tainan_roads.graphml  bundle 的真實 OSM 路網
tests/simulator/           pytest
```

資料流：

```
前端(瀏覽器) ──WebSocket──► web 層 ──► SimulationEngine（擁有狀態）
                                          │
                                          ├─ spatial：GIS / 路網 / 路徑
                                          └─ decisions：Mock 規則
                                                         └─或─ LLMAdapter ──► 直接呼叫 llm_server pipeline ──► Ollama
```

> `LLMAdapter` 在模擬器進程內直接呼叫 `llm_server` pipeline（in-process），
> 回傳的 LLM 原始文字由 `response_parser` 解析、失敗時 fallback 到 Mock。

---

## 9. GAMA 對應（parity）

| GAMA 能力 | Python 對應 |
|---|---|
| `init` / cycle / `max_steps=36` / `step_minutes=5` | `simulation/engine.py` + `scheduler.py` |
| start/pause/resume/reset/single-step/stop@max | `SimulationEngine` 控制方法 + web 層 |
| deterministic seed | `simulation/random_seed.py`（同 seed → 同軌跡）|
| 載入 TOWN_MOI 篩臺南市 + 球場 point + CRS | `spatial/gis_loader.py`（EPSG:3826 距離 / 4326 前端）|
| ROADLINK 路網（本專案無此檔）| `spatial/road_network.py`：OSM bundle / 合成 fallback |
| `as_edge_graph` / `path_between` / `with_weights` 動態權重 / crowded recompute | `spatial/routing.py` + 引擎 |
| road 欄位（speed_car/moto, lanes, capacity, flow, congestion_proxy, weight…）| `domain/road.py` |
| vehicle 欄位與 active_mode 偏好、旅次記憶（STM/LTM，見 `docs/MEMORY_zh-TW.md`）、route_status… | `domain/agent.py` |
| perceive（速限/missing cap/crowded factor/感知半徑/鄰近數/距離/抵達/重算）| `simulation/engine.py` |
| congestion 指標、mode/status 分佈、per-cycle | `simulation/metrics.py` |
| init/step payload 契約（in-process 直呼 llm_server pipeline）、LLM fallback | `decisions/llm_adapter.py` + `response_parser.py` |
| 輸出 `agent_memory.csv` / `road_flow.csv`（欄位對齊）| `simulation/metrics.py` → `output/` |
| GUI display（地圖/道路/車輛/球場/控制/圖表/檢視）| `simulation_web/frontend/` |

---

## 10. 已知差異與限制

- GAML 以「附近有車（nearby>0）」觸發 recompute_path；本實作改以「道路 congestion_proxy ≥
  門檻」觸發，語意更貼近「避開壅塞」並大幅減少不必要的最短路徑重算，使互動 demo 流暢。
- 球場 point 與最近路網節點之間有固定偏移，抵達判定以「到達目的地節點」為準。
- 前端底圖預設只畫主要道路（約 7 千條）以維持效能；完整路網（約 2.8 萬條邊）仍用於路徑規劃。
- LLM 回應品質取決於模型/prompt/溫度；解析器已盡量容錯（含空格/中文 key、markdown 圍欄、雜訊）。
