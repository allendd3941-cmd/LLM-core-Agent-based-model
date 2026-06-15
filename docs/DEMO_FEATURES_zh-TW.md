# 互動 Demo 功能（P0②③④）

本檔說明三個面向 SIGSPATIAL demo 的互動功能。資料流均透過 WebSocket。

## 1. 前端 LLM 模型選擇器（P0②）

控制面板「LLM 模型」兩個下拉：**伺服器（Ollama / vLLM）→ 模型**。整套 LLM 用途
（agent_profile / decision_making / 暫停對話 / 介入）**共用所選模型**。

- **Ollama**：即時查 `/api/tags` 列出實裝模型，可熱切換；選定後自動帶 `options.num_ctx`
  （避免 Ollama 預設 ~2k 截斷）。
- **vLLM**：列 5 個候選登錄表（`llm_server/model_registry.py`），定位為**「對齊目標」**——
  只設模型名 + context 給 client / token 預算用；**實際 vLLM server 由你自己 `vllm serve` 啟動**
  （API 無法熱切換模型）。選 vLLM 模型時 `max_model_len = min(8192, 模型 max_context)`。
- 後端：runtime 狀態在 `llm_server/llm_config.set_runtime_llm()`；`config.effective_max_model_len()`
  供 token 預算切批。`.env` 為啟動預設、UI 可即時覆寫。
- 協定：`GET /api/llm/models?backend=…`、WebSocket `control{action:"set_llm", value:{backend,model}}`、
  `init.config.llm`（目前後端/模型 + vLLM 候選）。

5 個 vLLM 候選（非 gated、可直接串）：Qwen2.5-1.5B/7B（推薦）/14B-Instruct、Phi-3.5-mini-instruct、
internlm2.5-7b-chat。詳見 `model_registry.py` 與記憶 `llm-model-selector-plan`。

## 2. 模擬後交通分析圖（P0③）

模擬跑完（自動）或主動要求（`control{action:"analysis"}`）→ 後端 `engine.build_analysis()`
送 `{type:"analysis", …}`，前端右側「📊 交通分析」面板用 Chart.js 畫：

- **抵達曲線**：累積抵達 + 每步抵達率（事件尖峰負荷）
- **旅行時間分布**：各 agent 抵達步數×step_minutes 的直方圖
- **出發地分布（OD）**：實際 vs **重力期望**（空間性，扣合需求模型）
- **摘要**：抵達率、平均旅行時間、號誌停等總次數

資料來源：`recorder.history`（累積抵達 / 壅塞 / 號誌停等）+ 各 agent `arrival_cycle` + 出生地計數。
> 後續可加（P1/P2）：地圖上的 OD desire lines、時空壅塞熱圖、瓶頸路段 Top-N。

## 3. 暫停對話查詢（P0④）

控制面板「💬 與模擬對話」：輸入問題（或點建議 chips）→ `control{action:"ask", value:問題}`
→ 後端用 `engine.chat_context()`（當前步數/抵達數/整體交通/壅塞熱點/等紅燈數）+ 所選 LLM
（`llm_server/sim_chat.py`）回答 → 前端聊天泡泡顯示。**唯讀**（不改模擬狀態）。
LLM 不可用時 fallback 成「附當前狀態文字」，不中斷。

> 進階「自然語言介入（封路 / 區域避讓 / 需求突增）」列 P2，會限定在受控動作集。

## 對應程式

| 功能 | 後端 | 前端 |
|------|------|------|
| 模型選擇器 | `llm_config`、`model_registry`、`web/app.py`(`/api/llm/models`)、`web/websocket.py`(`set_llm`)、`engine._llm_init_info` | `index.html`(下拉)、`simulation.js`(`refreshLlmModels`) |
| 分析圖 | `engine.build_analysis`、`metrics`(signal_waiting)、`agent.arrival_cycle`、`websocket._send_analysis` | `charts.js`(`renderAnalysis`)、`app.js`(`analysis`) |
| 暫停對話 | `engine.chat_context`、`llm_server/sim_chat.py`、`websocket._ask` | `index.html`(對話卡)、`simulation.js`(`sendChat`/`appendChat`) |

---

# P1 互動功能

## 4. 可抽換圖層 / 場景（P1⑤）

「場景」把模擬的空間輸入抽象成合約（`scenarios.Scenario`）：county_filter、road_graphml、
population_csv、signals_json、dest_lat/lng + dest_town、map center/zoom。引擎不再寫死台南，
改讀 `scenarios.active()`（`gis_loader` / `road_network` / `signals` / `engine` 都已 honor）。

