# 系統設計完整總整理（paper 撰寫用｜由大到小，與程式碼一致）

> 目標：ACM SIGSPATIAL 2026 demo paper。本檔把**整個系統從定位到每一個細部機制**完整攤開，
> 每節附**確切公式 / 參數 / 對應程式碼**，可直接對應到論文的 System / Method / Demonstration 章節。
> 內容已逐項對照原始碼核對（2026-06）。高階入口見 [`OVERVIEW_zh-TW.md`](OVERVIEW_zh-TW.md)；
> 本檔為其「可寫進 paper 的細節展開版」。所有可調參數真實來源：`config/simulation.toml` ↔ `config.py`。

---

## 0. 一句話定位與題目
**LLM-Driven Mobility Digital Twin for Event-Based Urban Traffic Simulation [Demo]**。
在**真實 OSM 路網 + 行政區人口**上，用 **LLM 認知核心**驅動「事件參與者（去球場的車）」的微觀交通決策，
疊加**輕量規則核心**模擬的**常態背景車流**，做成**可即時互動、可規模化、可重現**的事件交通數位分身，
並輸出**交通局視角的兩層交通評估**。場景：台南亞太棒球場賽事進出場尖峰。

---

## 1. 系統分層架構（最上層）

兩個 Python 套件，**in-process 直呼、無 GAMA、無 HTTP hop**（原型曾是 GAMA+FastAPI，已全面取代）：

| 套件 | 角色 | 主要模組 |
|---|---|---|
| `llm_abm_simulator` | 模擬器主體（擁有全部狀態與物理） | `domain`（agent/road/town/state/events）、`spatial`（road_network/routing/gis_loader/geojson/signals/build_*）、`mobility`（demand 重力）、`decisions`（registry/base/mock_policy/llm_adapter/response_parser/profile_pool）、`simulation`（engine/scheduler/metrics/random_seed）、`web`（FastAPI app + WebSocket session）、`config.py`、`scenarios.py`、`calibrate.py` |
| `llm_server` | LLM pipeline（被模擬器在本進程直接呼叫） | `agent_profile`（人格生成）、`perception`（**確定性模板、不呼叫 LLM**）、`decision_making`（決策，唯一每批 LLM 呼叫，結構化輸出）、`llm_client`（Ollama/vLLM 統一入口）、`llm_config`/`model_registry`、`rag_store`（通用 TF-IDF 檢索引擎 + RRF）、`rag_query`（領域查詢建構：路況/任務/人格子查詢）、`sim_chat`/`sim_intervene`、`json_utils`（強韌解析）、`prompt_store` |

前端：`simulation_web/frontend/`（`index.html`/`index.css`/`map.js`/`charts.js`/`simulation.js`/`app.js`，Leaflet + Chart.js + WebSocket）。

**每步資料流**（`engine.step()`，對應 `ARCHITECTURE.md` mermaid）：
感知（確定性、無 LLM）→ 決策（規則式 *或* LLM；背景車一律規則式）→ 移動（沿加權最短路；壅塞時重算；背景車抵達重生）
→ 重算道路 flow/congestion/weight（含背景車）→ 指標（事件 KPI + 路網層）→ 記憶 → 快照推前端。

---

## 2. 研究範圍與資料（study area & data）

| 項目 | 內容 | 來源 / 檔案 | 誠實標註 |
|---|---|---|---|
| 行政區 | 臺南市 37 區（界線 + 形心 + 人口 join） | `data/gis/TOWN_MOI_*.shp` | — |
| 目的地 | 亞太棒球場 point（可換場景） | `data/gis/亞太棒球場_point.shp` | 球場 point 與最近路網節點有固定偏移，抵達以「到達目的地節點」為準 |
| 路網 | 真實 OSM 道路，**涵蓋全台南市 37 區**（依 TOWN_MOI 縣界 union 下載） | `data/tainan_roads.graphml`（**gitignore；首次啟動 osmnx 依縣界自動下載建檔**，之後讀檔） | 前端底圖只畫主要道路保效能；路徑規劃用全網。需 osmnx+網路建檔 |
| 速限/車道/容量 | 建網優先用 OSM `maxspeed`/`lanes`（缺值回退 `[highway_specs]` 層級估計）；容量＝`lanes × capacity_per_lane`（載入時算，調參免重建） | `config/simulation.toml` `[highway_specs]` | **OSM maxspeed/lanes 覆蓋率部分**，缺值用層級估計；`capacity_per_lane` 為 demo 可見性的代理值（**非 HCM 校準**）；`lanes` 只影響容量、地圖以中心線呈現（非車道級） |
| 人口 | 各區人口（重力需求） | `data/gis/town_population.csv` | **近似值（~2023 量級）；正式 paper 換內政部戶政司/臺南市民政局月報** |
| 號誌 | 號誌節點 + 相位軸 + offset | `data/tainan_signals.json`（由 `build_signals.py`） | **台南只有點位、無真實時相秒數**（時相僅臺北/澎湖且 ID 無法 join）→ cycle/yellow 為**合成值**，非真實號誌孿生 |
| CRS | 距離/空間運算 EPSG:3826（公尺）；前端 EPSG:4326 | `config.CRS_METRIC/CRS_WGS84` | — |

路網來源三層 fallback：① 讀 bundle graphml → ② 允許時 OSMnx 即時下載 → ③ 確定性合成網格（`spatial/build_roads.py`）。

---

## 3. 空間基底：路網、路徑規劃、壅塞模型（細部，含公式）

