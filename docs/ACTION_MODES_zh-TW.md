# Active Mode 設計（數值 + 路徑策略）

本文件記錄五種 `active_mode` 的設計：每個 mode 的數值權重、以及讓它們走出**不同路徑選擇方式**的策略旗標。所有數值集中在 `config/simulation.toml` 的 `[active_modes.*]`，可自行調整。

---

## 1. 背景：active_mode 是什麼

每個 vehicle agent 在每個 cycle 會有一個 `active_mode`（由 mock 規則或 LLM 決定），代表它當下的移動取向。共五種（語意定義在 `src/llm_server/prompts/decision_making_prompt.txt`）：

| mode | 語意 |
|---|---|
| `fast` | 想要快一點 |
| `tolerate_congestion` | 繼續塞車也沒關係 |
| `avoid_congestion` | 避開壅塞 |
| `comfortable` | 穩定舒適 |
| `short_distance` | 想走短一點 |

mock / LLM **只回傳 mode 名字字串**；套用時（`VehicleAgent.apply_active_mode`）會依名字查 `ACTIVE_MODE_PROFILES` 表，帶入對應的數值與路徑策略。

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

## 4. 五種 mode 的設定（預設值）

數值表（四個 weight 各列加總 ≈ 1，方便比較取向）：

| mode | desired_speed | time | distance | comfort | capacity |
|---|---|---|---|---|---|
| `fast` | 55 | **0.70** | 0.20 | 0.05 | 0.05 |
| `tolerate_congestion` | 45 | 0.55 | 0.30 | 0.10 | 0.05 |
| `avoid_congestion` | 38 | 0.20 | 0.10 | 0.25 | **0.45** |
| `comfortable` | 42 | 0.20 | 0.10 | **0.45** | 0.25 |
| `short_distance` | 35 | 0.10 | **0.70** | 0.10 | 0.10 |

策略旗標：

| mode | congestion_penalty | avoid_threshold | road_class_bias | recompute_on_crowded | route_randomness |
|---|---|---|---|---|---|
| `fast` | 0 | — | — | true | 0.05 |
| `tolerate_congestion` | 0 | — | — | **false** | 0.05 |
| `avoid_congestion` | **3.0** | **0.6** | — | true | **0.20** |
| `comfortable` | 1.0 | — | **0.4** | true | 0.10 |
| `short_distance` | 0 | — | — | true | 0.10 |

### 各 mode 的「走法」一句話總結

- **fast** — 最短時間路徑：無視壅塞，走自由流最快路線（塞了會重算找更快的）。
- **tolerate_congestion** — 時間優先但**不繞路**：成本同 fast，但塞車不重算，路徑走到底。
- **avoid_congestion** — **避塞**：壅塞重罰 + 對高壅塞邊（>0.6）硬避開 + 積極重算 + 高隨機分散。
- **comfortable** — **偏好大路**：幹道打折、小路加罰，中度避塞，走大條好開的路。
- **short_distance** — **純距離最短**：幾乎無視速度與路型，願意鑽小路抄近。

> 注意：`avoid` 的繞路、`tolerate` 的不繞路等差異，**主要在模擬跑出實際壅塞後才會明顯**。零壅塞時（剛開始）各 mode 可能走相同路線。

---

## 5. 資料流（如何串起來）

```
config/simulation.toml [active_modes.*]
    │ tomllib 載入
    ▼
config.ACTIVE_MODE_PROFILES: dict[str, ActiveModeProfile]
    │ 依名字查表
    ▼
VehicleAgent.apply_active_mode("avoid_congestion")     # mock/LLM 只回名字
    │ 套用數值 + 策略旗標到 agent 欄位
    ▼
VehicleAgent.routing_strategy()  ──►  routing.find_path(..., strategy, seed)
                                 └─►  engine 依 recompute_on_crowded 決定塞車是否重算
```

關鍵檔案：
- `src/llm_abm_simulator/config.py` — `ActiveModeProfile` 資料模型、`_DEFAULT_ACTIVE_MODE_PROFILES`、`_build_active_mode_profiles`、`ACTIVE_MODE_PROFILES`。
- `src/llm_abm_simulator/domain/agent.py` — 策略欄位、`apply_active_mode`/`_apply_named_profile`、`routing_strategy`。
- `src/llm_abm_simulator/spatial/routing.py` — strategy-aware `find_path`（含 `_edge_jitter`、road-class、硬避開）。
- `src/llm_abm_simulator/simulation/engine.py` — 初始指派套 profile、重算讀 `recompute_on_crowded`、傳 `seed`。

---

## 6. 怎麼調整

改 `config/simulation.toml` 的 `[active_modes.<mode>]` 任一值，重設模擬即生效（即時，不需重建路網）。範例：

```toml
[active_modes.avoid_congestion]
congestion_penalty = 5.0     # 想更激進地避塞
avoid_threshold = 0.5        # 更早就把路段視為「該避開」
route_randomness = 0.30      # 車流分得更散
```

- 缺某個 key 會回退到該 mode 的程式碼預設；缺整個 mode 會用內建預設。
- TOML key 名稱 == `ActiveModeProfile` 欄位名；要加新策略旗標＝dataclass 加欄位 + `find_path` 讀它 + TOML 加同名一行。

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