- 內建場景：`tainan_stadium`（預設，亞太棒球場）、`tainan_station`（同城換事件地點示範）。
- 切換：前端「場景（圖層）」下拉 → `control{action:"set_scenario"}` → 重新初始化引擎 + 重送 init。
- **新城市/尺度**：`python -m llm_abm_simulator.spatial.build_scenario --key … --county … --dest-lat … --dest-lng … --dest-town …`
  下載該縣市 OSM 路網、寫 `data/scenarios/<key>.json` manifest（啟動自動註冊）。⚠ 需 OSMnx+網路；
  換縣市請另備該縣市人口 CSV（`--population`），否則重力需求生成 fallback。
- 協定：`init.scenario`（active / name / list / center / zoom）。
- 與重力需求生成的關係：換場景 → 出生地依該場景的人口分布生成，與 persona 原型不打架（見 `DEMAND_zh-TW.md`）。

## 5. 前端可改 Prompts（P1⑥）

控制面板「✎ 編輯 Prompts」→ Modal 編輯**人物生成 / 決策** prompt，即時生效、可還原預設。

- 後端：`llm_server/prompt_store.py`（register_default / get / set_override / snapshot）；
  `agent_profile` 與 `decision_making` 在呼叫時讀 `prompt_store.get(name)`。
- 協定：`GET /api/prompts`、`control{action:"set_prompt", value:{name,text}}`（text 空＝還原）。
- **安全**：decision 的結構化輸出 schema 仍強制合法 JSON，使用者改壞 prompt 也不會讓解析崩。

## 6. 前端分頁化（P1⑦）

右側面板改為三分頁：**📈 即時**（壅塞/抵達/模式三圖）、**📊 分析**（模擬後分析圖，完成自動跳此頁）、
**💬 對話**（暫停查詢，從左側移入）。`simulation.js` 的 `activateTab()` 控制；進階功能（Prompts）
收進 Modal，主畫面更乾淨（progressive disclosure）。

---

# P2 互動功能

## 7. RAG 知識庫（P2⑧）

控制面板「📚 RAG 知識庫」→ Modal 上傳純文字/markdown/csv → decision 時**每批用當前路況檢索一次**
相關片段注入決策 prompt（grounding LLM 決策在上傳的在地/權威知識）。

- 後端 `llm_server/rag_store.py`：sklearn **TF-IDF（char_wb n-gram，對中文友善）**，免額外依賴、離線可跑。
  `add_text` / `retrieve(query,k)` / `clear` / `stats` / `enabled`。
- 注入點：`decision_making.run_decision_making` 用 `perception_data` 當 query 取 top-3 注入（每批一次，控 token）。
- 協定：`GET /api/rag/status`、`POST /api/rag/add`{name,text}、`/api/rag/clear`、`/api/rag/toggle`。
- 誠實定位：上傳「真正影響決策的知識」（交通管制計畫、疏運手冊）才有意義；可升級為 embedding 檢索（之後）。

## 8. 自然語言介入（P2⑨）

對話分頁切「介入」模式 → 輸入指令（或點 chips）→ 解析成**受限動作集**套用、即時更新地圖：

- 動作：`avoid_area(town)`（避開某區，車輛繞道重算）、`demand_surge(town,count)`（某區湧入 N 台）、`none`。
- 解析 `llm_server/sim_intervene.py`：**關鍵字優先**（確定性、對受限指令最可靠），判不出才用 LLM。
- 引擎：`apply_intervention` / `clear_interventions`——`avoid_area` 在該區形心放**半徑 2500m 避讓圓**(`_avoid_circles`)，
  移動中車重算路徑、圈內邊 **成本 ×25(近乎封路)**；`demand_surge` 新增 **clamp 1–1000** 台事件車(起點該區、終點球場)；`snapshot_now()` 不前進直接回快照供即時更新。
- **只影響事件車**：避讓重算/湧入新增都針對事件車；**背景常態車流不受 NL 介入**。
- 協定：`control{action:"intervene"|"clear_intervention", value}`，回 `chat` + `state_update`。
- 安全：只有受限動作集，NL 不能亂改任何東西；新增的車不會被 clear 移除(避讓區會清)。
- **模型來源**：介入與「暫停對話查詢」的 LLM 解析都用**前端模型選擇器所選的後端+模型**（`llm_client.generate` 不帶 model→用 runtime 值），整套 LLM 共用。

## 9. 網頁上傳自訂場景（P2 upload）