### 3.1 道路模型（`domain/road.py`）
一條 `Road` = 圖上一條有向邊。每步動態更新（`update_flow`）：
```
congestion_proxy = min(1, current_flow / capacity)        # capacity≤0 用 fallback(10)
weight           = max(length, 1) × (1 + current_flow × flow_weight_multiplier)   # flow_weight_multiplier=2.0
```
速限 `speed_limit_for(vehicle)`：汽車用 `speed_car`、機車用 `speed_moto`（依 OSM highway type 由 `[highway_specs]` 推估，烤入 graphml）。

### 3.2 加權最短路徑（`spatial/routing.py`，Dijkstra）
`find_path` 用 `networkx.shortest_path` + 自訂邊成本函式。每條邊基礎成本：
```
edge_cost = length × (
      w_time     · (length / speed_car) / 100      # 旅行時間（慢=貴）
    + w_distance · 1                               # 距離
    + w_comfort  · (1 + congestion × 1.5)          # 壅塞降舒適
    + w_capacity · (1 + congestion × 2.0)          # 壅塞=貴
)
```
再依 `active_mode` 的**路徑策略旗標**疊加（預設全關＝退化為純最短路）：
```
if congestion_penalty: cost ×= (1 + congestion_penalty × congestion)     # avoid 用
if congestion > avoid_threshold: cost ×= 25                              # 硬避開（近乎封路）
if road_class_bias>0: 幹道 cost ×=(1−bias)、小路 cost ×=(1+bias)          # comfortable 偏好大路
if randomness: cost ×= jitter∈[1−r,1+r]                                  # 分散車流（見 3.4）
if avoid_circles: 落在避讓圓內的邊 cost ×= 25                            # NL 介入
```
`w_time/w_distance/w_comfort/w_capacity` 為 agent 當前 active_mode 的四權重。

### 3.3 壅塞觸發重算（reactive rerouting）
`engine._move_agent`：當 `is_crowded`（`congestion_proxy ≥ crowded_road_threshold=0.5`）且該 mode `recompute_on_crowded=True`
→ 從目前位置對目的地重算路徑。速度：`speed = base × (crowded_speed_factor=0.55 if is_crowded else 1)`，
`base` 受道路速限上限（缺速限路段用 `missing_road_speed_cap_kmh=40`）。

### 3.4 可重現的車流分散（`_edge_jitter`）
微擾用穩定 hash：`frac = crc32("u|v|seed|salt") % 10000 / 10000`，`jitter = 1 + (2·frac−1)·randomness`。
**非 live RNG** → 同 seed 同軌跡；salt=`agent_id` 讓不同車即使同 mode 也走散。

### 3.5 移動與抵達（`_advance_along_path`）
沿節點路徑前進 `speed_kmh × (step_minutes/60) × 1000` 公尺，支援跨步推進長邊（`edge_progress` 線性內插）。
抵達判定：`path_index 到終點` **或** 直線距離 `< arrival_distance_threshold_m=50`。

---

## 4. 事件需求生成：重力模型（`mobility/demand.py`，含確切公式）

把「**人是誰**（persona）」與「**人從哪來**（出生地）」**解耦**。出生地由生產約束式重力抽樣決定，**不**由 persona 的 residential_location 決定。

### 4.1 事件車出生地（單一目的地＝球場）
```
weight_i = population_i × f(d_i)
  f(d) = exp(−β · d_km)        (decay="exp"，預設)
       = d_km^(−β)             (decay="power")
  d_km = max( dist(形心_i, 球場)/1000 , min_distance_km=0.5 )    # EPSG:3826 歐氏
  P(origin = i) = weight_i / Σ_k weight_k
```
每台事件車**獨立**用引擎 seeded RNG 二分搜尋抽一區（`assign_origin_towns`）→ 各區數量 ≈ P×總數，總數恰為 `nb_agents`。
人口=0 的區不參與。`enabled=False` 或無人口 → 回退既有指派。參數 `[demand] beta=0.08, decay="exp"`。

### 4.2 背景車 OD（雙端，鄉鎮對｜`sample_od_pairs`）
```
P(origin = i) ∝ population_i                          # trip generation
P(dest = j | i) ∝ population_j × f(d_ij)  , j≠i       # trip distribution（gravity）
⇒ 聯合 P(i,j) ∝ population_i · population_j · f(d_ij)  # 無約束重力（unconstrained）
```
**誠實措辭**：這是**無約束重力的抽樣形式**，非嚴格 doubly-constrained（Wilson，無迭代平衡因子 A_i/B_j）。
背景車抵達後以目前位置 + `sample_dest_town`（∝人口×距離衰減）抽新終點重生 → 穩態。

### 4.3 期望分布（分析用）
`expected_distribution` = 各區 `weight_i / Σweight` 降冪 top-k → 分析面板「出發地實際 vs 重力期望」對照。

---

## 5. Agent 與五種行為模式（`domain/agent.py`、`[active_modes.*]`）

`VehicleAgent`：identity（`agent_id`/`profile_name`/`role`）、起訖、`active_mode` + 四權重 + 路徑策略旗標、
路網位置（公尺座標 + 節點路徑 + `edge_progress`）、感知狀態、單一 `memory`、事件觸發內部狀態。
`role`：`event`（去球場，可用 LLM 核心）/ `ambient`（背景，一律規則式、無記憶）。

mock/LLM **只回 mode 名字字串**；`apply_active_mode` 查 `ACTIVE_MODE_PROFILES` 套入數值 + 策略旗標。五種：

