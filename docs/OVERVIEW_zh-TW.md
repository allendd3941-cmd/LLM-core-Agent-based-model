# 系統總覽與設計決策（paper 撰寫用完整參考）

> 主題：**LLM-Driven Mobility Digital Twin for Event-Based Urban Traffic Simulation [Demo]**
> 場景：台南亞太棒球場賽事進出場尖峰人潮的短期交通衝擊。目標：ACM SIGSPATIAL 2026 demo paper。
>
> 本檔是**單一入口**：把所有功能、為什麼這樣設計、空間/研究角度、是貢獻還是現成基礎建設、誠實限制、
> 可重現性、設定旋鈕與對應程式碼/細節文件,集中整理。各項末標 **[doc]** 與 **[code]** 指向細節。

---

## 0. 一句話定位
在**真實 OSM 路網 + 行政區人口**上,用 **LLM 認知核心**驅動「事件參與者」的微觀交通決策,疊加由**輕量規則核心**模擬的**常態背景車流**,做成**可即時互動、可規模化、可重現**的事件交通數位分身;並輸出**交通局視角的兩層交通評估**。

## 1. 文件地圖（哪個 doc 講什麼）
| 文件 | 內容 |
|---|---|
| **OVERVIEW_zh-TW.md**（本檔） | 全功能總覽 + 設計決策 + 貢獻/限制 + 可重現性 |
| `ARCHITECTURE.md` | 分層架構、每步資料流（mermaid）、決策核心路徑 |
| `PYTHON_SIMULATOR_zh-TW.md` | 安裝/啟動、設定表、Persona 原型池、SSH/部署 |
| `SCALING_zh-TW.md` | LLM 規模化（事件觸發+批次）+ §6 引擎規模化優化（①③④⑤⑥⑦） |
| `DEMAND_zh-TW.md` | 事件車出生地的重力模型（人口×距離衰減） |
| `AMBIENT_zh-TW.md` | 背景常態車流（雙邊重力 OD）+ 兩層交通分析 |
| `MEMORY_zh-TW.md` | 單一旅次記憶（不分長短期；重決策時 LLM 摘要） |
| `ENVIRONMENT_zh-TW.md` | 送 LLM 的環境感知（質性標籤、熱點、前方路況） |
| `ACTIVE_MODES_zh-TW.md` | 五種 active_mode 的權重與路徑策略 |
| `DEMO_FEATURES_zh-TW.md` | 互動功能（模型選擇器/分析/對話/場景/prompt/RAG/NL 介入/上傳/zoom 渲染） |
| `CHANGES_LLM_PIPELINE_zh-TW.md` | LLM pipeline 重構（perception 模板化、token 預算切批、結構化輸出） |
| `DATA.md` | 資料來源（GIS、路網、人口、號誌） |

## 2. 分層架構
- `llm_abm_simulator`（模擬器，**主體**）：`domain`（agent/road/town/state）、`spatial`（OSM 路網/路徑/GIS/號誌）、
  `mobility`（重力需求）、`decisions`（核心 registry / 規則式 / LLM adapter / persona 池 / 回應解析）、
  `simulation`（engine 主迴圈 / metrics / scheduler）、`web`（FastAPI + WebSocket）。
- `llm_server`（LLM pipeline，**in-process 直呼**）：agent_profile（人格生成）、perception（**確定性模板、不呼叫 LLM**）、
  decision_making（決策，結構化輸出）、memory_summary（記憶摘要）、rag_store、sim_chat、sim_intervene、
  llm_client（Ollama/vLLM 統一入口）、llm_config / model_registry。
- **無 GAMA、無 HTTP hop**：原型是 GAMA+FastAPI，現已全改成 Python 原生 + in-process pipeline。[doc] ARCHITECTURE.md

---

## 3. 功能與設計決策（逐項）

