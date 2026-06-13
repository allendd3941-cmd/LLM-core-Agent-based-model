# 背景常態交通流（Ambient Traffic）與兩層交通分析

本文件說明在「去事件地點（球場）的事件車流」之外，注入的**不指定事件終點的常態背景車流**，
以及最終分析如何把背景＋事件車一起納入「路網層」評估（像交通局做大型活動交評）。

對應程式碼：
- `src/llm_abm_simulator/mobility/demand.py`（`sample_od_pairs` 雙邊重力 OD、`sample_dest_town` 重生）
- `src/llm_abm_simulator/domain/agent.py`（`role` 欄位：`"event"` / `"ambient"`）
- `src/llm_abm_simulator/simulation/engine.py`（`_build_ambient_agents` / `_respawn_arrived_ambient` /
  事件 KPI 與路網層拆分 / `build_analysis` 的 `_network_analysis`）
- `src/llm_abm_simulator/config.py` 的 `AmbientConfig` + `config/simulation.toml` 的 `[ambient]`

---

## 1. 為什麼需要背景車流

真實的事件壅塞不是只有「去球場的車」造成的——城市本來就有常態車流。沒有背景流，LLM agent
感知到的壅塞只反映**彼此**，不像真實路況，digital twin 的可信度就站不住。背景常態車流提供一個
**壅塞場（congestion field）**，讓 LLM 的繞道 / 避塞決策變得有意義；也讓最終分析能像交通局一樣，
評估「這場活動疊加在日常車流之上」對路網的衝擊。

---

## 2. 設計：對齊運輸規劃四步驟模型

背景車不是「隨機亂跑的車」，而是用審稿人熟悉的需求模型語言描述：

1. **Trip generation（產生）**：背景車以穩態方式在路網上維持一定數量（`[ambient].count`），
   抵達後換新 OD 重生（`respawn`）→ 等效於連續的背景流入率。
2. **Trip distribution（分布）**：起訖以**雙邊重力模型**在鄉鎮對之間抽樣（`demand.sample_od_pairs`）：
   - 起點 i ∝ `population_i`
   - 終點 j ∝ `population_j × f(d_ij)`，且 j≠i；`f(d)=exp(−beta·d_km)` 或 `d_km^(−beta)`
     （沿用 `[demand].beta/decay`）
3. **Assignment（指派）**：在**同一張路網**上用規則式核心走最短/策略路徑 → 產生 link flow / congestion。
4. **Performance（績效）**：讀路網層績效（見第 4 節）。

重生（`_respawn_arrived_ambient`）不 teleport：以目前所在節點為新起點、依距離衰減（`sample_dest_town`）
抽新目的地，像真實駕駛完成一趟再啟程，維持穩態背景負載。

**核心定位**：背景車**一律走規則式核心、不吃 LLM、不存記憶、不可被 inspect**——它是路網上的負載來源。
這讓成本可控（不可能對數百台背景車跑 LLM），論文敘事也乾淨：
**「LLM 事件參與者，穿越由輕量規則核心模擬的常態背景車流」**。

無人口資料（`data/gis/town_population.csv` 全 0，或換縣市未附人口）→ `sample_od_pairs` 回 None → 背景車流自動停用。

---

## 3. 前端如何區分事件車 vs 背景車

- **事件車**：鮮明狀態色（移動🚗藍 / 等紅燈🚦琥珀 / 已抵達🏁綠）的 emoji 圖標或彩點，可點選 inspect。
- **背景車**：低調**灰色半透明小點**、不可點選——一眼就能和事件車區分。
- 圖例新增「背景常態車流」一條 + 「🚙 背景車流」顯示/隱藏開關；狀態列新增「背景車 N」。
- icon/dot 模式由**事件車數**決定（背景車一律小灰點），確保背景車再多也不影響事件車可讀性。
- 左側「背景常態車流」slider 可即時調整數量（0＝關閉），範圍 `0..[ambient].max_count`。

---

## 4. 兩層交通分析（`build_analysis`）

模擬完成後的「📊 分析」面板分兩層：

### ① 事件層（只算事件車）
這場活動的疏運表現：抵達曲線（累積 / 每步抵達率）、旅行時間分布、出發地 OD（實際 vs 重力期望）、號誌停等。

### ② 路網層（事件車＋背景車，交通局視角）
`_network_analysis` 把背景＋事件一起納入：
- **路網車流量隨時間**：事件 vs 背景（堆疊），看事件如何疊加在背景負載上。
- **服務水準 LOS**：由平均/尖峰 `congestion_proxy` 粗映射成 A–F 等級（A 最順、F 壅塞）。
- **Top-N 瓶頸路段**：整趟尖峰壅塞最高的路段（路名、V/C＝尖峰車流/容量、LOS、尖峰車流/容量）——
  交通局報告最常見的產出（`engine._road_peak` 全程累積尖峰）。
- **路網負載占比（事件 / 背景）**：以「車·步」累積量算出事件車佔整體路網負載的比例（邊際負載）——
  量化「這場活動讓路網多承擔多少」，不需另跑一次基線模擬。

> 路網層的壅塞/流量本來就含背景車（`_recompute_flows` 統計所有移動中的車）；事件 KPI 則只算 `role=="event"` 的車。

---

## 5. 可調參數（`config/simulation.toml` 的 `[ambient]`）

```toml
[ambient]
enabled = true       # 是否注入背景常態車流
count = 40           # 穩態背景車數（前端 slider 可在 0..max_count 覆寫）
respawn = true       # 抵達後以新 OD 重生，維持穩態（false＝抵達即停）
max_count = 600      # 前端/介入可設的背景車上限（保護效能）
```

執行期可由前端「背景常態車流」slider 即時覆寫（`control{action:"set_ambient"}` → `config.set_runtime_ambient_count`，
模擬進行中需先重設）。換縣市請一併附該縣市人口 CSV，否則背景流 fallback 停用。

> **開場成本（誠實）**：初始化要為**每台車**（事件＋背景）各算一次起點→目的地路徑（Dijkstra），
> 所以背景車越多、開場與每次重設越久（實測 Tainan 路網約每台 0.4s；80 台約 40s，40 台約 20s）。
> 預設 `count=40` 取「可見的背景負載」與「開場可接受」的平衡；要更擬真可調高，但會犧牲載入速度。

---

## 6. 與既有機制的關係

- **可重現性**：背景車的 OD 抽樣、重生全走注入的 seeded RNG，同 seed 同軌跡仍成立。
- **記憶**：背景車不存記憶（`memory == {}`），不進 `_summarize_memory`、不寫旅次摘要。
- **決策核心切換**：切「LLM」只影響**事件車**；背景車永遠規則式（見 `docs/DEMO_FEATURES_zh-TW.md` 決策核心選擇器）。
- **NL 介入**：`demand_surge`（某區湧入 N 台）新增的是 `role="event"` 的車（事件參與者）；背景車不受 NL 介入移除。