| mode | desired | time | dist | comfort | cap | penalty | avoid_thr | class_bias | recompute | randomness | 一句話走法 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `fast` | 55 | 0.70 | 0.20 | 0.05 | 0.05 | 0 | — | — | ✓ | 0.05 | 最短時間、無視壅塞、塞了重算 |
| `tolerate_congestion` | 45 | 0.55 | 0.30 | 0.10 | 0.05 | 0 | — | — | ✗ | 0.05 | 時間優先但不繞路、走到底 |
| `avoid_congestion` | 38 | 0.20 | 0.10 | 0.25 | 0.45 | 3.0 | 0.6 | — | ✓ | 0.20 | 避塞：重罰+硬避開+積極重算+高分散 |
| `comfortable` | 42 | 0.20 | 0.10 | 0.45 | 0.25 | 1.0 | — | 0.4 | ✓ | 0.10 | 偏好大路、中度避塞 |
| `short_distance` | 35 | 0.10 | 0.70 | 0.10 | 0.10 | 0 | — | — | ✓ | 0.10 | 純距離最短、願鑽小路 |

> 數值為 demo 取向經驗值、非實證標定（誠實限制）。差異需模擬跑出壅塞後才明顯。

---

## 6. 送 LLM 的環境感知（`ENVIRONMENT_zh-TW.md`；`engine._refresh_env_labels`/`_environment_summary`、`agent.build_environment_payload`）

**全域（每步只送一份，不乘 N）**：`overall_traffic`（順暢/普通/壅塞，由平均 proxy 套門檻）、
`congestion_trend`（與上一步比，Δ>+0.02 惡化/<−0.02 改善/否則持平）、`congestion_hotspots`（top-K=5 行政區，
由 agent 所在區+路況聚合，O(agent) 不掃全網）。**裸統計（車數/活躍路數…）不進 LLM、只給前端**。

**每車局部（純質性）**：`current_road`（路名+等級）、`traffic_here`（順暢/普通/壅塞）、`speed_status`
（自由流/略慢/壅塞緩行，由 speed÷速限）、`road_ahead`（沿路徑往前看 `lookahead_distance_m=2000`，
回第一個壅塞段「前方約 X 公里後壅塞（路名）」）。全為確定性規則運算（不呼叫 LLM、可重現）。
> 重要：環境只影響「LLM 選哪個 mode」；真正改道仍由「踩到壅塞才重算」觸發（road_ahead 目前不直接觸發預先繞路）。

---

## 7. 單一旅次記憶（`MEMORY_zh-TW.md`；`agent.update_memory`/`_compose_summary`）

**不分長短期**（1 step=1 分鐘，區分無意義）→ 單一 `memory`：running 的自然語言 `summary` + 當下印象
（where/traffic_feel/moved/getting_closer/remaining…）+ 整趟聚合量（congested_spots/mode_switches/overall_smoothness/elapsed）。
量化→質性門檻在 `[memory]`。每步**覆蓋重算**（非堆疊；歷史壓進有界累積器）。
- `summary` **一律由 `_compose_summary` 模板確定性每步重算**（規則式與 LLM 核心皆然，零 token、可重現）。
- **已移除 LLM 摘要重寫**：原本在重決策時用小模型重寫 summary，因只改寫既有事實、邊際價值低、又多一次 LLM 呼叫而移除 → memory 全走模板、LLM 只做決策。（未來若要記憶用 LLM，方向是「跨旅次經驗影響決策」，非摘要。）
- 背景車**不存記憶**（`memory=={}`）。摘要不回饋物理 → 同 seed 同軌跡仍成立。

---

## 8. 決策核心：規則式 vs LLM（`decisions/`，paper 主對照）

可在前端切換（`registry.py` 具名核心；`engine.last_decision_source` 下發顯示）。**背景車永遠規則式**。

### 8.1 規則式核心（`mock_policy.py`，baseline）
確定性 threshold 規則選 mode：`cong>0.7→avoid`；`>0.4→comfortable`；`dist<2km→short_distance`；
汽車→`fast`；機車→`tolerate_congestion`。零 LLM 成本、即時、可重現 → demo 預設 + LLM fallback + 論文 baseline。

### 8.2 LLM 認知核心（`llm_adapter.py` → `llm_server` pipeline）
**in-process 管線**：`profile_pool`（persona）→ `perception`（確定性模板，**不呼叫 LLM**）→
`decision_making`（**唯一**每批 LLM 呼叫，結構化輸出）→ `response_parser`（強韌 JSON→rows）→ 依 `agent_id` 套用。
任一失敗（匯入/Ollama 掛/解析不到）→ **fallback 規則式**、不崩，前端顯示實際來源。

### 8.3 事件觸發 + 並行批次（規模化關鍵；`SCALING_zh-TW.md`）
- **事件觸發**（預設）：LLM 只在「踩到壅塞 *或* 前方塞」的上升緣 + 過 `cooldown_steps=5` 時重決
  （`_triggered_agents`：`signal = cong≥0.5 OR road_ahead≠clear`）。順暢的車整趟維持規則式初始 mode。
  → **LLM 成本 ∝ 決策事件數，而非 agent×步數**。開場用 persona 池**確定性**指派（不對全車 init 決策）。
- **token 預算動態切批**（`_budget_batch_size`）：
  `avail = effective_max_model_len − reserve_output − prompt_overhead`；
  `per_agent_tok = (persona_chars + status_chars)/chars_per_token`（取樣前 5 台實測字元）；
  `batch = min([scaling].batch_size, avail // per_agent_tok)` → 保證 prompt 不超過 `max_model_len`。
- **並行**（scatter–gather）：觸發車分批，`ThreadPoolExecutor`（`concurrency=4`）並行送，**同步等齊**後依 `agent_id` 套用（可重現）。
- 記憶 `summary` 一律確定性模板、不呼叫 LLM（§7）。每步 INFO 日誌印實採 batch。