### 3.1 可選決策核心（規則式 vs LLM）
- **定位**：事件車的決策核心可在前端切換：`rule`（確定性啟發式）/ `llm`（依人格與感知決策）。
- **為何**：兩者對照本身就是論文賣點（同路網/同事件車流比 LLM 認知 vs 傳統規則的繞道/抵達/壅塞）;規則式同時是 demo 的零成本 baseline 與 LLM 不可用時的 fallback。
- **設計**：核心登錄於 registry；`engine.last_decision_source` 即核心 key（下發前端顯示）。LLM 失敗自動 fallback 規則式、不崩。
- **事件觸發**：LLM 模式只在「踩到壅塞/前方塞」時重決（其餘維持現 mode）→ LLM 成本 ∝ 決策事件數,而非 agent×步數。觸發車分批（token 預算切批）、並行送出。
- **設定**：`[llm].use_llm`、`[scaling].event_triggered_decisions/cooldown_steps/batch_size/concurrency`、`[llm_budget]`。
- [doc] SCALING_zh-TW.md、DEMO_FEATURES_zh-TW.md §10　[code] `decisions/registry.py`、`mock_policy.py`、`llm_adapter.py`、`engine._apply_step_decisions`

### 3.2 事件車出生地：重力模型（persona/出生地解耦）
- **定位**：事件車「從哪來」由**生產約束重力模型**決定：`T_i ∝ 人口_i × f(d_i)`,`f=exp(−β·d_km)` 或 `d_km^(−β)`。
- **為何**：把「人是誰（persona）」與「人從哪來」**解耦**——出生地不再由 persona 的 residential_location 決定,而是依各區人口與到場館距離,符合空間互動模型,且不必生成龐大且與真實人口一致的 persona 池。
- **機制**：每台事件車**獨立**依機率抽一個出生區（seeded）→ 各區數量 ≈ 機率×總數,總數恰為 `nb_agents`。**會覆蓋** persona 的 residential_location（重力啟用時）；停用/無人口資料則回退 persona 居住地。
- **空間角度**：β 是距離敏感度（催客圈大小）,可即時展示;分析層用「實際 vs 重力期望」OD 對照。
- **設定**：`[demand].enabled/beta/decay/min_distance_km`；人口 `data/gis/town_population.csv`（**近似值,paper 前換 MOI 官方**）。
- [doc] DEMAND_zh-TW.md　[code] `mobility/demand.py`（`assign_origin_towns`/`gravity_weights`/`expected_distribution`）

### 3.3 背景常態車流（ambient）
- **定位**：在事件車之外注入**不指定事件終點的常態背景車**,提供路網基礎負載（壅塞場）,讓 LLM 事件車感知到的壅塞更真實。
- **設計（對齊四步驟模型）**：trip generation（穩態車數）→ trip distribution（**雙邊重力 OD**：起點∝人口、終點∝人口×距離衰減）→ assignment（同路網、規則式核心）→ performance（路網層分析）。抵達後以重力抽新目的地**重生**,維持穩態。
- **取捨**：背景車**一律規則式、不吃 LLM、不存記憶、不可 inspect**（成本可控 + 敘事乾淨：「LLM 事件參與者穿越規則式背景流」）。`role` 欄位區分 event/ambient。
- **前端區分**：事件車＝狀態色 icon/dot;背景車＝低調灰小點 + 圖例 + 顯示開關 + 「背景車 N」狀態。
- **設定**：`[ambient].enabled/count/respawn/max_count`；前端 slider 即時調 `count`。
- [doc] AMBIENT_zh-TW.md　[code] `demand.sample_od_pairs`/`sample_dest_town`、`engine._build_ambient_agents`/`_respawn_arrived_ambient`

### 3.4 兩層交通分析（交通局視角）
- **事件層（只算事件車）**：抵達曲線（累積+每步抵達率）、旅行時間分布、出發地 OD（實際 vs 重力期望）、號誌停等。
- **路網層（事件車＋背景車全部）**：總車流量隨時間（事件/背景堆疊）、**服務水準 LOS（A–F，由壅塞 proxy 映射）**、
  **Top-N 瓶頸路段 V/C**（整趟尖峰累積）、**事件/背景車佔路網負載比（邊際負載）**——量化「這場活動讓路網多承擔多少」,即大型活動交評。
- [doc] AMBIENT_zh-TW.md §4　[code] `engine.build_analysis`/`_network_analysis`/`_road_peak`、`charts.renderAnalysis`/`renderNetwork`

