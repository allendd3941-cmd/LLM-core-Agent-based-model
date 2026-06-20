# 散場（Egress）疏運評估

本文件說明在「進場（ingress：往球場）」之外新增的**散場（egress：離場返家）**階段與其交通評估。
散場往往比進場更尖峰（一窩蜂),是大型活動交通評估的核心價值。

對應程式：
- `config.py` `EgressConfig` + `config/simulation.toml` `[egress]`
- `domain/agent.py`（`phase`/`home_node`/`home_town`/`egress_cycle`/`egress_start_cycle`/`egress_arrival_cycle`/`begin_egress_leg`）
- `mobility/demand.py` `sample_residence`（人口加權抽居住地）
- `simulation/engine.py`（`_assign_home`/`_build_persona_residence`/`declare_egress`/`_handle_egress`/`_egress_analysis`）
- `web/websocket.py`（`declare_egress` 控制指令）
- 前端：`index.html` 活動階段卡 + 已返家 KPI + 分析③；`simulation.js`/`charts.js`/`app.js`

---

## 1. 階段模型（單次模擬內兩階段）
每台**事件車**有 `phase`：
```
ingress（往球場） → dwell（抵達球場停留） → egress（往家） → home（已返家）
```
- 進場抵達球場 → `dwell`（停車、不移動、不算路網流量）。
- **宣告散場**後，停留中的車在散場視窗內陸續離場 → `egress`（重新規劃路徑往家、回到路網）。
- 抵達家 → `home`。
- **背景常態車流不分階段**（全程穩態負載）→ 散場壅塞 = 散場事件車 + 背景車。

## 2. 觸發方式：操作者驅動（event-based，手動）
散場由**前端「宣告散場開始」按鈕**觸發（`control{action:"declare_egress"}` → `engine.declare_egress()`），
等同操作者宣布「比賽結束」。這是刻意的**互動式事件評估**設計：demo 時可邊敘述邊按，觀察散場洪峰即時形成。
> 目前僅手動模式（依使用者決定）。日後可選加「自動排程」（到第幾分自動轉散場）以利批次/可重現實驗。

## 3. 散場終點＝居住地（destination = "residence"）
散場回到每台車的**居住地**（與進場出發地 `origin_town` 可不同）：
- **LLM 核心**：用 persona 的 `residential_location`，以 `response_parser.normalize_town_name` 對應到真實行政區。
- **對應不到 / 規則式車（無 persona）**：以 **`sample_residence`（∝各區人口、與距離無關）** 抽一個居住區當後備。
- 在出生時（`_place_agent`→`_assign_home`）就決定 `home_town`/`home_node`（節點），seeded、可重現。
- → 散場 OD 成為「**人口加權的疏散分布**」（大家散回各自人口比例的住處），是可辯護的空間模型。
- 另可設 `destination = "origin"`＝單純來回程（回進場出生地）。

## 4. 散場錯開（profile）
宣告散場後車輛**不是同一秒全離場**，而是在 `window_minutes` 視窗內依 `profile` 錯開（每台 seeded 抽 `egress_cycle`）：
- **`peak`（預設，一窩蜂）**：絕大多數車在宣告後立刻離場、快速遞減（u²）→ 最尖銳的散場洪峰，最貼近真實散場。
- `uniform`：視窗內平均離場。
- `gradual`：拖長、偏後段（如續攤慢慢散）。
> 切到散場時 `begin_egress_leg` 會**重置該車記憶累積器**，讓散場那一腿的旅次摘要/旅時獨立量測（視為新旅程）。

## 5. 散場層交通評估（`build_analysis` 的 `egress`）
模擬後「📊 分析」面板新增**③ 散場層**（宣告散場後才有資料）：
- **疏散曲線**：累積返家 + 每步離場數。
- **散場旅行時間分布**（分鐘）。
- **返家地分布（OD）**：各區返家車數 Top。
- **頭號指標 — 清場時間（clearance time）**：從宣告散場到 **90% 曾抵達球場的車返家** 所需分鐘數。
- summary：返家率、平均散場旅時、清場時間。
> 路網層（LOS / V·C / 瓶頸 / 車流量）本就含散場車流（`_recompute_flows` 統計所有移動車），故散場壅塞自動納入路網層評估，可與進場對照。

## 6. 設定（`config/simulation.toml` 的 `[egress]`）
```toml
[egress]
destination = "residence"   # residence（回居住地）| origin（回出生地，來回程）
window_minutes = 5          # 宣告散場後的錯開視窗（分鐘）
profile = "peak"            # peak（一窩蜂）| uniform | gradual
carry_ingress_memory = true # true＝散場保留進場累積的旅次記憶（跨旅次記憶，影響散場 LLM 決策）；false＝兩段獨立（ablation）
```

> **跨旅次記憶（2026-06）**：`carry_ingress_memory`（前端「進出場時間型態」卡可即時開關，亦可 `config.set_runtime_egress`）。開啟時 `agent.begin_egress_leg(carry_memory=True)` 不重置進場記憶累積器，散場 LLM 決策因此看得到進場經驗（例如「來時哪裡塞 → 回家避開」）。散場旅時/OD/清場分析用獨立的 `egress_start_cycle / egress_arrival_cycle`，**不受此旗標影響**。搭配「點 agent 看整趟路徑」（`get_agent_path`）可同 seed 開/關對照散場路徑差異。

## 7. 可重現性與誠實限制
- **可重現**：居住地抽樣、散場錯開時間全走注入的 seeded RNG → 同 seed 同軌跡。
- **限制（paper 要標清）**：
  1. **手動觸發**（目前無自動排程；批次實驗需日後補）。
  2. 散場視為**單一返家旅次**；未模型化「續攤/轉乘/多目的」。
  3. 居住地：LLM persona 文字可能雜（已正規化）；規則式/對不到者用人口加權後備 → 散場 OD 是「人口加權疏散」近似。
  4. `max_steps` 須足以涵蓋進場 + 停留 + 散場，否則散場/清場時間量不完整（前端可調週期數）。
  5. 全台南路網下，遠區的家也有真實節點（不再被邊界吸附）——前提是已用全台南路網（見 `DATA.md`）。

## 8. 論文框架（建議）
- 把系統定位成「**事件交通數位分身**」：同一場模擬涵蓋**進場洪峰 + 散場洪峰**兩個尖峰,輸出交通局視角的兩層×兩階段評估。
- 散場的 **清場時間** 與 **進場 vs 散場對照（LOS/V·C/瓶頸）** 是大型活動交評最關心、也最有賣點的量化結果。