### 8.4 結構化輸出 + 強韌解析（`decision_making.DECISION_SCHEMA`、`json_utils`）
`generate(fmt=DECISION_SCHEMA)`：Ollama 走 `format`、vLLM 走 `guided_json`（受限解碼）；`active mode` 用 enum 限五種。
輸出 token 可預測、解析成功率高。壞 JSON（尾逗號/圍欄/截斷陣列）由 `json_utils.salvage_objects` 物件級救回，不整批作廢。

---

## 9. LLM 後端 adapter（`llm_client.py`、`llm_config.py`、`model_registry.py`）

統一入口 `generate()`，由 `LLM_BACKEND` 決定後端（連線設定在 `.env`，刻意與 `simulation.toml` 分離）：
- **Ollama**（預設）：原生 `/api/generate`，行為與原手刻一致；帶 `options.num_ctx`；前端即時查 `/api/tags` 熱切換模型；think 容錯（不支援的模型自動拿掉 think 重試並記住）。
- **vLLM**：OpenAI 相容 `/v1/chat/completions`（continuous batching 高並行）；options→OpenAI 參數映射；**一機一模型、前端下拉只是「對齊目標」不能熱切換**。候選登錄表 `model_registry.VLLM_MODELS`（Qwen2.5-1.5/7/14B、Phi-3.5-mini、internlm2.5-7b；皆非 gated）。`max_model_len = min(8192, 模型 context)`。

---

## 10. 紅綠燈號誌（`spatial/signals.py`、`engine._advance_along_path` gating）
ESRI 號誌點 snap 到最近路網節點；**方向相位**（bearing mod 180 分兩組，一軸綠、垂直紅，黃燈尾段皆紅）。
車剛好停在號誌節點、要進入下一條邊前看燈（控制相位的是「進場方向」＝前一節點→本節點）；紅燈 → 本步停等（`waiting_at_signal`）。
`cycle_s=90`/`yellow_s=3` 為**合成值**（誠實：非真實時相）。前端 zoom≥14 才畫號誌。

---

## 11. 背景常態車流（`AMBIENT_zh-TW.md`；`engine._build_ambient_agents`/`_respawn_arrived_ambient`）
對齊運輸規劃四步驟：generation（穩態 `count` 台）→ distribution（§4.2 雙端重力 OD）→ assignment（同路網、規則式核心）
→ performance（路網層分析）。背景車**一律規則式、不吃 LLM、不存記憶、不可 inspect、不分批出發**，抵達以重力抽新 OD 重生維持穩態負載。
無人口資料 → 自動停用。`[ambient] count=40, max_count=600`，前端 slider 即時調。
**開場成本（誠實）**：初始化每台（事件＋背景）各算一次 Dijkstra（實測 Tainan ~0.4s/台）→ 背景車越多開場越久。

---

## 11.5 散場（egress）兩階段疏運評估（`docs/EGRESS_zh-TW.md`）
單次模擬涵蓋**進場 + 散場**兩個尖峰。事件車有 `phase`：ingress→dwell（抵達球場停留）→egress→home（返家）。
- **手動觸發（event-based）**：前端「宣告散場」→ `engine.declare_egress()`；停留車在 `[egress].window_minutes` 內依
  `profile`（**peak 一窩蜂**預設）錯開離場，重算路徑往**家**（seeded、可重現）。
- **散場終點＝居住地**（`destination="residence"`）：LLM persona 的 residential_location 正規化；對不到/規則式車用
  **`sample_residence`（∝人口）**後備 → 散場 OD ＝人口加權疏散分布。`begin_egress_leg` 重置記憶累積器（獨立量散場旅時）。
- **散場層分析**（`_egress_analysis`）：疏散曲線、散場旅時分布、返家 OD、**清場時間（到 90% 返家的分鐘數＝頭號指標）**；
  路網層 LOS/V·C/瓶頸本就含散場車流，可與進場對照。`max_steps` 須涵蓋兩階段。
- code：`engine._assign_home`/`_handle_egress`/`_egress_analysis`、`agent.phase`、`demand.sample_residence`、`websocket.declare_egress`。

## 12. 兩層交通分析（交通局視角；`engine.build_analysis`/`_network_analysis`，含公式）

### ① 事件層（只算 `role=="event"`）
抵達曲線（累積 + 每步抵達率=cumulative 差分）、**每步出發**對照（分批出發）、旅行時間分布（`arrival_cycle×step_minutes`）、
出發地 OD（實際 vs 重力期望）、號誌停等總次數、抵達率/平均旅時 summary。

### ② 路網層（事件車＋背景車全部）
- **車流量隨時間**：事件 vs 背景（`event_on_network`/`ambient_on_network` 每步移動台數，堆疊）。
- **服務水準 LOS**（由壅塞 proxy 粗映射）：`<0.2 A, <0.4 B, <0.6 C, <0.75 D, <0.9 E, else F`（平均 + 尖峰）。
- **Top-N 瓶頸路段**：整趟尖峰累積（`_road_peak`），`V/C = peak_flow / capacity`，附 LOS。
- **事件邊際負載占比**：`event_load_share = 100 × event_vehsteps / (event_vehsteps + ambient_vehsteps)`（「車·步」累積）→
  量化「這場活動讓路網多承擔多少」，不需另跑基線。

### ③ 車流監測器（detectors，使用者放置；`engine._update_detectors`/`_register_detectors`）
- **放置（街景丟人式拖放）**：前端拖曳相機 icon 到地圖（拖曳時道路打亮）→ 放開處座標送後端 `snap_point`
  **一律吸附到最近路段**（numpy 點到線段；只有最近道路都 > `_DETECTOR_SNAP_M`=1.5km＝外海/深山才拒絕，否則總落在最近街道）；地圖 icon 隨 zoom 縮放，左面板顯示放置數；
  隨「套用設定」（`apply_config`）一起帶入、初始化時註冊。