控制面板「⬆ 上傳場景」→ Modal 填 key/name/縣市/目的地 + 選**路網 graphml（本專案格式）** + 選填人口 CSV
→ `POST /api/scenario/upload`（純 JSON 文字，免 multipart）→ 驗證 graphml 含座標屬性 → 寫檔+manifest+註冊
→ 立即出現在場景下拉並切換。

- 限制（誠實）：graphml 須為**本專案格式**（由 `build_scenario`/`build_roads` 產生，節點含 x_m/y_m/lat/lng）；
  不做瀏覽器端任意 OSM 檔解析/建網（那是更大的管線）。大網路受算力限制。
- 換縣市請附該縣市人口 CSV，否則重力需求生成 fallback。

---

# P3 互動功能

## 10. 決策核心選擇器（P3）

控制面板「決策核心（事件車）」兩個按鈕：**規則式（Rule-based）** / **LLM**。這對比本身就是 demo paper
的賣點之一——同一張路網、同一波事件車流，比較 LLM 認知核心 vs 傳統規則核心的繞道行為 / 抵達曲線 / 壅塞。

- 後端：核心登錄於 `decisions/registry.py`（`rule` / `llm`，可擴充）；`engine.last_decision_source` 即核心 key，
  下發前端顯示（`simulation.js` 的 `CORE_LABEL`）。規則式＝`decisions/mock_policy.py`（確定性、零成本、對照基線），
  LLM＝`decisions/llm_adapter.py`（直呼 pipeline）。
- 協定：`control{action:"set_mode", value:"rule"|"llm"}`、`init.config.{decision_cores, current_core}`。
- 切「LLM」只影響**事件車**；背景常態車流永遠走規則式核心（見下）。

## 11. 背景常態交通流（P3）

控制面板「背景常態車流」slider：在事件車之外注入**不指定事件終點的常態背景車**（雙邊重力 OD、
規則式核心、抵達後重生），造成路網基礎負載，讓 LLM 事件車感知到的壅塞更貼近真實。前端以**低調灰小點**
與事件車明顯區隔，圖例 + 顯示/隱藏開關 + 狀態列「背景車 N」。協定：`control{action:"set_ambient", value:N}`、
`init.config.ambient`、`state_update` 每台 agent 帶 `role`、`metrics.ambient_count`。完整設計見 `docs/AMBIENT_zh-TW.md`。

## 12. 兩層交通分析（P3，交通局視角）

「📊 分析」面板分兩層：**① 事件層**（只算事件車：抵達曲線 / 旅行時間 / OD vs 重力期望 / 號誌停等）、
**② 路網層**（事件車＋背景車：車流量隨時間堆疊、服務水準 LOS、Top-N 瓶頸路段 V/C、事件佔路網負載比＝邊際負載）。
就像交通局核發大型活動交評在做的事。後端 `engine._network_analysis`，前端 `charts.js` 的 `renderNetwork`。

## 13. 記憶簡化為單一 memory（P3）

記憶不再分長短期（1 step=1 分鐘，區分無意義），合併為**單一 `memory`**；`summary` 一律由
**確定性模板每步重算**（已移除 LLM 摘要重寫，零 token、可重現）。完整設計見 `docs/MEMORY_zh-TW.md`。

## 14. 大規模渲染：zoom / 可視範圍裁切（P3+）

往 1～2 萬台事件車時,逐台送/畫會讓 WS 流量(~MB/步)與瀏覽器(數萬 marker)垮掉。**自動切換**:

- 車數 ≤ `[ui].render_individual_max`(預設 1500)→ **逐台送/畫**(現有 icon/dot,任何 zoom)。
- 超過 → 依**前端目前 zoom 與可視範圍**裁切:
  - **zoom out（< `[ui].agent_min_zoom`,預設 14）→ 不送車,只看道路壅塞**(道路本來就依壅塞上色)。
  - **zoom in → 只送「可視範圍內」的車**(後端用公尺框過濾,經緯度只算這批)→ 就算總共 2 萬台,畫面內通常幾百台。
- 前端 `map.js` 在 zoom/平移後(節流 250ms)回報 `{zoom, bounds}`(`control{action:"set_view"}`);後端 `engine.set_view`
  存成公尺框,`_visible_agents` 據此決定送哪些車。全域統計/圖表不受影響(由伺服器對全部車算)。
