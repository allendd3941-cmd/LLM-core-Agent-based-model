# 旅次記憶設計（單一 memory）

本文件說明 agent「旅次記憶」的設計。目標是讓回傳給 LLM 的記憶**像真實人類在回想這趟旅程**，
而不是逐格回放每一步的原始量化數值。

> **2026-06 變更**：記憶**不再分長短期（STM/LTM）**，合併為**單一 `memory`**。
> 因為本模擬 **1 step = 1 分鐘**，「長期 / 短期」的人類記憶隱喻在這個時間尺度沒有意義、
> 也讓 paper 不好說明。同時 LLM 摘要的時機由「每 N 步」改為**「事件車重新決策時」**重寫一次
> （記憶恰好在做決定的當下最新，也省 LLM）。

對應程式碼：
- `src/llm_abm_simulator/domain/agent.py`（`memory` 欄位、`update_memory`、`memory_facts`、質性轉換）
- `src/llm_abm_simulator/config.py` 的 `MemoryConfig`（質性門檻）與 `SummaryConfig`（後備摘要模型）
- `config/simulation.toml` 的 `[memory]` / `[summary]` 區段
- `src/llm_abm_simulator/simulation/engine.py` 的 `_summarize_memory`（LLM 核心、重決策時重寫 summary）
- `src/llm_server/perception.py`（確定性模板：把 `memory.summary` 等組成文字給 decision，不另呼叫 LLM）
- `src/llm_server/memory_summary.py` + `prompts/memory_prompt.txt`（LLM 摘要器）

> 背景常態車流（ambient）**不存記憶**（`memory == {}`）；記憶只屬於事件車。見 `docs/AMBIENT_zh-TW.md`。

---

## 1. 為什麼是這樣

人不會記「nearby_agent_count=7、congestion_proxy=0.43」，也不會記機器路段 ID；
只會記「那邊很塞 / 一路很順、換過幾次路、走了大概多久」。所以記憶用**固定大小**的單一物件，
每步「更新」而非「累加」（取代舊的無上限成長清單），並把量化感知轉成**質性印象**。

---

## 2. 單一 memory 的欄位

每步由內部累積器確定性重算，固定大小：

```json
{
  "summary": "從官田區出發前往安定區；一路大致順暢；曾在善化區一帶遇到壅塞；中途換了 2 次策略，目前採「avoid_congestion」；正在接近目的地；已行進約 30 分鐘。",
  "step": 30,                    // 目前 cycle
  "where": "善化區・南科九路",     // 行政區・路名（人記得到的粒度）
  "traffic_feel": "普通",         // 順暢 / 普通 / 壅塞（當下質性）
  "mode_used": "fast",           // 目前採用的 active mode
  "moved": "前進中",              // 前進中 / 緩慢 / 停滯（當下質性）
  "getting_closer": true,        // 是否比上一步更接近目的地（趨勢）
  "remaining": "約 7.6 公里",     // 粗略距離（人講大概）
  "elapsed": "約 30 分鐘（30 步）",
  "congested_spots": ["善化區"],  // 印象中塞過的地點（去重、上限見 config）
  "mode_switches": 2,            // 整趟換過幾次策略
  "overall_smoothness": "順暢"    // 整趟順暢度：順暢 / 中等 / 不順
}
```

`summary` 是這趟到目前為止的一段自然語言印象；其餘是「當下印象」＋「整趟聚合量」。

---

## 3. 量化 → 人類印象的轉換

每步的原始量化感知，用 `MemoryConfig` 的門檻轉成質性標籤：

| 印象欄位 | 來源量化值 | 轉換門檻（TOML `[memory]`） |
|---|---|---|
| `traffic_feel` | `congestion_proxy` 或 `is_crowded` | `≥ feel_congested_proxy` → 壅塞；`≥ feel_normal_proxy` → 普通；否則 順暢 |
| `moved` | 當步移動公尺 | `< moved_stalled_m` → 停滯；`< moved_slow_m` → 緩慢；否則 前進中 |
| `overall_smoothness` | 整趟平均 `congestion_proxy` | `≥ smoothness_rough_proxy` → 不順；`≥ smoothness_mid_proxy` → 中等；否則 順暢 |
| `where` | `current_town` + `current_road_name` | 兩者皆有 →「行政區・路名」；只有區 → 區；路名空（OSM NAME 常缺）退回 road_id |

