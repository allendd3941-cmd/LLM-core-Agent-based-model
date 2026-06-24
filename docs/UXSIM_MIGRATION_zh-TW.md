# 後端模擬器遷移到 UXsim（規劃 + 進度）

> 本文件記錄「把後端車流物理/路由換成 [UXsim](https://github.com/toruseo/UXsim)、LLM 行為層架其上」的計畫與進度。
> 平台未完全鎖定的最後一個決策見文末「待決策」。

## 1. 為什麼 UXsim
- 純 Python（LLM 模組原生接入，無跨語言橋）、MIT。
- mesoscopic（Newell 簡化跟車 = kinematic-wave + 三角基本圖）→ **內建排隊與 spillback**（解決自寫引擎「同速無限堆積」的不真實壅塞）。
- 內建可選 route choice（DUO/DUE/DSO）+ 每車路由控制 → LLM 異質行為可注入。
- 我們因此**不再自己維護**：路徑搜尋、壅塞模型、移動、回堵物理。

## 2. 架構：Facade 保留（關鍵）
前端 / `web` / LLM 模組只依賴 `SimulationEngine` 的**公開介面** + `SimulationState` 輸出。
所以遷移 = **保留 engine 公開介面當 facade，只把「內部」換成 UXsim 驅動**。
全程以 `engine_backend = legacy | uxsim` 旗標保護：UXsim 版未通過全部驗收前，系統隨時可退回 legacy。

| 類別 | 內容 |
|---|---|
| **保留不動** | 前端、`web/*`、`decisions/*`、`llm_server/*`、`mobility/demand.py`、`scenarios`、`SimulationState`、scheduler/metrics |
| **改接資料來源**（介面不變，改讀 UXsim） | 感知標籤、偵測器計數、analysis、座標、散場、介入、匯出、agent 欄位（movement 欄位改從 UXsim 讀、加 `ux_name`） |
| **取代/刪除** | `spatial/routing.py`、engine 的移動/車流（`_move_agent`/`_advance_along_path`/`_recompute_flows`/…）、`Road` 壅塞模型 |
| **新增** | `spatial/uxsim_builder.py`（graphml→World + 裁切）、engine UXsim 驅動層 |

## 3. decision ↔ route：Design 2
LLM 決策輸出的是 **`action_mode`（策略/偏好），不是路徑**。遷移後採 **Design 2**：
```
LLM → action_mode → UXsim 的 set_links_prefer / set_links_avoid（或 route_pref）→ UXsim 內建演算法算出異質路線
```
**不自寫 Dijkstra**。只有個別 agent 需要「指定整條精確路徑」時才退回 `enforce_route`（M1）。
`action_mode` 欄位處置：保留並映射 `distance` / `road_class_bias`；精簡被真實 travel time 涵蓋的
`congestion_penalty`/`avoid_threshold`/`comfort`/`capacity`；`departure`/`recompute_on_crowded` 留為行為。
（`action_mode` 全面改名為 `action_mode`。）

## 4. 已確認的 UXsim API（spike 實測，見 `spike/uxsim_spike.py`）
- `World(deltan, tmax, random_seed, hard_deterministic_mode, print/save/show_mode, route_choice_principle='homogeneous_DUO')`；DUO 旋鈕為大寫屬性 `DUO_NOISE / DUO_UPDATE_TIME / DUO_UPDATE_WEIGHT`。
- `W.NODES` / `W.LINKS` 是 **list**；查名用 `W.get_link(name)` / `W.LINKS_NAME_DICT`。`W.VEHICLES` 是 OrderedDict（name→Vehicle）。
- `W.addNode(name, x, y)`、`W.addLink(name, start, end, length, free_flow_speed(m/s), number_of_lanes, jam_density)`。
- `W.addVehicle(orig, dest, departure_time, name)` → 回 Vehicle；**運行中可加**（介入 demand_surge ✓）。
- Vehicle：`state / link / x / v(速度) / route_pref(每link numpy) / links_prefer / links_avoid / orig / dest`；
  `set_links_prefer([名])` / `set_links_avoid([名])` / `enforce_route([名])` 吃**字串 link 名**、即時生效（含運行中改道 ✓）；
  `traveled_route()` → `(Route, [t])`、`Route.links`。
- `W.exec_simulation(until_t=)` 可增量步進；`W.check_simulation_ongoing()` 當迴圈條件。

## 5. ⚠️ 網路規模發現與解法（重要）
UXsim 內建 route choice 用**全對最短路**：`dist/pred/next = n_nodes²`、`route_pref = n_nodes × n_links`。
記憶體 ∝ 節點數²。

- 完整 OSM 台南 **15,833 節點 / 42,510 邊 → ~9 GiB**（本機 MemoryError；server 64GB 可放但 route 更新慢）。
- 拓樸簡化幫助有限（degree≤2 僅 11%，graph 已被 osmnx 簡化）。
- **解法 = 區域裁切**（`uxsim_builder.crop_to_region`，以球場為圓心）：

| 半徑 | 節點數 | route-choice 記憶體估計 |
|---|---|---|
| 5 km（含全部 55 相機）| 1,494 | ~0.1 GiB |
| 6 km | 2,428 | ~0.2 GiB |
| 8 km | 4,783 | ~1.0 GiB |
| 全市 | 15,833 | ~9 GiB |

裁切 ≤8km 的網路已實測 build+run 通過（`spike/uxsim_network_analysis.py`）。

**決策（使用者拍板）：用「全市 + 只在 server 跑」**——保留城市級重力 OD（37 區），接受 ~9 GiB
+ route 更新較慢、完整模擬只在 server（64GB）跑。`crop_to_region` 改作**本機開發/測試工具**
（小半徑裁切讓 facade 邏輯能在筆電快速驗證；正式跑用全網 `radius_km<=0`）。
後續若 route 更新太慢，可調大 `DUO_UPDATE_TIME` 或日後做「只算實際終點的稀疏 route choice」。

## 5.5 congestion_proxy 對齊 UXsim 物理 + highway_specs 套用情形

**`congestion_proxy` 已對齊 UXsim 物理**（`UXsimEngine._readback`）：
```
congestion_proxy = min(1, num_vehicles / (kappa × length))   # 占有率
```
- `kappa` = UXsim link 的 **jam density**。`build_world` 以 **`jam_density_per_lane`** 傳入 `[uxsim].jam_density`
  → `kappa = 該值 × number_of_lanes`（**每車道**堵塞密度 × 車道數）、`length` = link 長度
  → `kappa × length` = 該 link 堵塞時最多容納車數（＝ UXsim 判斷 spillback/壅塞的同一儲容）。
  **儲容(kappa×length)與吞吐(基本圖 q)皆隨車道數「線性」放大**（3 車道≈停 3 倍車、過 3 倍車；1 車道行為不變）。
- **單一容量來源**：`UXsimEngine._build_world_and_vehicles` 把 `Road.capacity` 設為 `kappa × length`（jam 儲容），
  之後 `congestion_proxy`、`build_analysis` 的 V/C、snapshot、GIS 匯出**全部讀同一個 `Road.capacity`** → 報表與即時地圖、與物理完全一致。
- 此 proxy 驅動 `is_crowded` / `avoid_congestion` 觸發 / 前端壅塞上色 / `build_analysis` 的 V/C（皆同一儲容）。
- **已移除設定參數**（換 UXsim 後與物理無關、會誤導）：`[highway_specs].capacity_per_lane`、`[roads].capacity_fallback_vehicle_count`、`[roads].flow_weight_multiplier`。
  （legacy 引擎內部改用常數佔位；UXsim 後端本就覆寫 `Road.capacity`，故無影響。）

**`[highway_specs]` 在 UXsim 後端的實際套用**：
| highway_specs 欄位 | 是否進 UXsim 物理 | 說明 |
|---|---|---|
| `speed_car` / `lanes` | ✅ 是 | 下載時烤進 graphml（OSM 真實值優先）→ `addLink(free_flow_speed, number_of_lanes)`。**改了要重建 graphml**。 |
| jam density（容量來源） | 來自 `[uxsim].jam_density`（以 `jam_density_per_lane` 傳入） | `kappa = 每車道值 × 車道數` → 容量/儲容 = `kappa×length`，**隨車道線性放大**（✅ 已改，2026-06；舊「全路型統一、車道不影響儲容」已淘汰）。 |

> **引擎預設已改 UXsim**：`web/websocket._make_engine` 預設 `uxsim`；本機記憶體不足（UXsim 全市約 9GB）時設 `LLM_ABM_ENGINE=legacy` 退回自寫物理引擎當逃生口/baseline。`tests/simulator/test_engine.py` 仍以 legacy 引擎做輕量整合測試（筆電可跑）。

> ⚠️ **`crowded_road_threshold` 語意改變**：congestion_proxy 現在是「真實占有率」（1km link 約可容 `kappa×length` 台，故同樣車數下占有率比舊 proxy 低很多）。demo 若要更敏感的壅塞觸發，**把 `crowded_road_threshold` 調低**。（多車道容量已用 `jam_density_per_lane` 隨車道線性放大；多車道路占有率因儲容變大會更低，壅塞更難飽和。）

## 5.6 三種 action_mode → UXsim 路徑選擇參數（最終版，純參數、零自算路徑）

**方向一律由 UXsim 自己的 `route_pref`（內建最短時間樹）提供**；我們只用 UXsim 既有參數「在它的時間路由上加篩選/凍結」，**完全不自算路徑、無 Dijkstra**。

| action_mode | UXsim 參數 | UXsim 怎麼選（原理） |
|---|---|---|
| `fast` | `route_choice_principle="homogeneous_DUO"`，清 prefer/avoid | 純走 UXsim 最短時間，自然避開壅塞 |
| `avoid_congestion` | DUO + `set_links_avoid(壅塞 link；每節點保底)` | 在「非壅塞出口」中、用時間樹挑往終點 → 繞開塞段 |
| `tolerate_congestion` | `route_pref.copy()` + `principle="fixed"` | 凍結當下時間路線、不再被 DUO 更新 → 不改道、忍受 |

- `links_avoid` 的那組 link 名只是「**查屬性篩出**」：壅塞用 `congestion_proxy ≥ 門檻` 篩。**不是算路徑。**
- **方向**全取自 UXsim 的 route_pref（它的時間樹）；選路 100% 是 UXsim 的 `route_next_link_choice`。
- `tolerate` 只是 `route_pref.copy()`（記憶體複製）+ 非 DUO principle（讓 UXsim 不覆寫它）。
- **`short_distance` 與 `comfortable` 已移除**：兩者都需要 UXsim 沒有的成本維度（距離 / 路型偏好）。UXsim 路徑選擇
  唯一方向來源是「最短時間樹」，任何路型/距離篩選只要時間樹下一步不在篩選集 → 替代邊 route_pref=0 → 路口亂選到不了
  （實測 comfortable：prefer 幹道 1/60、avoid 小路 2/60 抵達）。在「零 Dijkstra」前提下無法實現，故移除。

**不崩的保證**：
- `set_links_prefer` strand-safe：路口無該類出口 → UXsim 自動退回全部出口/DUO。
- `set_links_avoid`：用 `_safe_congested_avoid_set()`——**保證每個節點至少留一個非避開出口**（出度檢查，非算路徑）→ 不會空集合崩潰。

**重算時機**：`_inject_routes` 以 `first=(已注入 mode ≠ 當前 mode)` 判斷，decision 一變即套用；`avoid_congestion` 另在「壅塞 + 過 cooldown」時重算避開集合。背景車不經此（純 DUO）。

**散場也自動成立**：方向都靠 UXsim 自己的時間樹，跟終點是球場或居住地隨機點無關，我們只加路型/壅塞篩選。

**車輛顯示位置**：UXsim 後端覆寫 `_xy_to_latlng` → 用 `_metric_to_latlng` 真實投影
（UXsim 的 `a.x/a.y` 是 `get_xy_coords` 給的真實連續公尺座標）。**不沿用父類「吸最近節點」近似**——
否則整段路的車會塌到路口節點、前端攤成一坨格子（顯示假象，非物理塞爆）。`_visible_agents` 已視窗裁切故投影成本可接受。

## 5.7 環境感知對接 UXsim（road_ahead）

送 LLM 的感知大多直接吃 UXsim 真值：`congestion_proxy`（kappa 占有率）、`speed_status`（`veh.v`÷速限）、
`nearby_agent_count`（真實 x,y 空間網格）、`congestion_hotspots`/`trend`（聚合）。

**唯一需重接的是「前方路況」`road_ahead`**：base 版沿 `agent.current_path` 掃 `lookahead_distance_m`，
但 UXsim 的路由逐路口即時決定、agent 不持有完整前方路徑（`current_path` 只有當前 link）→ base 版恆回「順暢」。
→ `UXsimEngine` 覆寫 `_road_ahead`：**只看「朝終點的下一條 OSM 段」**＝ `_dest_path_links(agent,"time")[0]`
（其起點＝當前 link 末端節點，實測 42/42 確認是真正下一段），壅塞則回質性文字「下一條路壅塞（街名）」、否則「順暢」。
- 比固定距離掃描更貼合 UXsim、也更像真人「看到下一條路塞就改道」（bounded local perception）。
- **連帶恢復前瞻觸發**：event-triggered 的 `signal = 腳下壅塞 or road_ahead 非順暢` 第二項回來（trigger 程式碼零改）。
- ⚠️ 注意 `[memory].feel_congested_proxy = 0.6` 對 kappa 真實占有率偏高 → road_ahead 實測常為「順暢」；
  要更敏感需調此門檻（與 `crowded_road_threshold` 同屬 §5.5 的門檻 tuning，未動）。
- `[perception_context].lookahead_distance_m` 僅 legacy 用；UXsim 只看下一段、忽略它。

**記憶體 `memory.moved`（停滯/緩慢/前進中）也已接上**：它吃 `distance_moved_last_step`，原只在 legacy 的
`_move_agent` 被設、UXsim 不走那條 → 曾恆為 0 → 記憶誤報「停滯」。修法：`UXsimEngine._readback` 在更新 `a.x,a.y` 時
用**每步歐氏位移**(`hypot(x-prev_x, y-prev_y)`，與 legacy 同算法)設 `distance_moved_last_step`，未在路上的車設 0。
實測 `moved` 隨實際位移分布為 緩慢/前進中、換算時速 6.5~25 km/h（合理）。記憶其餘欄位（traffic_feel/where/
getting_closer/remaining/congested_spots/smoothness/summary）本就直接吃 UXsim 真值，無需改。

**前端 agent 檢視「狀態」列接上 `selected_action`**：原 `selected_action`（goto_destination/wait_at_signal/
arrived/error/recompute）只在 legacy 寫、且**未進 `AgentSnapshot`**（沒接到前端）。已：① `UXsimEngine._readback`
依車況設 `selected_action`（含「中途改道」＝該 cycle 有 reroute）；② 加進 `AgentSnapshot` + `_snapshot`；
③ 前端 `simulation.js` 以 `ACTION_LABELS` 轉中文（前往目的地/改道中/等紅燈/已抵達/路徑異常）顯示在「狀態」列。

## 5.8 稀疏終點 + 稀疏 route_search（城市尺度吞吐瓶頸的解法）

**問題**：UXsim 的 DUO 選路 `RouteChoice.route_search_all` 每 `duo_update_time` 對**全節點當終點**跑一次
scipy all-pairs Dijkstra（N≈1.5 萬節點）→ O(N·(E+N·logN))、**單核**、N² 記憶體。server 實測 step1
`move≈340s`，是城市尺度最大吞吐瓶頸（`deltan` 調大只省物理、對此無效——它與車數無關、只與節點數有關）。

**觀察**：其實只需要「**有車真的要去的終點**」的最短路樹；其他節點當終點算了也沒車用。

**兩段解法（結果一致、只算得少）**：

1. **稀疏終點池**（`[demand].dest_pool_per_capita`，預設 1000）：init 時為每區建
   `ceil(人口/1000)` 個**不重複隨機節點**當「終點池」，所有終點（背景車 OD、散場回家）都從池抽。
   把全市不同終點節點數從上萬壓到**數百~千**（台南全市實測 2052 個、dev-crop 8km 1233 個）。
   - 單一咽喉：`SimulationEngine._dest_node_in_town`（終點專用，原 `_node_in_town` 仍供**起點**用、保留起點多樣性）。
   - 用獨立 rng 建池 → **不擾動主 rng 序列**（既有可重現性不變、同 seed→同結果）。0/負＝停用（舊行為）。

2. **稀疏 route_search**（`[uxsim].sparse_route_search`，預設 true；`simulation/uxsim_sparse_routing.py`）：
   把 `route_search_all`/`homogeneous_DUO_update` 換成「**只對 active 終點集合 D 算**」
   （`dijkstra(adj.T, indices=D)` 只從 D 個源做）。D＝目前所有 vehicle 的終點（搭配池 → |D| 數百千）。
   - **結果完全一致**：`adj_mat_time` 逐 link 逐字複製原版（含 noise/多 link 平均）→ 同樣 dijkstra →
     `route_pref[D]` 與原生 all-pairs **逐元素相同**（`tests/test_sparse_routing.py` 對拍）。
   - **為何安全可 compact**：`RouteChoice.next`/`dist` 在 UXsim 內只被 `homogeneous_DUO_update` 消費
     （per-vehicle 的 `route_pref_update` 僅 `heterogeneous_DUO` 分支用、本專案不用；homogeneous_DUO 車讀
     `route_pref[dest.id]`；`fixed`（tolerate）車該方法 no-op）→ 不需配 N×N。`dist_record` 原為唯寫，省略。
   - 以 patch **RouteChoice 類別**實作（UXsim 在 `exec_simulation` 內才建 `W.ROUTECHOICE`，故 patch 類別、時機無關）；
     `enable_/disable_sparse_routing()` 可開關（false＝還原原生 all-pairs，供對拍/baseline）。

**效益**：route_search 成本 ∝ |D|（而非 N）。實測 dev-crop 8km：active 終點 146 vs 節點 4716（~32× 少源）。
比例越粗（每更多人一個終點）池越小、越快；每 1000 人一個約 4× 多終點（仍遠少於節點）。

**已知限制（可接受、可自癒）**：D 取自當下 vehicle 終點。若某池節點在某次 route_search 當下**無任何車以它為終點**
（respawn/散場才首次用到）→ 該終點 route_pref 等**下次重算**才建好，中間該車可能短暫亂走。實務上車數遠多於池節點
→ 幾乎每池節點時時有車要去，極少發生且下次自癒。原生 all-pairs 無此延遲。詳見 `uxsim_sparse_routing.py` docstring。

## 5.9 readback 去 `traveled_route()`（readback 熱點優化）

`_readback` 每步把每台車狀態讀回 agent，其中「本步走過哪些 link（供偵測器計數）」原用
`veh.traveled_route()[0].links`——它每車**重建 Route 物件**、每條 link 對 `W.LINKS`（list）線性搜 `get_link`。
cProfile 實測：城市尺度下 **`get_link` 佔 readback 的 93%**（3500 車：readback ~13s/步、其中 get_link 36.8s/3 次）。

**改法**：直接讀 UXsim 的 `veh.log_t_link`（換 link 才記的 (t, Link) 序列；非字串者即 Link），取名字差分本步新進入的邊。
**完全免重建 Route、免 get_link**。差分語意與舊版逐元素一致（prog 記未過濾長度、輸出依 name 過濾）。
- 實測：`get_link` 從 36.8s → 0.3s；每步平均 30.8s → 21.3s。
- 正確性：`tests/test_readback_edges.py` 證明兩種抽取法逐元素等價（含一步跨多 link）；偵測器計數不變（見 §5.10 對拍）。

## 5.10 ⚠️ 記憶體 / OOM：關掉 UXsim 每步軌跡記錄（城市尺度必須）

**症狀**：城市尺度（73500 車）跑到第 8 步左右，server 進程**無 log、無預兆瞬間消失**＝ Linux **OOM killer（SIGKILL）**
（SIGKILL 攔不住，來不及印任何東西）。`flow` 每步從 48s 一路漲到 251s 是記憶體暴增的可見徵兆。

**根因**：UXsim 的 `vehicle_logging_timestep_interval` **預設 1**＝每個時間步、每台車都記 `log_x/log_v/log_link/log_lane/…`。
73500 車 × deltan=1 × 數千時間步 → 記憶體**線性暴增** → 撐爆 RAM。

**修法**（`[uxsim]`）：
- `vehicle_logging_interval = -1`：關掉「每步每車」軌跡記錄。**只關每步記錄**——`log_t_link`（換 link 才記、很小）
  **照常產生**，故 readback（§5.9）與偵測器仍正常；`get_xy_coords()` 取當前位置用 `veh.x/link`（非 log）也不受影響。
- `reduce_memory_route_pref = true`：車結束後刪其 per-vehicle `route_pref`（road_ahead 已防呆 `route_pref is None`）。

**驗收（嚴格對拍，`tests/simulator/test_logging_off_equiv.py`）**：同一模擬 logging **ON vs OFF**，
**①進入邊序列 ②偵測器累計計數 ③車輛位置/狀態 三者逐位元相等**，且關掉後進入邊非空 → 證明
「偵測器與模組邏輯完全不受影響」（logging 純觀測、不影響物理）。另確認固定 `PYTHONHASHSEED` 下模擬可重現。

> 註：跨進程的「黃金指紋」比對需固定 `PYTHONHASHSEED`（set/dict 迭代序會隨進程變）；同進程兩跑則恆等。

## 6. 進度
- **Phase 0 spike ✅**：UXsim 能支撐 Design 2 + 介入（不退 SUMO）。唯 deltan=1 城市尺度吞吐待 server 量。
- **Phase 1 ext_id ✅**（+ 連帶修 `app.py` logging import 副作用 → lifespan；全套 53 測試通過）。
- **Phase 2 builder ✅**：`uxsim_builder.build_world`（graphml→World，全網建置 5.7s）+ `crop_to_region`（裁切），均實測通過。
- **Phase 3 核心 ✅（rule/DUO + ingress，本機驗證）**：`simulation/uxsim_engine.py` 的 `UXsimEngine(SimulationEngine)` 子類別，只覆寫物理（`_place_all_agents` 不算路徑、`step` 用 `exec_simulation`、`_readback` 把 UXsim 車況/壅塞讀回 agent/Road、`_current_road`/`_recompute_flows` 改讀 UXsim），其餘繼承。`web/websocket._make_engine` 以環境變數 `LLM_ABM_ENGINE=uxsim|legacy`（預設 legacy）選後端。小裁切網路實測 init→step→snapshot→reset：SimulationState 契約合法、車會移動/抵達、壅塞隨匯入上升（`spike/uxsim_engine_check.py`）。
- **Phase 4–5 本機部分 ✅（已驗證）**：
  - **偵測器計數 ✅**：`_readback` 用 `traveled_route` 差分填 `_step_entered_edges` → 繼承 `_update_detectors`（55 相機、15 計到車、事件車通過 265 次）。
  - **散場 ✅**：`_handle_egress` 在 UXsim 重生「球場→home」車（運行中 addVehicle）；實測宣告後車陸續返家、phase=home（40/40）。
  - **背景車重生 ✅**：`_respawn_arrived_ambient_ux` 抵達後以重力抽新終點重生（維持穩態負載，40→172 重生）；`_road_peak` 於 `_readback` 累積 → `build_analysis` 瓶頸分析可用。
  - **Design 2 路徑注入 ✅（prefer）**：`_inject_routes` `road_class_bias>0` → `set_links_prefer(幹道)`，**安全**（只過濾出口、無偏好出口退回全部、不困死）。效力於 spike 鑽石網路已證；小裁切看不到幹道比例上升＝最短路本就大量用幹道（網路特性）。
  - **號誌 ✅**：`build_world(signals=)` 把雙相位號誌節點→`addNode(signal=[半,半], signal_offset)`、入口邊→`addLink(signal_group=方向相位組)`（8km 裁切 2477 號誌節點，實測有等紅燈步、無崩潰）；`_readback` 啟發式設 `waiting_at_signal`。
  - **整趟路徑視覺化 ✅**：`_readback` 累積 `visited_nodes` → `get_agent_path` 正常（ingress 多點）。
  - **NL 介入 ✅（demand_surge）**：`apply_intervention` 覆寫，`demand_surge` 用運行中 `addVehicle` 注入（實測 20 台加入且移動）；`avoid_area` 記錄但路由生效待 stranding-safe（見下）。
  - **壅塞上色**：免重標定——`_readback` 用同一 `Road.update_flow`（num_vehicles/capacity）公式，`congestion_proxy` 語意與 legacy 相同。
  - **契約（Phase 1.5）**：以 `spike/uxsim_engine_check.py` 對 `SimulationState` 結構斷言涵蓋（前端訊息鍵/型別一致）。
  - **⚠️ `set_links_avoid` 困死風險**：某節點所有出口被避 → UXsim `max()` 崩潰；故 avoid_area 路由與 `_inject_routes` 的 avoid 延後，需 stranding-safe 機制（小圓 / 成本懲罰）。
- **餘下（必須 server / LLM / 使用者設計才能驗，本機無法）**：
  - **全市規模 + deltan=1 吞吐 / route 更新時間** → server（9GiB）。
  - **LLM 端到端 Design 2**（真 LLM 決策驅動路由）→ LLM 服務。
  - **FD 校準 + 驗證（main.py）+ baseline（DUO vs rule vs LLM）** → server + LLM + 相機資料。
  - **avoid_area stranding-safe 機制** → 設計決策。
  - **清理舊碼**（刪 routing/移動/congestion + legacy 旗標）→ parity 後。

## 7. 怎麼跑 spike（驗證用）
```bash
uv run python spike/uxsim_spike.py            # API 能力驗證（小網路）
uv run python spike/uxsim_build_check.py       # 全網 graphml → World 建置 + 小跑
uv run python spike/uxsim_network_analysis.py  # 規模分析 + 裁切後 build/run
```
（deltan=1 城市尺度吞吐請在 server 跑。）

## 8. 已決策
- **網路範圍 = 全市 + 只在 server 跑**（保留城市級重力 OD）。本機開發用 `crop_to_region` 小裁切驗證 facade 邏輯，正式全網跑在 server。
- **deltan = 1**（個體 LLM agent）。deltan=1 全市吞吐 + 全對 route 更新時間 → 待 server 實測；必要時調 `DUO_UPDATE_TIME`。