- **計數**：以「**進入新邊**」事件累積**通過次數**（背景車重生再經過再計＝真實流量），**與 `step_minutes` 無關**；
  雙向分開、按 `車種(汽/機) × 來源(事件/背景)` 交叉表，前端下拉自選總量/各類別/上下行。**被動量測、不改物理、可重現**。

### ④ GIS 主題圖層匯出（給交通局；`engine.gis_road_records`/`spatial/gis_export.py`）
- 同一套 edge-entry 計數擴及**全路網**（每段累積通過量），結合 `_road_peak` 與 LOS → 匯出 **Shapefile（zip）**：
  **道路服務水準 LOS / 車流量 / 壅塞程度**（線圖層）＋**監測器點位**（點圖層）；前端下拉選圖層、`GET /api/gis/<name>` 下載。
- CRS EPSG:4326（含 .prj/.cpg）、欄名 ≤10 字元對齊 DBF；交通局可在 QGIS/ArcGIS 用屬性分類上色出版級主題圖。
- 圖表面板另提供**每圖 PNG 下載 + 分析數據 CSV**。


---

## 13. 規模化引擎優化（往 1–2 萬台；`SCALING_zh-TW.md §6`，皆可開關）

| # | 優化 | 從→到 | 開關 | 是否改物理 |
|---|---|---|---|---|
| ① | 節點→行政區索引一次（放置）；建表 Point 預建一次 + STRtree bbox 先篩 | 放置每台掃全節點→O(1)；建表 O(節點×區)→~O(節點) | —（covers 一致、候選昇冪映回原順序） | 否（建表 6.4s→~0.3s，全台南每次重設 ~1–2s；2萬台 init ~1hr→~1min 為推估） |
| ③ | 鄰近車數空間網格 | 每步 O(n²) → O(n)（cell=300m，3×3 桶−自己） | `[perception].nearby_mode=grid\|exact` | grid 近似（只餵 LLM）；exact 還原 |
| ④ | 記憶摘要分批 | 單 prompt 爆 context → token 預算分批/並行 | —（不設每步重決上限） | 否 |
| ⑤ | persona 池記憶體快取 | 每批重讀大檔 → 載入一次 | — | 否 |
| ⑥ | 前端 zoom/可視範圍裁切 | 逐台送 → 車多時 zoom out 只送道路、zoom in 只送可視框內車 | `[ui].render_individual_max=1500/agent_min_zoom=14` | 否（只改呈現） |
| ⑦ | current_town 反向索引 O(1) | 每步 O(車×區) 點面 → 查表 | `[perception].town_mode=node\|exact` | node 邊界近似；exact 還原 |

**刻意未做 ②終點最短路樹**：路徑規劃實測 ~14ms/台便宜，且單一樹會 funnel 車流失真。
**回歸基準**：`nearby_mode=exact + town_mode=exact` → 與舊版一致。

---

## 14. 互動 demo 功能（`DEMO_FEATURES_zh-TW.md`）
模型選擇器、可抽換場景/圖層、前端改 prompt、RAG 知識庫（TF-IDF 多重查詢+RRF、provenance 可點看全文、opt-in）、NL 介入（受限動作集：避開某區/某區湧入 N 台）、
上傳場景、暫停對話查詢、自訂時間（週期數/每週期分鐘）、分批出發、決策日誌即時化、**前端整體重設計（§18：頂列+大地圖+可收合底部面板、
系統日誌/忙碌動畫/執行心跳、乾淨彩點 agent、多底圖+色調微調）**。

WebSocket 控制指令（`web/websocket.py`）：start/pause/step/reset/set_speed/apply_config/set_mode/set_ambient/set_max_steps/
set_step_minutes/set_llm/regenerate_profiles/set_scenario/set_view/ask/intervene/clear_intervention/set_prompt。
HTTP（`web/app.py`）：`/`、`/ws`、`/healthz`、`/api/llm/models`、`/api/rag/*`、`/api/scenario/upload`、`/api/prompts`、`/api/decision-outputs[/N]`。

---

## 15. 可重現性（審稿人很在意）
- **seeded RNG 全程**：agent 建立、出生地抽樣、背景 OD、路徑微擾（crc32 非 live RNG）、persona 分批 seed（42+idx）皆走注入 RNG → 同 seed 同軌跡（有 determinism 測試）。
- **近似優化皆可 exact 還原**當回歸基準。
- **bundle 離線可重現**：路網 graphml / 號誌 json / 人口 csv committed。
- **唯一非確定元素**：LLM 決策理由文字 → **不回饋進物理**（記憶 summary 已改確定性模板）。

---

## 16. 參數總表（`config/simulation.toml` ↔ `config.py`，唯一真實來源）
`[time]`(max_steps36/step_minutes5)、`[agents]`(nb_agents/起訖)、`[perception]`(感知半徑300/抵達50/crowded_speed0.55/門檻0.5/nearby_mode/town_mode)、
`[movement]`、`[active_modes.*]`(5模式權重+旗標)、`[roads]`+`[highway_specs.*]`(速度/容量；改值需重建路網)、`[llm]`(use_llm)、
`[memory]`(質性門檻)、`[perception_context]`(hotspots_top_k5/lookahead2000/speed ratios)、
`[profile]`(pool_size 原型數上限)、`[scaling]`(event_triggered/cooldown5/batch_size30/concurrency4)、
`[llm_budget]`(max_model_len8192/reserve1024/overhead800/chars_per_token2.0)、`[demand]`(beta0.08/decay/min_dist0.5)、
`[ambient]`(enabled/count40/respawn/max600)、`[departure]`(window_minutes/profile)、`[signals]`(enabled/cycle90/yellow3)、
`[ui]`(slider 範圍，同時驅動後端 clamp)、`[reproducibility]`(seed42)、`[network]`。
> TOML key == dataclass 欄位名；載入時做合理性檢查（不合理直接報錯）。runtime 可覆寫：`max_model_len`、`ambient_count`。

