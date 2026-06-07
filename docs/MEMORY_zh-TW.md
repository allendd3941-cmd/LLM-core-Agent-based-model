# 旅次記憶設計（Short-Term / Long-Term Memory）

本文件說明 agent「旅次記憶」的設計方法。目標是讓回傳給 LLM 的記憶**像真實人類在
回想上一段旅程**，而不是逐格回放每一步的原始量化數值。

對應程式碼：
- `src/llm_abm_simulator/domain/agent.py`（記憶欄位、`update_memory`、質性轉換）
- `src/llm_abm_simulator/config.py` 的 `MemoryConfig`（可調門檻）
- `config/simulation.toml` 的 `[memory]` 區段（使用者調整入口）
- `src/llm_server/prompts/perception_prompt.txt`（LLM 端的記憶結構說明）

---

## 1. 為什麼要改

舊版每個 agent 有一個 `travel_memory: list[dict]`，每個 step 把當步快照
（`cycle / current_town / current_road_id / active_mode / vehicle_type / route_status /
nearby_agent_count / congestion_proxy / distance_to_destination_m`）**append** 進清單，
整串送進 LLM。問題：

1. **無上限成長**：步數越多清單越長，欄位大量重複。
2. **欄位攏餘 / 不可記憶**：`vehicle_type` 整趟不變、`current_road_id` 是機器路段 ID
   （人不會記路段編號）。
3. **量化數字不像記憶**：人不會記「nearby_agent_count=7、congestion_proxy=0.43」，
   只會記「那邊很塞 / 一路很順」。
4. **沒有壓縮 / 遺忘**：人類記憶是「上一刻記得清楚 ＋ 整趟一個模糊印象」，
   不是逐格保留。

---

## 2. 新設計：STM + LTM 兩段固定大小的記憶

把成長式清單換成兩個**固定大小**的物件，每步「更新」而非「累加」：

| | Short-Term Memory（STM） | Long-Term Memory（LTM） |
|---|---|---|
| 內容 | 只記「上一步」的人類化印象 | 整趟壓縮成「一段印象」＋ 少量聚合量 |
| 更新方式 | 每步直接覆蓋 | 每步由內部累積器確定性重算 |
| 大小 | 固定（7 個欄位） | 固定（5 個欄位） |

### STM 欄位（`short_term_memory`）

```json
{
  "step": 12,                    // 上一步 cycle
  "where": "善化區・南科九路",     // 行政區・路名（人記得到的粒度）
  "traffic_feel": "普通",         // 順暢 / 普通 / 壅塞（質性）
  "mode_used": "fast",           // 上一步採用的 active mode
  "moved": "前進中",              // 前進中 / 緩慢 / 停滯（質性）
  "getting_closer": true,        // 是否比上一步更接近目的地（趨勢）
  "remaining": "約 7.6 公里"      // 粗略距離（人講大概）
}
```

### LTM 欄位（`long_term_memory`）

```json
{
  "trip_summary": "從官田區出發前往安定區；一路大致順暢；曾在善化區一帶遇到壅塞；中途換了 2 次策略，目前採「avoid_congestion」；正在接近目的地；已行進約 30 分鐘。",
  "elapsed": "約 30 分鐘（30 步）",
  "congested_spots": ["善化區"],   // 印象中塞過的地點（去重、上限見 config）
  "mode_switches": 2,             // 整趟換過幾次策略
  "overall_smoothness": "順暢"     // 整趟順暢度：順暢 / 中等 / 不順
}
```

---

## 3. 量化 → 人類印象的轉換方法

每步的原始量化感知，用 `MemoryConfig` 的門檻轉成質性標籤：

