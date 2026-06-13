# 事件需求生成（出生地分配）— 重力模型

## 為什麼

把「**人是誰**」（persona 行為原型）與「**人從哪來**」（出生地）**解耦**：

- **舊**：出生地由 persona 的 `residential_location` 決定 → 換圖層會打架、且 persona 池得生到很大（2 萬）才有足夠空間覆蓋。
- **新**：出生地由「**生產約束重力模型**」依各區**人口** + 對**場館距離**衰減加權抽樣決定；persona 只負責行為（residential_location 退為風味文字）。

好處：①persona 池只需數百個原型即可（可用開源模型分批生成、可重現）；②換圖層時出生地自動依該圖層人口分布生成，不打架；③符合 GIScience 的 spatial interaction 模型，論文可引用。

## 模型

單一目的地（場館）、生產約束重力模型：

```
weight_i = population_i × f(d_i)
  f(d) = exp(−beta · d_km)      （decay = "exp"，預設）
       = d_km^(−beta)           （decay = "power"）
  P_i  = weight_i / Σ weight     （各區被抽中機率）
d_i = 區 i 形心到場館的距離（公尺座標 EPSG:3826）；下限 min_distance_km。
```

每個 agent 依 `P` 抽一個出生區（用引擎的 seeded RNG → 同 seed 同結果）。

## 設定（`config/simulation.toml` 的 `[demand]`）

```toml
[demand]
enabled = true        # false → 回退既有出生地指派
beta = 0.08           # 距離衰減；越大越集中在近場館的區
decay = "exp"         # "exp" 或 "power"
min_distance_km = 0.5 # 距離下限，避免同區 d→0 權重爆掉
```

- `beta` 是「距離敏感度」：之後前端會做成 **slider**，demo 時即時展示「催客圈」如何隨距離衰減改變（互動 + 空間性賣點）。
- 替代模型：可在 paper 提 **radiation model（Simini 2012）** 作為免參數對照。

## 資料來源

- 人口：`data/gis/town_population.csv`（`town_name,population`，`#` 開頭為註解）。
- ⚠ 目前 bundle 的是**近似值（約 2023 量級）**，供 demo/相對權重；**正式論文請替換為內政部戶政司／臺南市民政局官方月報的精確數據**（同格式即可，不需改 code）。
- 無人口資料（population 全 0）或 `enabled=false` → 重力生成略過，保留既有出生地指派（不中斷）。

## 程式對應

- `src/llm_abm_simulator/mobility/demand.py`：`gravity_weights` / `assign_origin_towns` / `expected_distribution`。
- `engine.initialize()`：在 `_initial_decisions()` 後呼叫 `demand.assign_origin_towns(...)` 覆寫出生地，再 `_place_agent`（在該區內隨機路網節點生成）。
- `domain/town.py`：`Town.population`；`spatial/gis_loader.py`：載入時 join 人口 CSV。

## 與其他模組的關係

- **Persona 池**：`profile_pool` 仍指派 name/車種；其 `residential_location` 不再是出生地權威來源（被重力模型覆寫）。
- **可抽換圖層（規劃中）**：每個 scenario 自帶區界+人口，重力模型自動套用 → 出生地與 persona 不打架。