---

## 17. 誠實限制總表（paper 要主動標清）
1. 號誌時相為**合成值**（台南無真實秒數）→ 非真實號誌孿生。
2. 人口 CSV **近似值** → 正式 paper 換 MOI 官方。
3. RAG（**TF-IDF**、非語意 embedding、opt-in）：
   (a) **只透過「LLM 選 active_mode」這條通道**影響模擬，**不修**路網容量/合成號誌/需求模型/人口近似——誤差若來自這些，RAG 幫不上；
   (b) `vehicle_type` 由 persona 綁定，改運具分布應改 persona 而非 RAG；
   (c) **每批全域檢索、無 per-agent 人格化**（per-archetype 為 future work）；
   (d) 「準確率」宣稱需 **ground truth** 對照（真實散場清空時間/運具分布/路口流量），否則只能稱 grounding 的 **plausibility** 提升。
4. NL 介入限**受限動作集**（避讓/需求突增），非任意控制。
5. 路徑規劃仍**每台 Dijkstra**（②未做）；`nearby`/`current_town` 大規模用近似（邊界誤差，可切 exact）。
6. Persona 重用 N>原型數時出現同名車（LLM 模式實務上規模小不觸發）。
7. 背景車無法共用「終點樹」（各隨機終點）；大規模背景車路徑未輕量化。
8. 上傳場景須**本專案格式 graphml**（非任意 OSM，瀏覽器端不建網）。
9. vLLM 一機一模型、不可熱切換；極新 GPU（5090/sm_120）需較新 vllm + CUDA 12.8+ torch。
10. 壅塞降速為**二元因子**（0.55），非連續 speed–density 關係（連續基本圖 = B1 future work）。
11. 車流為**中觀（mesoscopic）、無微觀跟車**：等紅燈以 **point-queue** 表示、前端做「空間排隊視覺化」（沿進場道依車距錯開，隊長為**示意**、非物理 spillback）。微觀車跟車（IDM）/ 接 SUMO 為 future work。
12. 重力為**無約束**形式（非 Wilson doubly-constrained）；距離用**歐氏直線**（非網路距離）。

---

## 18. 貢獻 vs 現成基礎建設（誠實分界）
**貢獻（應用/整合層）**：① 事件觸發+並行批次的 LLM 決策管線（LLM 成本 ∝ 決策事件數，城市尺度可即時互動）；
② persona/出生地解耦 + 重力需求（空間互動模型生成事件需求）；③ LLM 事件車 + 規則式背景流的混合 + 兩層交通評估（接到交通局式活動交評）；
④ 可即時編輯的互動數位分身（場景/prompt/RAG/NL 介入/暫停對話）。
**現成（引用而非宣稱）**：vLLM/Ollama、networkx、OSMnx、shapely/pyproj、scikit-learn(TF-IDF)、FastAPI/uvicorn、Leaflet/Chart.js。

---

## 19. 建議論文章節對應
- **Introduction / Motivation**：§0、§18（貢獻）。
- **Related Work**：重力/spatial interaction（§4）、四步驟模型（§11）、LLM agents、microscopic ABM。
- **System Overview**：§1、§2、`ARCHITECTURE.md` mermaid。
- **Method**：§3 路網/路徑/壅塞、§4 需求、§5 行為模式、§6 感知、§7 記憶、§8 決策核心+規模化、§10 號誌、§11 背景流。
- **Scalability**：§8.3、§13（+ 量測 scalability 曲線，目前 ⏳）。
- **Evaluation / Demonstration**：§12 兩層分析、§14 互動、規則 vs LLM 對照。
- **Reproducibility**：§15、§16。
- **Limitations**：§17。

---

## 20. 支援模組內部演算法細節（補充，逐行精讀後補入）

> 本節把前面各節用到、但屬於「實作內部」的關鍵演算法攤開，供寫 Method/Implementation 細節時引用。

### 20.1 強韌 LLM-JSON 解析（`llm_server/json_utils.py`、`decisions/response_parser.py`）
- `repair`：智慧引號→直引號、`True/False/None`→`true/false/null`（用 word-boundary 避免改到字串內）、移除 `}`/`]` 前尾逗號。
- `salvage_objects`（**最關鍵**）：先 `loads_lenient`（去 ```json``` 圍欄→原文→repair 後各試一次 `json.loads`）；
  失敗時用 `_balanced_object` **字串感知**地逐一掃出頂層平衡 `{...}`（正確處理字串內引號/跳脫），各自寬鬆解析、能解的就收 →
  **陣列被截斷時保留前面完整物件、只丟最後半個**（小模型輸出截斷的常見情形）。
- `response_parser`：欄位**多 key 別名**容錯（mode：`active_mode`/`active mode`/`mode`/`type`；vehicle：`vehicle_type`/`車種`/`vehicle_ownership`；
  name/id/origin/reason 各有別名清單）；`normalize_town_name` **由長到短**比對區名（避免「安南區」被「南區」搶先命中）；
  支援 dict 含 `agents/decisions/initial_vehicles/requested_agents`、純 list、單 dict、含雜訊字串。

