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
dest_pool_per_capita = 1000  # 稀疏終點：每區取 ceil(人口/此值) 個不重複隨機節點當終點池；0/負=停用
```

- `beta` 是「距離敏感度」：之後前端會做成 **slider**，demo 時即時展示「催客圈」如何隨距離衰減改變（互動 + 空間性賣點）。
- 替代模型：可在 paper 提 **radiation model（Simini 2012）** 作為免參數對照。

### 稀疏終點池（`dest_pool_per_capita`）

把所有**終點**（背景車 OD 的終點、散場回家的家）收斂到一個**有界池**：每區只取
`ceil(人口 / dest_pool_per_capita)` 個**不重複隨機節點**（人口加權→近真實活動分布，台南全市約 2052 個終點）。
目的是把「全市不同終點節點數」從上萬壓到數百~千，**讓 UXsim 的 DUO route_search 只需對這些終點算最短路**
（搭配 `[uxsim].sparse_route_search`），城市尺度吞吐大幅提升——這是 73k 車城市尺度能即時跑的關鍵槓桿之一。
- 起點不受影響（仍可為區內任一節點，保留多樣性；起點不影響 route_search 成本）。
- 池用獨立 rng 建、不擾動主序列 → 既有結果可重現性不變。`0/負`＝停用（終點回到區內任一節點，舊行為）。
- 機制與對拍詳見 [`UXSIM_MIGRATION_zh-TW.md`](UXSIM_MIGRATION_zh-TW.md) §5.8、`simulation/uxsim_sparse_routing.py`。

### 球場「抵達圈」多閘門終點（`arrival_radius_m`）

**問題**：事件車原本**全部以單一球場節點為終點** → 2.3 萬台全擠一個進場喉口，受該節點進場道路容量限制 →
城市尺度 120 分鐘只 ~11% 抵達(其餘排長龍)。這是**單點 funnel**，不是真實塞車(真實球場有多個入口/停車場分流)。

**做法**：以球場為心、半徑 `arrival_radius_m`(預設 800m)內的所有路網節點 = **抵達圈節點集**(對應周邊停車場/入口)。
每台事件車的終點 = 圈內節點之一(從哪個方向來就停那一側)→ 路由方向分散。

**距離抵達(`arrival_on_circle_entry`,預設 true)— 解「終點前 sink 回堵」的關鍵**：
只「分散終點節點」還不夠——車仍要**走到節點**才算抵達,於是在節點前排成 sink 隊伍(實測城市尺度排空僅 ~10 台/分、移動中車數整場卡在數千不降)。
改為:**事件車一踏進抵達圈(對球場中心 ≤ `arrival_radius_m`)就算抵達,並把該車安全移出 UXsim 路網**(=停在周邊、離開車流)。
- 沒有 sink 節點了 → 車在圈周各處跨界即離網,接近道路只承載「流量」不承載「排到節點的隊伍」→ 終點前人造回堵消失。
- **判定順序**:每步物理跑完(readback)先更新位置、**再**判距離 → 綠點必在圈內才標抵達(「踏進那一刻才算」)。
- **agent 不從畫面消失**:只移除 UXsim Vehicle(解塞),agent 物件保留、座標凍在「踏入點」、顯示為已抵達(綠點停在圈內)。
- **安全移除不破壞物理**:`_remove_vehicle_from_network` 只重接該車的 leader/follower 鏈 + 從 link.vehicles 與節點 incoming_vehicles 依值移除 + cum_departure 計數 + pop registry;在 step 末(步間)執行,不碰其他車/號誌/引擎。
- `false` = 回退「走到終點節點才算抵達」(會在終點前回堵;節點抵達車 snap 到終點節點顯示)。

**K-nearest 分流(`arrival_gates_per_car`,預設 5)**：若只挑「離出發地**最近 1 個**」節點,因出生地由重力模型集中在少數方向 →
同方向車全擠少數熱門節點(實測 top-1 仍佔 **44%**,等於只把單點 funnel 變成幾個小 funnel)。改為從「**最近 K 個**節點」中
以**穩定 hash**(crc32(agent_id|seed),跨進程可重現)挑一個 → 主流方向車流散到鄰近數個節點、仍維持「就近停」(只在最近 K 內)。
- 實測(R=600, 2000 車)：top-1 佔比 **44%→13%**、用到節點 16→35、每節點 max 873→257(K=5 vs K=1,~70% 改善)。
- `K=1` = 回退「只挑最近」舊行為;圈內節點 < K → 用現有全部。

- 圈內無節點(半徑過小)→ 回退單一球場節點(舊行為)。
- 前端：以**半透明灰色圓圈**顯示此半徑,圖標窗格「抵達圈」可開關。
- ⚠ `arrival_distance_threshold_m` 是 **legacy 引擎專用**(UXsim 不吃此值)；勿與 `arrival_radius_m`/`arrival_on_circle_entry` 混淆。
- 程式：`engine._build_arrival_nodes` / `_assign_arrival_node`(K-nearest 路由方向);
  `uxsim_engine._readback`(距離抵達判定)/ `_remove_vehicle_from_network`(安全移除)。
  測試 `tests/simulator/test_arrival_circle.py`、`test_arrival_spread.py`、`test_circle_arrival.py`。

## 資料來源

- 人口：`data/gis/town_population.csv`（`town_name,population`，`#` 開頭為註解）。
- ⚠ 目前 bundle 的是**近似值（約 2023 量級）**，供 demo/相對權重；**正式論文請替換為內政部戶政司／臺南市民政局官方月報的精確數據**（同格式即可，不需改 code）。
- 無人口資料（population 全 0）或 `enabled=false` → 重力生成略過，保留既有出生地指派（不中斷）。

## 程式對應

- `src/llm_abm_simulator/mobility/demand.py`：`gravity_weights` / `assign_origin_towns` / `expected_distribution`。
- `engine.initialize()`：在 `_initial_decisions()` 後呼叫 `demand.assign_origin_towns(...)` 覆寫出生地，再 `_place_agent`（在該區內隨機路網節點生成）。
- 稀疏終點池：`engine._build_dest_pool` / `_dest_node_in_town`（終點專用）/ `_dest_pool_for`；稀疏 route_search 在 `simulation/uxsim_sparse_routing.py`。
- `domain/town.py`：`Town.population`；`spatial/gis_loader.py`：載入時 join 人口 CSV。

## 與其他模組的關係

- **Persona 池**：`profile_pool` 仍指派 name/車種；其 `residential_location` 不再是出生地權威來源（被重力模型覆寫）。
- **可抽換圖層（規劃中）**：每個 scenario 自帶區界+人口，重力模型自動套用 → 出生地與 persona 不打架。