### 3.5 單一旅次記憶（不分長短期）
- **定位**：每台事件車一個 `memory`：running 的自然語言 `summary` + 當下印象 + 整趟聚合量（塞過的點/換策略次數/順暢度/已行時間）。
- **為何**：1 step=1 分鐘,長短期區分無意義 → 合併單一記憶,paper 好說明。
- **摘要時機**：規則式核心→模板每步重算;**LLM 核心→只在「重新決策」時由 LLM 重寫一次**（記憶在做決定的當下最新、也省 LLM；大規模分批避免爆 context）。失敗 fallback 模板。
- **可重現**：聚合量全程確定性,不影響軌跡。
- **設定**：`[memory]` 質性門檻、`[summary].summary_model`。
- [doc] MEMORY_zh-TW.md　[code] `domain/agent.py`（`memory`/`update_memory`/`memory_facts`）、`engine._summarize_memory`

### 3.6 紅綠燈號誌
- **定位**：把 ESRI 號誌點位 snap 到路網節點,做**方向相位停等**（一軸綠、垂直軸紅,黃燈尾段皆紅）。
- **誠實限制**：**台南只有點位、無真實時相秒數**（只有臺北/澎湖有,且 ID 無法 join）→ `cycle_s`/`yellow_s` 為**合成值**,相位軸由 bearing mod 180 分兩組。**不是真實號誌孿生**,需在 paper 標清。
- **設定**：`[signals].enabled/cycle_s/yellow_s`；點位 `data/tainan_signals.json`。
- [doc] DATA.md　[code] `spatial/signals.py`、`spatial/build_signals.py`、`engine._advance_along_path`（gating）

### 3.7 Persona 原型池
- **定位**：人格設定（identity/traits）由 LLM 生成,存成穩定原型池;**`pool_size` 是「原型數上限」,與模擬車數 `nb_agents` 分離**。
- **抽樣重用**：車數 ≤ 原型數 → 各車不同;車數 > 原型數 → `pool[i % len]` 循環重用（少量原型餵大量車）。
- **分批生成**：一次 LLM 吐不出大量 persona（輸出截斷）→ 切批（每批數量依輸出預算自動推算、**每批不同 seed** 以保多樣又可重現,沿用 `[scaling].concurrency` 並行）。
- **效能**：池載入**記憶體快取**,`personas_json` 不再每決策批次重讀大檔（⑤）。前端「重新生成人物」清池重生。
- **限制**：重用時 N>原型數會出現同名車（大規模可接受;LLM 模式實務上小於原型數）。
- **設定**：`[profile].pool_size`。也可**離線用大模型預生成**好一份丟進 `output/agent_profile_output_1.txt`,執行期零生成。
- [doc] PYTHON_SIMULATOR_zh-TW.md §4c　[code] `decisions/profile_pool.py`、`llm_server/agent_profile.py`

### 3.8 規模化優化（往數萬台 agent；SCALING §6）
| # | 優化 | 從 → 到 | 開關/設定 | 改結果? |
|---|---|---|---|---|
| ① | 節點→行政區索引一次（放置） | 每台掃全節點 shapely O(節點×車數) → O(1) 抽 | —（與 covers 一致） | 否（實測每台放置 ~177ms→O(1)；**推估** init 2萬台 ~1hr→~1min） |
| ③ | 鄰近車數空間網格 | 每步 O(車數²) → O(車數) | `[perception].nearby_mode=grid\|exact` | grid 近似(只餵 LLM);exact 還原 |
| ⑦ | current_town 反向索引查表 | 每步 O(車數×區數) → O(1) | `[perception].town_mode=node\|exact` | node 邊界近似;exact 還原 |
| ④ | 記憶摘要分批 | 單一 prompt 爆 context → token 預算分批/並行 | —（不設每步重決上限,依使用者要求） | 否 |
| ⑤ | persona 池記憶體快取 | 每批重讀大檔 → 載入一次快取 | — | 否 |
| ⑥ | 前端 zoom/可視範圍裁切 | 逐台送/畫 → 車多時 zoom out 只送道路、zoom in 只送可視範圍車 | `[ui].render_individual_max/agent_min_zoom` | 否（只改呈現） |
- **刻意未做：② 終點最短路徑樹**——實測路徑規劃 ~14ms/台很便宜,且單一樹會 funnel 車流（壅塞失真）;日後每步重算路成瓶頸再評估。
- **驗證基準**：`nearby_mode=exact` + `town_mode=exact` → 結果與舊版一致。
- [doc] SCALING_zh-TW.md §6　[code] `engine._build_town_node_index`/`_node_in_town`/`_current_town`/`_build_nearby_grid`/`_count_nearby`/`_visible_agents`/`set_view`