### 20.2 RAG 知識庫（`llm_server/rag_store.py` + `rag_query.py`）
**檢索引擎（rag_store，通用、不認識交通）**：sklearn `TfidfVectorizer(analyzer="char_wb", ngram_range=(2,4), min_df=1)`
（char n-gram 對中文友善、零額外依賴、離線）。**句/段感知切塊**：先以句末標點/換行斷句、再貪婪打包到 ~`CHUNK_SIZE=400` 字、
塊間以整句重疊（≤`CHUNK_OVERLAP=80` 字）；無標點超長句（如 CSV 整列）硬切保底——好處是每塊為完整句集合，provenance 顯示乾淨。
`_topk_with_scores` 為單一 query 檢索核心（cosine top-k、門檻 `>SIM_FLOOR=0.01`），`retrieve`/`retrieve_multi` 共用。
`enabled` 預設 True 但**無上傳文件即無作用**。

**多重查詢 + RRF（`retrieve_multi`）**：`rag_query.build_subqueries` 每批從模擬狀態組三條子查詢——
路況（取【全域路況】）、任務（固定描述五種 active_mode，英文 key + 中文）、人格（聚合這批 persona 的職業/車種/特質高頻）；
各自檢索 `PER_QUERY_K=5` 塊，依名次以 Reciprocal Rank Fusion（`RRF_C=60`）融合去重，取 `DEFAULT_K=4` 注入。
被多條子查詢撈到的塊分數自動變高。回傳含 provenance（source/idx/via/scores）。
`rag_store.query_mode`：`multi`（預設）/`single`（只用 perception 當 query，ablation 對照）。
**仍是每批全域檢索、不做 per-agent**（控 token）。

**provenance（可解釋性）**：每批 hit 的 `source/idx/via/scores` 隨**回傳值**往上帶（`run_decision_making → llm_adapter.decide_step_traced
→ engine._llm_decide_batched`；**並行安全，不走模組全域**），在 engine 依 `(source,idx)` 去重（`_dedupe_provenance`，留 rrf 高者）
後放進 snapshot `rag_provenance`，經 WebSocket 送前端決策日誌：每步顯示「本批參考知識 N 段」可收合清單（檢索面向色標 + 來源#塊號 + 預覽），
點擊以 modal 看完整片段 + 相似度。讓操作者看到 LLM 決策的在地知識依據。

**論文定位與引用**（誠實、不過度宣稱）：基底為 **Naive RAG（Lewis et al., 2020；分類見 Gao et al., 2023 survey「Naive/Advanced/Modular RAG」）**；
查詢端用 **multi-query + RRF**（RAG-Fusion；RRF＝Cormack et al., 2009《Reciprocal Rank Fusion outperforms Condorcet…》），屬 survey 的
**Advanced RAG → Query Transformation**；provenance 透明化承接 **Self-RAG（Asai et al., 2024）「檢索內容應可被檢視」** 的精神（未做整套反思）。
HyDE（Gao et al., 2022）已實作為**長文件查詢增強的 opt-in**（`rag_query.hyde_expand`）：`rag_store.hyde_active()`＝`hyde_enabled`
（**預設關閉**）且語料塊數 > `HYDE_GATE_CHUNKS=50` 時，先用 LLM 把各子查詢改寫成「假想手冊片段」再檢索（橋接 descriptive↔prescriptive；
每子查詢多一次輕量生成，失敗自動降級回原 query）；短語料不划算 → 走純檢索。**引用年份/venue 入稿前請再核對**。

**ablation（免改 code，靠旗標切換）**：四段對照——`無 RAG`（`enabled=False`）→ `single`（`query_mode="single"`，只用 perception）→
`multi`（預設，多重查詢+RRF）→ `multi+HyDE`（`hyde_enabled=True` 且語料夠大）；固定 seed、同一情境，比同一指標（散場清空時間／停等分布是否更貼近上傳報告）。

### 20.3 persona 生成 / prompt（`agent_profile`/`prompt_store`）
- `agent_profile`：**prompt 驅動、無 schema 驗證**；批次帶 `seed=42+idx`（同 seed+prompt→相同輸出，故不同 seed 保多樣又可重現）。
- `prompt_store`：`register_default`/`get`(覆寫優先)/`set_override`(空→還原)；結構化輸出 schema 保證即使 prompt 被改壞仍吐合法 JSON。
- 註：記憶摘要(`memory_summary`)已移除——`summary` 一律確定性模板,不再有 LLM 摘要器(見 §7、`MEMORY_zh-TW.md`)。

### 20.4 NL 介入 / 對話（`sim_intervene`/`sim_chat`/`engine.apply_intervention`）

**介入完整流程**：前端「介入」模式 → `control{action:"intervene"}` → `websocket._intervene`：
① `sim_intervene.run_intervene(text, available_towns)` 解析成 `{action, town, count}` → ② `engine.apply_intervention(...)` 真正套用 → ③ 立即 `snapshot_now()` 推前端（不等下一步、即時反映）。

**解析（`sim_intervene`）**：**關鍵字優先**（確定性、對受限指令最可靠）——含「避/封/繞/別走/不要走」→ `avoid_area`；
含「湧入/增加/多/湧進/新增」+ 數字或區名 → `demand_surge`；判不出（none）才用**前端所選 LLM** + schema 解析模糊措辭；
`town` 正規化到實際可用行政區（長到短部分比對）。動作集 = `{avoid_area, demand_surge, none}`（**受限動作沙盒**，非任意控制）。

**套用（`engine.apply_intervention`）**：
- `avoid_area`：在該區形心放一個 **半徑 2500m 的避讓圓**（存入 `engine._avoid_circles`）；對**所有移動中的車**重算路徑，
  `routing.find_path` 對「下一節點落在避讓圓內」的邊 **成本 ×25（近乎封路）** → 車流繞開該區。回報重算車數。
