# 環境感知設計（送 LLM 的環境資訊）

本文件說明送給 LLM 決策的「環境資訊」如何設計，讓 LLM 真正看懂環境（塞在哪、會不會更糟、
我接下來要走的路順不順），同時控制 LLM 的 max context。

對應程式碼：
- `src/llm_abm_simulator/simulation/engine.py`（`_environment_summary` 算全域熱點/趨勢、`_refresh_env_labels` / `_road_ahead` 算每車質性標籤）
- `src/llm_abm_simulator/domain/agent.py`（`build_environment_payload` 組裝、質性標籤常數與 helper）
- `src/llm_abm_simulator/config.py` 的 `PerceptionContextConfig`（可調參數）
- `config/simulation.toml` 的 `[perception_context]`（使用者調整入口）
- `src/llm_server/prompts/perception_prompt.txt` / `decision_making_prompt.txt`（LLM 端說明與決策提示）

---

## 1. 為什麼要改

舊版送給 LLM 的環境只有：
- **全域**：`agent_count / destination_town / active_road_count / crowded_road_count /
  average_congestion_proxy` — 全是**全市總量統計**，看不出「塞在哪」。
- **每車局部**：`current_road_id`（機器 ID，無語意）、`congestion_proxy`（裸數字）、
  `nearby_agent_count`、`distance_to_destination_m`。

要選 active_mode（fast / avoid_congestion / tolerate / comfortable / short_distance），
LLM 真正需要、但舊版**完全沒有**的是：
1. **前方路況**——接下來要走的路塞不塞？（avoid vs fast 的決定性資訊）
2. **壅塞的空間分佈**——哪些區在塞？
3. **我自己快不快**——目前速度 vs 自由流？
4. **路的語意**——目前走在哪條路、什麼等級？

---

## 2. 新設計

### 全域環境（每步**只送一份**，不隨 agent 數膨脹）

全域環境分成「給 LLM 決策的精簡版」與「給 recorder/前端/CSV 的完整統計版」兩份，
刻意分離「決策用」與「展示用」：

**給 LLM 的精簡質性版**（`engine._llm_environment`，只留決策相關欄位）：

| 欄位 | 內容 | 來源 |
|---|---|---|
| `cycle` | 目前模擬 cycle | — |
| `destination_town` | 所有 agent 的共同目的地 | cfg |
| `overall_traffic` | 全市整體壅塞：順暢 / 普通 / 壅塞 | `average_congestion_proxy` 套 `[memory]` 的 `feel_*` 轉質性 |
| `congestion_trend` | 改善中 / 持平 / 惡化中 | 本步平均壅塞 vs 上一步（engine 存 `_prev_avg_congestion` 比較，門檻 ±0.02） |
| `congestion_hotspots` | top-K 壅塞**行政區**，每筆 `{town, level, crowded_roads}` | 由 agent 的所在區 + 路況聚合 |

```jsonc
"environment": {
  "cycle": 12,
  "destination_town": "安定區",
  "overall_traffic": "普通",
  "congestion_trend": "惡化中",
  "congestion_hotspots": [
    {"town": "善化區", "level": "壅塞", "crowded_roads": 6},
    {"town": "永康區", "level": "普通", "crowded_roads": 3}
  ]
}
```

**給 recorder/前端/CSV 的完整版**（`engine._environment_summary` 的原始輸出，**不進 LLM**）仍含
`agent_count` / `active_road_count` / `crowded_road_count` / `average_congestion_proxy` /
`elapsed_minutes` 等裸統計，供前端 metrics 面板與 CSV 使用。

> **為什麼這樣分？** 那些裸統計（全場車數、活躍/壅塞道路數、平均 proxy）對「單一 agent 選哪個
> active_mode」幫助不大，卻會佔 LLM context；它們真正的用途是展示與記錄，所以只給前端/CSV，
> 不送 LLM。LLM 只看得懂、也只需要質性的 `overall_traffic` / `congestion_trend` / `congestion_hotspots`。

> **為什麼用 agent 聚合算熱點？** 壅塞只存在於有 agent 的路段（`congestion_proxy = flow/capacity`，
> 而 flow 來自 agent）。所以由 agent 的 `current_town` + `congestion_proxy` 聚合，等價於掃描壅塞路段，
> 但成本是 O(agent 數)、不必掃全路網（約 2.8 萬條邊），也不會乘上 context。

### 每車局部（純質性，丟掉裸 proxy）