### 3.9 互動 demo 功能
| 功能 | 說明 | code |
|---|---|---|
| **LLM 模型選擇器** | 前端選後端（Ollama/vLLM）+ 模型;整套 LLM 共用所選模型 | `llm_config`/`model_registry`/`websocket._set_llm` |
| **可抽換場景/圖層** | `tainan_stadium`/`tainan_station` 內建 + `build_scenario` 建新縣市 | `scenarios.py`/`spatial/build_scenario.py` |
| **前端可改 Prompts** | 即時改人格/決策 prompt（結構化輸出保護格式不崩） | `llm_server/prompt_store.py` |
| **RAG 知識庫** | 上傳文字 → decision 每批用路況檢索注入（sklearn TF-IDF char n-gram,非 embedding） | `llm_server/rag_store.py` |
| **自然語言介入** | 受限動作集（避開某區/某區湧入 N 台）,關鍵字優先解析 | `llm_server/sim_intervene.py`/`engine.apply_intervention` |
| **網頁上傳場景** | 上傳本專案格式 graphml + 人口 CSV → 註冊切換 | `web/app.py` `/api/scenario/upload` |
| **暫停對話查詢** | 暫停時用當前路況 + LLM 回答（唯讀） | `llm_server/sim_chat.py` |
| **分頁化 UI** | 頂列 + 左控制 + 大地圖 + 可收合底部面板（即時/分析/對話/日誌四分頁）；KPI 浮在地圖上 | `simulation.js`/`index.html` |
| **自訂時間** | 前端可調「週期數 / 每週期分鐘」（範圍由 `[ui]` 下發，改了重設） | `websocket._set_time` |
| **決策日誌即時化** | 重決批次 → WS 推送（mode/reason + 解析健康度 fallback 數）；取代讀 txt 檔，原始 JSON 仍寫 `output/` 供離線檢核 | `engine._record_decision_log`/`simulation.js updateDecisions` |
| **系統日誌 + 忙碌動畫 + 執行心跳** | 任何操作有時間戳分級日誌；重操作有頂列進度條+按鈕 spinner（純前端，收到 init/state 解除）；心跳顯示在不在跑 | `app.js beginBusy/endBusy`/`simulation.js log/setRunState` |
| **乾淨彩點 agent** | 移除 emoji 車圖，改狀態色圓點+車種大小（更專業） | `map.js upsertAgentDot` |
| **多底圖 + 色調微調** | 5 種免金鑰底圖（暗/淺/Voyager/OSM/衛星）+ 亮度/對比/飽和 sliders | `map.js buildBaseLayers/addAppearanceControl` |
- [doc] DEMO_FEATURES_zh-TW.md（P0–P3 全列；§18 為前端整體重設計）

### 3.11 事件車分批出發（時空需求）
- **定位**：事件車**陸續進場**而非全部 cycle 0 同時出發——加每台 `departure_cycle`，之前「尚未進場」
  （`waiting_for_origin`：不移動、不算流量、不顯示），到點才跑。出發時間在 [0, 視窗] 依 profile 抽樣（seeded）。
- **為何**：真實事件 ingress 是時間分布的；這讓抵達曲線變成真實 ramp-up，是清楚的**時空需求**貢獻。
- **設定**：`[departure].window_minutes`（0＝同時出發＝舊行為，向後相容）/ `profile`（uniform/front_loaded/peak）。背景車不分批。
- **分析**：抵達曲線加「每步出發」對照；狀態列「未出發 N」。
- [doc] DEMO_FEATURES_zh-TW.md §17　[code] `engine._assign_departures`/`_activate_due_departures`

### 3.10 LLM 後端（Ollama / vLLM）
- **Ollama**：即時查 `/api/tags` 列實裝模型,**前端可熱切換**;帶 `options.num_ctx`。適合本機/無 GPU。
- **vLLM**：OpenAI 相容、continuous batching、高並行（`--max-num-seqs`）;**一個 server 綁一個模型,前端下拉只是「對齊目標」,不能熱切換**（換模型要重啟 `vllm serve`）。需 GPU。結構化輸出走 `guided_json`。
- **設定（連線）**：`.env` 的 `LLM_BACKEND`/`OLLAMA_*`/`VLLM_URL`/`VLLM_MODEL`（刻意與 `simulation.toml` 分離）。
- [doc] PYTHON_SIMULATOR_zh-TW.md、SCALING_zh-TW.md（vLLM 啟動）　[code] `llm_server/llm_client.py`/`llm_config.py`