> 標籤文字（順暢/普通/壅塞…）是**設計常數**，與 prompt 語意綁定、定義在 `agent.py`，不從 TOML 調整；
> TOML 只調**數值門檻**。

---

## 4. `summary` 怎麼生成

每步把「剛走完的那一步」摺疊進幾個內部累積器（`agent.py` 的 `_` 開頭欄位，不外送）：
`_mode_switch_count`（換 mode +1）、`_congested_spots`（壅塞時把 `where` 去重加入，上限 `congested_spots_max`）、
`_smoothness_sum/_smoothness_n`（平均壅塞 → `overall_smoothness`）、`_start_cycle`（→ `elapsed`）。

`summary` 的來源依**決策核心**而定：

- **規則式核心**：`summary` 由累積器**用模板確定性拼出**（`_compose_summary`）：
  出發/目的地 → 整趟順暢度 → 塞過的地點 → 換策略次數 → 接近/抵達 → 已行進時間。
  零額外 token、**可重現**（全程無隨機、不呼叫 LLM，維持「同 seed → 同軌跡」）。

- **LLM 認知核心**：`summary` **只在該事件車「重新決策」時**由小模型重寫一次
  （`engine._summarize_memory` → `llm_server.memory_summary.run_memory_summary`）：
  - 把該批 agent 的 `memory_facts()`（出發/目的地、塞過的點、換策略次數、整趟順暢度、已行進時間、
    目前狀態）包成一個 prompt 批次呼叫，回 `{agent_id: 一句摘要}`，只「把事實寫成一句話」、不得新增事實。
  - 時機與**決策觸發**一致：記憶在 agent 要做決定的當下最新，且不每步、不對全車 → 省 LLM。
  - 設定好後 `update_memory` 會**保留**該 LLM 摘要（不被模板覆蓋），直到下次重決策才更新；
    尚未被 LLM 摘要過的車（整趟順暢、沒觸發重決）則持續用模板，**記憶永遠有內容**。
  - **fallback**：匯入/呼叫/解析任一失敗 → 保留既有模板摘要，不中斷。
  - **不影響模擬物理**：決策物理不依賴 summary 文字，故同 seed 同軌跡仍成立；只有摘要文字非確定。

`agent.summary_source`（`"template"` / `"llm"`）隨 snapshot 送前端，inspect 面板摘要旁顯示「（模板）」或「（LLM 摘要）」。

---

## 5. 可調參數

```toml
[memory]                       # 質性門檻
feel_congested_proxy = 0.6
feel_normal_proxy = 0.3
moved_stalled_m = 50.0
moved_slow_m = 200.0
smoothness_rough_proxy = 0.6
smoothness_mid_proxy = 0.3
congested_spots_max = 5        # 記憶最多記幾個塞過的地點
distance_decimals = 1

[summary]
summary_model = "llama3.1:8b"  # 後備摘要模型（整套 LLM 一般共用前端所選模型；取不到才用這個）
```

> 已**移除** `use_llm_summary` / `summary_every_n_steps`：摘要是否由 LLM 生成，現在直接由
> **決策核心**決定（LLM 核心才有 LLM 摘要）；時機固定為「重決策時」，不再每 N 步。

改完存檔、重啟伺服器即生效；缺值自動回退到 `MemoryConfig`/`SummaryConfig` 程式碼預設。
載入時會檢查 `[memory]` 門檻合理性（如 `feel_normal_proxy ≤ feel_congested_proxy`），不合理直接報錯。

---

## 6. 資料流與相容性

- 寫入：`engine.step()` 每步對**事件車**呼叫 `agent.update_memory(cycle, step_minutes, MEMORY_CONFIG)`。
- 送 LLM：`agent.build_api_payload()` 送**單一 `memory`** 欄位（取代舊的 `short_term_memory` / `long_term_memory`）。
- 送決策：`run_perception`（確定性模板，不呼叫 LLM）讀 `memory.summary` 組成感知文字，交給 `decision_making` prompt。
- 前端 inspect 的「旅次摘要」讀 snapshot 的 `trip_summary`（＝ `memory.summary`）＋ `summary_source`。
- **不受影響**：`output/agent_memory.csv`（recorder 直接從 agents 輸出）；可重現性（更新全為確定性）。
- persona 生成（`agent_profile`）是 **prompt 驅動、不做 schema 驗證**，且 persona prompt 不含記憶欄位，
  因此與此處的執行期旅次記憶完全是兩條獨立路徑（已移除未使用的 `schemas/agentprofile_schema.py` 死碼）。