- **zoom/pan 即時顯示**:後端收到 `set_view` 會**立即回推一張 `snapshot_now()`**(不必等下一個慢的模擬步)→ LLM 模式下
  zoom 進去也馬上看到範圍內的車。送訊息加 `asyncio.Lock` 序列化,避免與 run loop 並發 send 撞在一起。
- **渲染可讀性**:道路線寬改**依 zoom 縮放**(細、半透明,不蓋住車);車輛畫在**專屬高 z-index pane(`agentPane`)**
  → 永遠在彩色道路之上;車點依 zoom 放大,背景車灰點調亮(r、opacity 提高)。
- **限制(誠實)**:zoom out 時點不了單一車(本來就只看全局壅塞)。詳見 `docs/SCALING_zh-TW.md` §6。

## 15. 前端自訂時間 + 「套用設定」按鈕（P3+）

控制面板新增**「週期數」slider** 與**「每週期分鐘」下拉**,讓使用者自訂「跑幾個週期、每週期多久」。
範圍由後端 `[ui].steps_min/max/step` 與 `step_minutes_options` 下發(單一真實來源)。

- **預覽 + 一次套用(避免拖滑桿就重設)**:**事件車數 / 背景車流 / 週期數 / 每週期分鐘**這四個改成
  **「拖動只更新數字、不自動生效」**;調整後「**✓ 套用設定**」按鈕會高亮提示,按一下才**一次**送
  `control{action:"apply_config", value:{nb_agents, ambient, max_steps, step_minutes}}` → 後端 `websocket._apply_config`
  **只重新初始化一次**(不再每次微調都重設)。`init` 到達後高亮清除。
- 進行中無法變更(會提示先重設);改 `step_minutes` 會重新初始化(牽涉號誌相位、記憶已行時間、抵達換算)。
- (速度 slider 仍即時生效,因為它不需重新初始化。)
- 後端仍保留 `set_agents`/`set_ambient`/`set_max_steps`/`set_step_minutes` 個別指令(相容),但前端統一走 `apply_config`。

## 16. 決策日誌即時化（P3+，取代讀 txt 檔）

舊設計前端「Decision 輸出」面板**輪詢 `output/*.txt`**——奇怪又脆弱。改成**走 WebSocket 即時推送**:
- 每次 LLM 重決批次後,後端把**解析後的決策**(哪些車 → mode → reason)+ **解析健康度**
  (`triggered / decided / fallback`,fallback 多＝LLM 解析有問題)放進 `state_update`。
- 前端「決策日誌(即時)」面板直接顯示 → **同時滿足「看結果」與「檢核錯誤」**,且不再讀檔。
- 每台車的決策也已併入 **Agent 檢視**(行為模式 + 決策理由 + 「上次重決週期」)。
- **原始 JSON 仍寫在 `output/`** 供離線深度除錯(`output_engine`),但前端不再依賴它。
- 後端 `engine._record_decision_log` / `state.decisions` / `decision_health`;前端 `simulation.js` 的 `updateDecisions`。

## 17. 事件車分批出發（時空需求）（P3+）

真實事件是**陸續抵達**而非全部同時出發。每台事件車有 `departure_cycle`,在那之前「**尚未進場**」
(`waiting_for_origin`:不移動、不算路網流量/壅塞、不顯示),到點才開始跑。出發時間在 `[0, 視窗]` 內依
`profile` 抽樣(seeded、可重現)。狀態列新增「未出發 N」,分析面板的抵達曲線加一條**「每步出發」**對照。
- 設定 `[departure].window_minutes`(0＝全部同時出發＝舊行為,向後相容)、`profile`(uniform / front_loaded / peak)。
- 背景車不分批(本即穩態連續流)。後端 `engine._assign_departures` / `_activate_due_departures`;完整見下方設定與 `OVERVIEW`。

## 18. 前端整體重設計（專業化版面 + 系統日誌 + 多底圖）（P3+）

把前端從「左控制／右圖表夾住地圖」改成 **頂列 + 左控制 + 大地圖 + 可收合底部面板**,並全面提升質感。
**純前端改動,後端邏輯與 WebSocket 介面完全不動;所有元素 ID 保留**(map.js / charts.js / app.js 邏輯不變,
simulation.js 只新增),故不影響可重現性與既有功能(功能 0 刪減,僅移除 emoji 車輛圖示)。

- **版面**:標題「LLM-abm 互動式交通模擬平台」+ 執行心跳 + 連線狀態 + 忙碌進度條(頂列);左側控制面板;
  中央地圖佔版面最大;底部資料面板(即時 / 分析 / 對話 / 日誌 四分頁)可一鍵收合 → 地圖近全屏。
  8 項 KPI 改成地圖上方的浮動玻璃列。
