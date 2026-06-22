# Action Mode 設計（數值 + 路徑策略）

本文件記錄三種 `action_mode` 的設計：每個 mode 的數值權重、以及讓它們走出**不同路徑選擇方式**的策略旗標。所有數值集中在 `config/simulation.toml` 的 `[action_modes.*]`，可自行調整。

---

## 1. 背景：action_mode 是什麼

每個 vehicle agent 在每個 cycle 會有一個 `action_mode`（由 mock 規則或 LLM 決定），代表它當下的移動取向。共三種（語意定義在 `src/llm_server/prompts/decision_making_prompt.txt`）：

| mode | 語意 |
|---|---|
| `fast` | 想要快一點 |
| `tolerate_congestion` | 繼續塞車也沒關係、不改道 |
| `avoid_congestion` | 避開壅塞、改道繞行 |

> 註：`short_distance` 與 `comfortable` 已移除。兩者都需要 UXsim 沒有的成本維度——`short_distance` 要距離成本、
> `comfortable` 要路型偏好（走大路）——而 UXsim 路徑選擇**唯一的方向來源是「最短時間樹」**。任何路型/距離篩選
> 只要時間樹的下一步不在篩選集，替代邊 route_pref=0、車輛會在路口亂選而到不了（實測 comfortable：prefer 幹道 1/60、
> avoid 小路 2/60 抵達）。在「不自算路徑（零 Dijkstra）」前提下無法實現，故移除，保留三個純參數可實現的模式。
> UXsim 後端的 3 模式 → UXsim 參數映射見 `docs/UXSIM_MIGRATION_zh-TW.md` §5.6。

mock / LLM **只回傳 mode 名字字串**；套用時（`VehicleAgent.apply_action_mode`）會依名字查 `ACTION_MODE_PROFILES` 表，帶入對應的數值與路徑策略。

---

## 2. 原始（基礎）路徑選擇

路徑規劃在 `src/llm_abm_simulator/spatial/routing.py` 的 `find_path`，用 **Dijkstra**（`networkx.shortest_path` + 自訂成本函式）。每條邊的基礎成本是四個權重的加權和：

```
edge_cost = length × (
    w_time     · (length / speed) / 100      # 旅行時間（慢=貴）
  + w_distance · 1                            # 距離
  + w_comfort  · (1 + congestion × 1.5)       # 壅塞降舒適
  + w_capacity · (1 + congestion × 2.0)       # 壅塞=貴
)
```

- `w_*` 為 agent 的四個權重，相對大小決定取向。
- 壅塞（`congestion_proxy`，即 flow/capacity）只透過 comfort / capacity 兩項進入成本。
- engine 在 agent `is_crowded`（所在道路 congestion_proxy ≥ `crowded_road_threshold`）時會重算一次路徑。

> 改版前：四個權重對所有 agent 其實都一樣（mock/LLM 只回名字、不改數值），且 `route_randomness` 與 `road_type_preference` 是死欄位 → 全員同一種走法。本設計修正此問題。

---

## 3. 兩層設計：數值 + 策略旗標

每個 mode = 一組**數值**（四個權重 + 期望速度）+ 一組**路徑策略旗標**。策略旗標是新增的「不同走法」核心，**預設皆為關閉**，全關時 `find_path` 行為等同上面的基礎最短路徑（向後相容）。

| 策略旗標 | 作用 | 關閉值 |
|---|---|---|
| `congestion_penalty` | 額外壅塞懲罰：成本 ×(1 + penalty × congestion) | `0.0` |
| `avoid_threshold` | `congestion_proxy >` 此值的邊成本 ×25（近乎封路、硬避開） | `1.0`（proxy 上限 1，故停用） |
| `road_class_bias` | `>0`：幹道成本打折 ×(1−bias)、小路加罰 ×(1+bias)（偏好大路） | `0.0` |
| `recompute_on_crowded` | 壅塞時是否重算路徑；`false` = 路徑定了走到底 | `true` |
| `route_randomness` | 每邊成本隨機微擾 ±值（seeded，可重現），分散車流 | `0.0` |

---

## 4. 三種 mode 的設定（預設值）

數值表（四個 weight 各列加總 ≈ 1，方便比較取向）：

| mode | desired_speed | time | distance | comfort | capacity |
|---|---|---|---|---|---|
| `fast` | 55 | **0.70** | 0.20 | 0.05 | 0.05 |
| `tolerate_congestion` | 45 | 0.55 | 0.30 | 0.10 | 0.05 |
| `avoid_congestion` | 38 | 0.20 | 0.10 | 0.25 | **0.45** |