| 欄位 | 內容 | 算法 |
|---|---|---|
| `current_road` | 「路名（等級）」取代 `road_id` | `road_name` + 清理後的 `highway`；路名缺時逐階退到等級 / road_id |
| `traffic_here` | 腳下壅塞感：順暢 / 普通 / 壅塞 | `congestion_proxy` 套 `[memory]` 的 `feel_*` 門檻 |
| `speed_status` | 速度感：自由流 / 略慢 / 壅塞緩行 / 已抵達 | `speed_kmh ÷ 速限`，門檻 `speed_free_ratio` / `speed_slow_ratio` |
| `road_ahead` | 前方路況 | 沿 `current_path` 從**下一段**起往前看 `lookahead_distance_m`，找第一個壅塞段 |

```jsonc
"current_road": "南科九路（secondary）",
"traffic_here": "普通",
"speed_status": "略慢",
"road_ahead": "前方約 1.2 公里後壅塞（南科路）"
```

`road_ahead` 是整個設計的核心——它讓 LLM 能「看見前方」、提前選 avoid_congestion，補上舊版
「只知道腳下這條」的盲點。掃描只看前方（不含腳下，腳下已由 `traffic_here` 表示）：累計目前
這條邊的剩餘距離後，逐段檢查 `congestion_proxy ≥ feel_congested_proxy`；找到就回
「前方約 X 公里後壅塞（路名）」，否則回「前方順暢」。

---

## 3. 如何控制 LLM max context

| 槓桿 | 做法 |
|---|---|
| **質性編碼** | 用 `順暢/普通/壅塞`、`自由流/略慢` 等標籤取代多位小數 float，省 token 又好讀（與記憶同一套詞彙） |
| **全域 once、不乘 N** | `congestion_hotspots` / `congestion_trend` 放全域那一份 doc，不在每個 agent 重複 |
| **top-K / look-ahead 上限** | `hotspots_top_k` 限制熱點數、`lookahead_distance_m` 限制前方掃描距離 |
| **丟裸數值** | 每車不再送 `congestion_proxy` 浮點數，改送質性 `traffic_here` |
| **只列有壅塞的區** | `congestion_hotspots` 只放真的有壅塞的行政區，順暢區不佔位 |

---

## 4. 可調參數（`config/simulation.toml` 的 `[perception_context]`）

```toml
[perception_context]
hotspots_top_k = 5            # 全域熱點取前幾個行政區
lookahead_distance_m = 2000  # 每車 road_ahead 往前看的距離（公尺）
speed_free_ratio = 0.8       # speed/速限 ≥ 此值 →「自由流」
speed_slow_ratio = 0.5       # ≥ 此值 →「略慢」；否則「壅塞緩行」
```

質性門檻（順暢/普通/壅塞）沿用 `[memory]` 的 `feel_congested_proxy` / `feel_normal_proxy`，
保持與旅次記憶同一套詞彙。標籤文字是設計常數，定義在 `domain/agent.py`，不從 TOML 調整。

---

## 5. 資料流與相容性

- 算值：engine 每步在 `_refresh_agent_perception` → `_refresh_env_labels` 算好每車的
  `traffic_here / speed_status / road_ahead` 並填回 agent；`_environment_summary` 算全域
  `congestion_trend / congestion_hotspots`。
- 送 LLM：`agent.build_environment_payload()` 組裝每車質性環境；全域環境先經
  `engine._llm_environment()` 瘦身成質性版才放進 `_build_step_payload`（裸統計留給 recorder/前端）。
  perception → decision 兩個 prompt 已同步更新欄位說明與決策提示。
- **確定性**：全為規則運算，不含隨機、不呼叫 LLM，維持「同 seed → 同軌跡」。
- **與實際路徑選擇的關係（重要）**：本文件的環境資訊只影響 **LLM 選哪個 active_mode**。
  真正的改道仍由 `simulation/engine.py` 的「踩到壅塞路才重算」（`is_crowded`）觸發、由
  `spatial/routing.py` 的加權最短路徑決定。也就是說 `road_ahead` 目前是**讓 LLM 提前選避塞
  mode**，而非直接讓 routing 預先繞路；若要「前方壅塞就提前改道」真正生效，需另外調整
  `_move_agent` 的重算觸發條件。
- **不影響**：`output/*.csv`（recorder 獨立）、前端 `AgentSnapshot`、旅次記憶 STM/LTM。