- **色票**:單主色(天藍 `#3FB6FF`)+ 深石板底色三階 + 語義狀態色;**地圖點色 = UI = 圖表**全系統一致
  (移動藍 / 等燈琥珀 / 抵達綠 / 異常灰 / 背景灰;壅塞 綠黃橘紅)。導入 Tabler 圖示字型(取代 UI emoji)
  與 Inter + Noto Sans TC 字型(皆走 CDN,失敗自動退系統字/純文字)。
- **系統日誌(專業 logging)**:底部「日誌」分頁。`simulation.js` 的 `log(level,msg)`,時間戳 + 分級上色
  (info/success/warn/error/step)、自動捲動、上限 200 行、可清除。餵入:操作生命週期、每步摘要、status、
  WS 連線/斷線、逾時、錯誤。與「決策日誌」並列同一分頁。
- **全域忙碌動畫 + 執行心跳**:按下會重新初始化/開跑的重操作 → 頂列進度條左右流動 + 被按按鈕顯示 spinner;
  收到 `init` 或首個 `state_update` 即解除,另設 90 秒保險逾時(`app.js` 的 `beginBusy/endBusy`)。
  心跳列顯示 待命 / 處理中 / 執行中第N步 / 已暫停 / 已完成,使用者一眼知道在不在跑。純前端,不改後端行為。
- **agent 渲染**:移除拉近時的 🚗/🏍️ emoji 圖示(觀感不專業),改為**乾淨彩色圓點**(Uber/Kepler 風):
  狀態用色、車種用大小(汽車大、機車小)、細暗描邊;保留點擊檢視、spiderfy 散開、背景灰點、依 zoom 放大。
- **多底圖切換**:`L.control.layers` 原生控制(右上),免金鑰底圖:CARTO 暗(預設)/淺 Positron / Voyager 街道 /
  OSM / Esri 衛星;切底圖時道路、車流、號誌、行政區等疊加圖層自動保留。
- **地圖色調微調**:地圖右上自訂控制,亮度 / 對比 / 飽和度 sliders(CSS `filter` 套在底圖圖磚層,不影響向量疊加)+ 還原。
- **可調窗格大小**:左控制面板↔地圖、地圖↔底部面板 之間各一條**可拖曳分隔條**(`#gutter-left`/`#gutter-dock`);
  用 CSS 變數 `--left-w`(clamp 240–560px)/`--dock-h`(clamp 120px–70vh)驅動;`pointer` 事件(滑鼠+觸控)、
  **localStorage 記住尺寸**、**雙擊分隔條還原預設**;拖動中/放開呼叫 `TrafficMap.resize()` 讓地圖補滿圖磚;底部面板收合時自動隱藏水平分隔條。
- **地圖尺寸穩健**:用 **`ResizeObserver` 監看地圖容器**,只要實際尺寸一變(版面長好/字型遲到重排/視窗縮放/收合面板/拖曳分隔條)就 `invalidateSize`(rAF 去抖),另加 `window.load` + `fitBounds` 前各補一次 → 徹底解決彈性版面初始化早於版面/字型載入導致的「下半部黑塊」(不靠定時猜)。
- 對應程式:`simulation_web/frontend/`(`index.html` 重排+分隔條、`index.css` 依色票重寫+`--left-w`/`--dock-h`+分隔條樣式、
  `map.js` 彩點+底圖+色調+`resize()`、`simulation.js` 日誌/忙碌/心跳/收合/`setupResizers`、`app.js` 日誌/忙碌/心跳);`charts.js` 圖表色對齊色票。
- 圖示字型 CDN 路徑修正:`@tabler/icons-webfont@2.47.0/tabler-icons.min.css`(該版 CSS 在套件根目錄,非 `dist/`)。
- **規則式核心隱藏 LLM 專屬控件**:決策核心選「規則式」時,**LLM 模型 / 重新生成人物 / Prompts / RAG** 自動隱藏(包進 `.llm-only`,`setMode`/初始化切 `body.mode-llm`,CSS 控制顯示),避免無關控件造成混淆;與核心無關的「上傳場景」移到「模擬設定」卡常駐。
- **文案精簡為正式 demo 版**:前端說明文字(hint)精簡為短標籤,移除 beta 感贅述;完整設計理由保留在本檔各節與 `OVERVIEW`/`PAPER_SYSTEM_DESIGN`。對照:
  - 套用設定 hint → 「拖動上方滑桿後按此套用(進行中需先重設)」(完整行為見 §15)。
  - 背景車流 hint → 「常態背景車流(重力 OD);0＝關閉」(完整見 §11、`AMBIENT`)。
  - 決策核心 hint → 「規則式:確定性基線。LLM:依人格與感知決策」(完整見 §10)。
  - 重新生成人物 hint → 「重生 persona 原型池」(完整見 `OVERVIEW §3.7`)。
  - 決策日誌 hint → 「LLM 壅塞觸發重決的車與原因 + 解析健康度」(完整見 §16;原始 JSON 仍寫 `output/`)。
  - Prompts 說明移除(功能見 §5)、場景/事件車/週期數等自明 hint 移除。