策略旗標：

| mode | congestion_penalty | avoid_threshold | road_class_bias | recompute_on_crowded | route_randomness |
|---|---|---|---|---|---|
| `fast` | 0 | — | — | true | 0.05 |
| `tolerate_congestion` | 0 | — | — | **false** | 0.05 |
| `avoid_congestion` | **3.0** | **0.6** | — | true | **0.20** |

### 各 mode 的「走法」一句話總結

- **fast** — 最短時間路徑：無視壅塞，走自由流最快路線（塞了會重算找更快的）。
- **tolerate_congestion** — 時間優先但**不繞路**：成本同 fast，但塞車不重算，路徑走到底。
- **avoid_congestion** — **避塞**：壅塞重罰 + 對高壅塞邊（>0.6）硬避開 + 積極重算 + 高隨機分散。

> 注意：`avoid` 的繞路、`tolerate` 的不繞路等差異，**主要在模擬跑出實際壅塞後才會明顯**。零壅塞時（剛開始）各 mode 可能走相同路線。

> **UXsim 後端**（`LLM_ABM_ENGINE=uxsim`，現為預設）：上表是 legacy 引擎的成本權重觀點；UXsim 後端改把這 3 個 mode
> 映射到 UXsim 既有的路徑選擇參數（`fast`=純最短時間樹、`avoid_congestion`=`set_links_avoid` 壅塞邊、
> `tolerate_congestion`=凍結時間樹不再改道）。行為一致點：**decision 一改變，下一步就重算該車的路徑選擇**。
> `road_class_bias` 旗標已無 mode 使用（隨 comfortable 移除）。完整映射表見 `docs/UXSIM_MIGRATION_zh-TW.md` §5.6。

---

## 5. 資料流（如何串起來）

```
config/simulation.toml [action_modes.*]
    │ tomllib 載入
    ▼
config.ACTION_MODE_PROFILES: dict[str, ActionModeProfile]
    │ 依名字查表
    ▼
VehicleAgent.apply_action_mode("avoid_congestion")     # mock/LLM 只回名字
    │ 套用數值 + 策略旗標到 agent 欄位
    ▼
VehicleAgent.routing_strategy()  ──►  routing.find_path(..., strategy, seed)
                                 └─►  engine 依 recompute_on_crowded 決定塞車是否重算
```

關鍵檔案：
- `src/llm_abm_simulator/config.py` — `ActionModeProfile` 資料模型、`_DEFAULT_ACTION_MODE_PROFILES`、`_build_action_mode_profiles`、`ACTION_MODE_PROFILES`。
- `src/llm_abm_simulator/domain/agent.py` — 策略欄位、`apply_action_mode`/`_apply_named_profile`、`routing_strategy`。
- `src/llm_abm_simulator/spatial/routing.py` — strategy-aware `find_path`（含 `_edge_jitter`、road-class、硬避開）。
- `src/llm_abm_simulator/simulation/engine.py` — 初始指派套 profile、重算讀 `recompute_on_crowded`、傳 `seed`。

---

## 6. 怎麼調整

改 `config/simulation.toml` 的 `[action_modes.<mode>]` 任一值，重設模擬即生效（即時，不需重建路網）。範例：

```toml
[action_modes.avoid_congestion]
congestion_penalty = 5.0     # 想更激進地避塞
avoid_threshold = 0.5        # 更早就把路段視為「該避開」
route_randomness = 0.30      # 車流分得更散
```

- 缺某個 key 會回退到該 mode 的程式碼預設；缺整個 mode 會用內建預設。
- TOML key 名稱 == `ActionModeProfile` 欄位名；要加新策略旗標＝dataclass 加欄位 + `find_path` 讀它 + TOML 加同名一行。

---

## 7. 可重現性

`route_randomness` 的微擾用穩定 hash（`zlib.crc32` 於 `(u, v, seed, agent_id)`）產生，**不是** live RNG，因此：
- 同 `seed` 兩次執行 → 完全相同軌跡（核心不變式，已測）。
- 不同 agent（salt = `agent_id`）即使同 mode 也會走散，避免全部擠同一條路。

---

## 8. 已知限制

- 各 mode 的差異需要模擬跑出壅塞後才完整顯現；初期低壅塞時差異有限。
- `congestion_penalty` 與 comfort/capacity 權重的壅塞項會疊加，這是刻意的強化（如 avoid 同時拉高 capacity 與 penalty），調整時留意兩者交互。
- 數值為 demo 取向的經驗值，非實證標定；若要對應真實行為需另以資料校準。
