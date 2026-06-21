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
- `kappa` = UXsim link 的 **jam density**（即 `build_world` 傳入的 `[uxsim].jam_density`）、`length` = link 長度
  → `kappa × length` = 該 link 堵塞時最多容納車數（＝ UXsim 判斷 spillback/壅塞的同一儲容）。
- **不再用 `[highway_specs].capacity_per_lane`** 當分母（那是 legacy 引擎的容量）。
- 此 proxy 驅動 `is_crowded` / `avoid_congestion` 觸發 / 前端壅塞上色 / `build_analysis` 的 V/C（用同一儲容）。

**`[highway_specs]` 在 UXsim 後端的實際套用**：
| highway_specs 欄位 | 是否進 UXsim 物理 | 說明 |
|---|---|---|
| `speed_car` / `lanes` | ✅ 是 | 下載時烤進 graphml（OSM 真實值優先）→ `addLink(free_flow_speed, number_of_lanes)`。**改了要重建 graphml**。 |
| `capacity_per_lane` | ❌ 否 | UXsim path **已不使用**（容量由 FD：`free_flow_speed × jam_density × lanes` 推；congestion_proxy 改用 `kappa×length` 儲容）。 |
| jam density | — | 來自 `[uxsim].jam_density`，**全路型統一**；要分路型需改用 `jam_density_per_lane`（未做，屬 Phase 6 FD 校準）。 |

> ⚠️ **`crowded_road_threshold` 語意改變**：congestion_proxy 現在是「真實占有率」（1km link 約可容 `kappa×length` 台，故同樣車數下占有率比舊 proxy 低很多）。demo 若要更敏感的壅塞觸發，**把 `crowded_road_threshold` 調低**（或日後用 `jam_density_per_lane` 讓多車道容量更真實）。

## 5.6 五種 action_mode → UXsim 路徑選擇參數（最終映射）

**只用 UXsim 既有的 per-vehicle route-choice 參數**（`set_links_prefer` / `set_links_avoid`），不自寫路由器。
路徑用快取的反向終點樹（單一權重）算出後，取其 link 名餵給 UXsim。

| action_mode | UXsim 動作 | 路徑來源（樹權重） | 行為 |
|---|---|---|---|
| `fast` | 清 prefer/avoid（純 DUO） | — | DUO 依**即時旅時**選路，自然避開壅塞 |
| `comfortable` | `set_links_prefer(路徑 links)` | 自由流時間 + `road_class_bias`（偏好幹道） | 走大路、朝終點（有方向→不亂繞） |
| `short_distance` | `set_links_prefer(路徑 links)` | 純距離 | 總距離最短 |
| `tolerate_congestion` | `set_links_prefer(路徑 links)` | 自由流時間 | 黏著計畫路徑、**忍受壅塞不繞開** |
| `avoid_congestion` | `set_links_prefer(繞塞最短路 links)` | 移除壅塞邊後的最短路 | 主動繞開塞車路段（全塞則回 DUO 走最不塞） |

**重算時機（驗收標準）**：`_inject_routes` 以 `first = (已注入 mode != 當前 mode)` 判斷——
**只要 decision 改變（mode 變），下一步即重算該車的路徑選擇**（重算偏好/避開集合）。
此外 `avoid_congestion` 還會在「該車壅塞 + 過 reroute cooldown」時追當前壅塞重算。
`tolerate/comfortable/short_distance` 的路徑與當前壅塞無關，故只在 decision 改變時重算即可。

**`set_links_prefer` 為何中途安全且不卡死**（見 `uxsim.py` `route_next_link_choice`）：
每個路口先做「出口邊 ∩ 偏好清單」，**且交集非空才限縮**；若該路口沒有任何偏好邊
（被擠出原路徑 / spillback / 前方全塞）→ 跳過限縮 → 退回全部出口邊由 DUO 選 → **永遠有路走**。
**為何全部改用 `prefer`、完全不用 `set_links_avoid`**：`set_links_avoid` 是在每個路口「相減」掉壅塞出口，
某節點所有出口都被避掉時 → 空集合 → UXsim `route_next_link_choice` 的 `max()` 崩潰（**重壅塞下實測會發生**，
全域連通守衛不足以擋，因 DUO 仍可能把車帶進局部死角）。改用 `prefer` 後（交集為空才不限縮、否則退回全部出口），
**任何路口都不會被清空 → 不崩、永遠有路走**。故：
- `avoid_congestion` = `set_links_prefer(移除壅塞邊後的最短路)`（取代舊的 `set_links_avoid`）；
- `tolerate` 不用 `enforce_route`（其 `specified_route` 用「已走 link 數」索引、中途套用會崩）。
→ 5 個 mode 一律 `set_links_prefer` 或清空，**唯一會崩的 `set_links_avoid` 路徑已徹底移除**。

**車輛顯示位置**：UXsim 後端覆寫 `_xy_to_latlng` → 用 `_metric_to_latlng` 真實投影
（UXsim 的 `a.x/a.y` 是 `get_xy_coords` 給的真實連續公尺座標）。**不沿用父類「吸最近節點」近似**——
否則整段路的車會塌到路口節點、前端攤成一坨格子（顯示假象，非物理塞爆）。`_visible_agents` 已視窗裁切故投影成本可接受。

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