- `demand_surge`：新增 `count`（clamp 1–1000）台**事件車**（`surge_*`，起點＝指定區/各區隨機、終點＝球場），當場 `_place_agent` 算路徑加入。
- **清除介入**（`clear_interventions`）：清掉所有避讓圓並讓移動中車恢復正常規劃（已湧入的車不移除）。
- **只影響事件車**：避讓重算 / 湧入新增都針對事件車；**背景常態車流不受 NL 介入**（它有自己的重力 OD）。

**對話（`sim_chat`）**：唯讀，只把「引擎組好的狀態文字（`chat_context`）+ 問題」送 LLM，要求不杜撰未提供數據；LLM 不可用 → fallback 附狀態文字。

> **模型來源（重要）**：對話與介入的 LLM 解析都呼叫 `llm_client.generate(...)` **不帶 `model=`** → 一律使用**前端模型選擇器所設的後端+模型**（`llm_config.set_runtime_llm`），與決策/人物整套共用。

### 20.5 路網建構與查詢（`spatial/road_network.py`、`geojson.py`）
- 三層 fallback：讀 graphml →（不存在且允許時）OSMnx `graph_from_polygon(network_type="drive")` **以縣界(`gis_loader.load_county_boundary_wgs84`，TOWN_MOI 篩該縣市 union＝全台南 37 區)為下載邊界** → 確定性合成網格。graphml gitignore、首次自動建檔。
- OSM 轉換：WGS84→EPSG:3826（pyproj）、去重複邊、依 OSM highway 套 `[highway_specs]` 速度/容量、邊幾何存 WKT；**只保留最大強連通分量**（避免 no-path）。
- `nearest_node`：numpy 向量化 `argmin` 平方距離；`random_node_in_town`：`geom.covers` 內節點隨機，否則形心最近節點（與 engine ① 索引同邏輯確保一致）。
- graphml 只存純量 + 邊 WKT，載回還原成 shapely LineString。`geojson.roads_to_geojson(only_major=True)`：前端底圖只送主要道路（~7k）保效能、全網仍供路徑規劃。

### 20.6 號誌建構與相位（`spatial/build_signals.py`、`spatial/signals.py`）
- **build（離線）**：讀路網節點 + 號誌點 shapefile→EPSG:3826；`scipy.cKDTree` 找每節點最近號誌點，`≤40m` 即號誌路口
  （**同一路口多號誌頭自然收斂到同一節點＝專業去重**）；相位軸 `ax` = 進場邊方位角 mod 180 的第一個；
  `two`（兩相位）= 是否存在與 ref 夾角 >30° 的 approach；offset = `md5(node_id) % cycle`（相鄰號誌不同步、確定性）。
- **runtime 相位**：`tc=(elapsed_s+off)%cycle`；組0綠 `tc∈[0, half−yellow)`、組1綠 `tc∈[half, cycle−yellow)`，黃燈尾段兩組皆紅；
  進場方向與 `ax` 夾角(mod180)`≤45°`歸組0否則組1；`two=False`/關閉/檔不存在 → `is_green` 恆 True（不卡車流）。**cycle/yellow 為合成值**。

### 20.7 GIS 載入與場景（`spatial/gis_loader.py`、`scenarios.py`）
- `gis_loader`：TOWN_MOI 全台→依場景 `county_filter` 篩；CRS 自動轉 EPSG:3826；人口 CSV join（`#` 註解/標題列跳過）；
  球場 point（場景有 dest_lat/lng 則覆寫換事件地點）；研究範圍 polygon 供 OSM 下載。
- `scenarios`：`Scenario` 合約（county_filter/road_graphml/population_csv/signals_json/dest/center/zoom）；內建 `tainan_stadium`（預設）
  + `tainan_station`（示範換事件地點）；`data/scenarios/*.json` manifest 自動註冊（builder / UI 上傳共用）。active 為 process 全域。

### 20.8 LLM 連線設定三分離（`llm_server/llm_config.py`）
**刻意分三處不重疊**：`.env`（連到哪/怎麼連：`LLM_BACKEND`/`OLLAMA_URL`/`OLLAMA_MODEL`/`OLLAMA_MODE`/`VLLM_URL`/`VLLM_MODEL`，gitignore）、
`config/simulation.toml`（模擬參數，committed）、`prompt_store`（runtime prompt）。`set_runtime_llm`/`current_model` 讓前端即時切後端/模型（整套 LLM 共用）。

### 20.9 Token 校準 CLI（`calibrate.py`）
用 mock 引擎跑 3 步產生真實 payload+persona，組出與正式 decision prompt **完全一致**的文字，
用「1 台 vs n 台」兩點回歸出 `overhead`（固定開銷）與 `per_agent_token` → 反推「此 max_model_len 下安全 batch」與
「維持現 batch 需多大 max_model_len」，並印 `vllm serve` 建議。有 `transformers --model` 用真 tokenizer，否則 `chars_per_token` 粗估。純量測、不呼叫 LLM。

### 20.10 其他
- `domain/events.py`：`RouteStatus` enum（CREATED/MOVING/ARRIVED/ERROR）。
- `decisions/base.py`：`DecisionPolicy` Protocol + `InitAssignment`/`StepDecision` dataclass（引擎只透過此抽象與核心互動）。
- `simulation/random_seed.py`：單一 `random.Random(seed)` 注入全引擎。
- `metrics.py`：每步指標累積在記憶體 `MetricsRecorder.history`（前端圖表與 `build_analysis` 都讀它，**不落地 CSV**）。
- 資料前處理 CLI：`spatial/build_roads.py`（OSMnx 下載/合成）、`spatial/build_scenario.py`（建新縣市/尺度 bundle）、`build_signals.py`（號誌 artifact）。

---

> 維護：本檔隨系統改動同步（見 [[always-update-docs]] 精神）。與其他 doc 衝突時，以**程式碼**為準、回報修正。