| 印象欄位 | 來源量化值 | 轉換門檻（TOML `[memory]`） |
|---|---|---|
| `traffic_feel` | `congestion_proxy` 或 `is_crowded` | `≥ feel_congested_proxy` → 壅塞；`≥ feel_normal_proxy` → 普通；否則 順暢 |
| `moved` | 當步移動公尺 | `< moved_stalled_m` → 停滯；`< moved_slow_m` → 緩慢；否則 前進中 |
| `overall_smoothness` | 整趟平均 `congestion_proxy` | `≥ smoothness_rough_proxy` → 不順；`≥ smoothness_mid_proxy` → 中等；否則 順暢 |
| `where` | `current_town` + `current_road_name` | 兩者皆有 →「行政區・路名」；只有區 → 區；路名空（OSM NAME 常缺）退回 road_id |

> 標籤文字（順暢/普通/壅塞…）是**設計常數**，與 prompt 語意綁定，定義在 `agent.py`，
> 不從 TOML 調整；TOML 只調**數值門檻**。

---

## 4. LTM 怎麼「壓縮成一段」（方式 A：模板生成）

LTM 不重看全部歷史，而是每步把「剛走完的那一步」摺疊進幾個內部累積器
（`agent.py` 的 `_` 開頭欄位，不外送）：

- `_mode_switch_count`：active_mode 改變時 +1
- `_congested_spots`：`traffic_feel == 壅塞` 時把 `where` 加入（去重、上限 `congested_spots_max`）
- `_smoothness_sum / _smoothness_n`：累計平均 `congestion_proxy` → `overall_smoothness`
- `_start_cycle`：起算步數 → `elapsed`

`trip_summary` 那一段話由這些累積器**用模板確定性拼出**（`_compose_summary`），
依序組合：出發/目的地 → 整趟順暢度 → 塞過的地點 → 換策略次數 → 接近/抵達 → 已行進時間。

**為什麼選模板而非每步呼叫 LLM 摘要？**
- 零額外 token 成本。
- **可重現**：全程無隨機、不呼叫 LLM，維持「同 seed → 同軌跡」。
- 之後若要更自然的語句，只需把 `trip_summary` 這一句升級成 LLM 生成，其他結構化欄位不動。

---

## 5. 可調參數（`config/simulation.toml` 的 `[memory]`）

```toml
[memory]
feel_congested_proxy = 0.6   # 當步壅塞感門檻
feel_normal_proxy = 0.3
moved_stalled_m = 50.0       # 當步移動感門檻（公尺）
moved_slow_m = 200.0
smoothness_rough_proxy = 0.6 # 整趟順暢度門檻
smoothness_mid_proxy = 0.3
congested_spots_max = 5      # LTM 最多記幾個塞過的地點
distance_decimals = 1        # remaining 距離小數位數
```

改完存檔、重啟伺服器即生效；缺值自動回退到 `config.py` 的 `MemoryConfig` 程式碼預設。
載入時會檢查門檻合理性（如 `feel_normal_proxy ≤ feel_congested_proxy`），不合理直接報錯。

---

## 6. 資料流與相容性

- 寫入：`engine._step()` 每步呼叫 `agent.update_memory(cycle, step_minutes, MEMORY_CONFIG)`
  （取代舊的 `travel_memory.append(build_memory_entry(...))`）。
- 送 LLM：`agent.build_api_payload()` 改送 `short_term_memory` / `long_term_memory`
  兩個欄位（取代舊的 `memory` 清單）。
- LLM 端：`run_perception` 把整包 payload 字串接在 `perception_prompt.txt` 後；
  prompt 已同步改為描述 STM/LTM 結構。
- **不受影響**：
  - `output/agent_memory.csv`（由 recorder 直接從 agents 輸出，不依賴 `travel_memory`）。
  - 可重現性（更新全為確定性運算）。
  - `llm_server/server.py` 的舊 `GamaAgent.travel_memory`（GAMA 時代欄位，
    `extra="allow"`；模擬器實際走 `agents_status` raw dict，不經此模型，故保留不動）。
  - `analysis/analyze_agents.py` 的 `memory.short_term_memory`（驗的是 **agent profile**
    JSON，與每步旅次記憶不同路徑）。