## 19. 散場（egress）+ 前端再優化（P3+）

- **散場疏運評估**（完整見 `EGRESS_zh-TW.md`）：左面板「活動階段」卡（進場 ▸ 散場 + **宣告散場開始**鈕,
  `control{action:"declare_egress"}`）;停留車依 `[egress]` 錯開離場回**居住地**;KPI 加「已返家」;
  分析面板加**③ 散場層**(疏散曲線/散場旅時/返家 OD/**清場時間**)。`charts.renderEgress`、`engine.declare_egress/_handle_egress/_egress_analysis`。
- **規則式核心暗掉**：demo 預設 LLM(`[llm].use_llm=true`),前端規則式按鈕 `disabled` 變暗;規則式仍是 LLM 失敗 fallback、背景車核心、paper baseline(config 可跑)。
- **決策日誌歷史化**：改成**逐步累積**(每步「第 N 步 · 重決 X 台」+ 該步重決車),可往上捲看整場;重設清空(不吃上次模擬)。`simulation.js updateDecisions/resetDecisions`(帶 cycle)。
- **底部面板自適應**：對話/系統日誌/決策日誌內捲區改 flex 撐滿,隨拖動面板高度自適應(不再寫死 max-height)。
- **地圖色調可開關**：右上小鈕(`ti-adjustments`)開/關色調面板,預設收起。`map.addAppearanceControl`(toggle)。
- **滾輪縮放絲滑**：`zoomSnap:0` + 加大 `wheelPxPerZoomLevel` → 連續(小數)縮放、平滑。

## 20. 車流監測器 + GIS 主題圖層匯出（P3+，交通局交付）

- **車流監測器（街景丟人式拖放）**：左面板「車流監測器」卡有一個**可拖曳的相機 icon（pegman）**——
  按住拖到地圖上,**拖曳時全網道路打亮**(像 Google 街景把有覆蓋街道變藍)+中央橫幅提示「拖到路上放開」;
  放開 → 後端 `snap_point` **一律吸附到最近路段**(像街景丟人總落在最近街道;只有最近道路都 >1.5km＝丟到外海/深山才拒絕)→ amber icon 暫存。
  地圖上的監測器 icon **隨比例尺(zoom)美觀縮放**(`zoomend` 重算大小);左面板顯示「**已放置 N 隻監測器**」。
  隨**套用設定**一起送(`apply_config.detectors`)、初始化註冊(紫色 icon)。模擬中以「進入新邊」累積**通過次數**
  (真實流量,與 step_minutes 無關),`車種×來源`交叉表。分析面板「**④ 車流監測器**」:**下拉選**總量/汽車/機車/事件/背景,表格 + 每步通過數曲線。
  `map.js`(`setupDetectorDrag`/`highlightRoadsForPlacement`/`detIcon` 縮放)、`engine._register_detectors/_update_detectors/snap_point`、`websocket snap_detector/export_gis`。
- **GIS 主題圖層匯出**:分析面板「**⑤ 匯出 GIS 圖層**」下拉(LOS / 車流量 / 壅塞 / 監測器 / 全部)+「下載 Shapefile」
  → `export_gis` → 後端 `spatial/gis_export.py` 產 zip(.shp/.shx/.dbf/.prj/.cpg,EPSG:4326)→ `GET /api/gis/<name>` 下載。
  交通局可在 QGIS/ArcGIS 用屬性分類上色。
- **圖表匯出**:每張圖右上「下載 PNG」(直接抓 canvas)、分析面板「下載分析數據 CSV」(時間序列 + 監測器)。
- **誠實**:監測器/匯出皆**被動量測、不改物理、可重現**;LOS/容量為代理映射(非 HCM 校準)。對應 `docs/DATA.md`、`PAPER_SYSTEM_DESIGN §12 ③④`。