---

## 4. 研究貢獻 vs 現成基礎建設（誠實分界）
**貢獻在應用/整合層**：
1. **事件觸發 + 並行批次的 LLM 決策管線**：使 LLM 成本 ∝ 決策事件數,讓 LLM 驅動的微觀 ABM 可城市尺度即時互動。
2. **persona/出生地解耦 + 重力需求**：把「人是誰」與「人從哪來」分離,以空間互動模型生成事件需求。
3. **LLM 事件車 + 規則式背景流的混合,並輸出兩層交通評估**：把 LLM ABM 接到交通局式的活動交評（LOS/瓶頸/邊際負載）。
4. **可即時編輯的互動數位分身**：場景/prompt/RAG/NL 介入/暫停對話,面向 demo 與決策溝通。

**現成基礎建設（非貢獻,需引用而非宣稱）**：vLLM/Ollama（推論）、networkx（圖/最短路）、OSMnx（路網）、shapely/pyproj（GIS）、scikit-learn（TF-IDF）、FastAPI/uvicorn、Leaflet/Chart.js。

## 5. 可重現性（審稿人很在意）
- **seeded RNG 全程**：同 seed → 同軌跡（agent 建立、出生地抽樣、背景 OD、路徑微擾、persona 分批 seed 皆走注入 RNG）。
- **近似優化皆有開關還原**：`nearby_mode=exact`、`town_mode=exact` → 與未優化版一致,可當回歸基準。
- **bundle 資料離線可重現**：`data/tainan_roads.graphml`（OSM 路網）、`data/tainan_signals.json`（號誌）、`data/gis/town_population.csv`（人口）committed。
- **確定性 perception/記憶模板**：規則式核心完全不依賴 LLM;LLM 文字（決策理由、摘要）是唯一非確定元素,且**不回饋進物理**。

## 6. 誠實限制總表（paper 要主動標清）
1. **號誌時相為合成值**（台南無真實秒數）→ 非真實號誌孿生。
2. **人口 CSV 為近似值** → paper 前換 MOI 官方資料。
3. **RAG 為 TF-IDF**（非語意 embedding）→ 只在上傳「真正影響決策的知識」時有意義;可升級。
4. **NL 介入限受限動作集**（避讓/需求突增）,非任意控制。
5. **路徑規劃仍為每台 Dijkstra**（② 未做）;`nearby`/`current_town` 大規模用近似（邊界誤差,可切 exact）。
6. **Persona 重用** N>原型數時同名車;LLM 模式實務上規模小、不觸發。
7. **每步寫全 agent CSV** → 數萬台時 I/O 重（未優化）。
8. **背景車無法共用「終點樹」**（各有隨機終點）;大規模背景車的路徑/CSV 尚未輕量化。
9. **上傳場景須本專案格式 graphml**（非任意 OSM 檔,瀏覽器端不建網）。
10. **vLLM 一機一模型、不可前端熱切換**;極新 GPU（如 5090/sm_120）需較新 vllm + CUDA 12.8+ torch。

## 7. 部署與執行（重點）
- **兩個 process**：① 網頁（uvicorn :8080,內含 in-process LLM pipeline）② vLLM（:8001,需 GPU）。經 HTTP（`VLLM_URL`）溝通。
- 啟動 vLLM（**勿用 `uv run`**,會劫持專案 .venv）：`uv pip install --python .vllm-env vllm ninja` → `source .vllm-env/bin/activate` → `vllm serve <model> --port 8001 --max-model-len 8192 …`（ninja 給 FlashInfer JIT 用,沒裝會崩 `FileNotFoundError: 'ninja'`;若再缺 nvcc 可用 `VLLM_USE_FLASHINFER_SAMPLER=0` 免編譯）→ 等 ready（`curl :8001/v1/models` 通）→ 再開網頁、前端切 LLM。詳見 `SCALING_zh-TW.md`。
- 無 GPU 用 Ollama（`LLM_BACKEND=ollama`,不必開第二 serve）。
- [doc] PYTHON_SIMULATOR_zh-TW.md（SSH 通道/tmux）、SCALING_zh-TW.md（vLLM 啟動）
