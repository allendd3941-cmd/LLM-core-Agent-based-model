# 專案自我完整檢查報告（自動 loop 產生）

> **Loop 計數：6 / 100**
> 最後更新：2026-06-25 · 基準 commit：`a5a0fbd` (v2.6) · 分支：`uxsim-migration`
> 性質：唯讀稽核（不做任何程式修改）。每 5 分鐘複查並補充；達 100 次自動停止。

---

## 0. 本次驗證到的基線狀態

| 項目 | 結果 |
|---|---|
| 單元測試 `pytest tests/` | ✅ **74 全綠**（loop 1 驗） |
| `python -m compileall src/` | ✅ **exit=0，全檔編譯通過**（loop 2 新增，補 lint 盲點） |
| git 工作樹 | ✅ 乾淨（session 改動已 commit 為 v2.6） |
| TODO/FIXME/XXX/HACK | ✅ src 內無殘留 |
| 靜態檢查 ruff | ⚠ 本機未安裝（compileall 僅檢語法/匯入，無法抓未用變數/風格） |

---

## 1. 真實風險 / 潛在錯誤（依嚴重度）

### 🟠 R1（中）改了 `[highway_specs].default` 但 graphml 未重建 → 改動「靜默失效」〔loop 2 已查證〕
- **查證結果（loop 2）**：本機 toml `[highway_specs.default].lanes` 現為 **2**（本 session 稍早讀到為 1）→ **使用者確實改了，且只動到 `default` 這一項**（其餘 spec 不變）。graphml 檔為 **2026-06-15** 建立，早於本次 config 變更 → **未重建、改動目前完全未生效**（且無報錯）。
- **影響範圍（loop 2 修正 loop 1 的誇大）**：`default` **僅套用在「未列在 `[highway_specs]` 的 highway 型別」**（如 `living_street`、各 `*_link`、`road`、`track`）。**不影響 primary/secondary/tertiary 等主要道路**（它們各有自己的 spec）。故實際受影響邊數有限（約數百條 link/living_street 級），影響低於 loop 1 措辭暗示的程度。
- **建議（未執行）**：若這項改動是刻意的 → server 跑 `python -m llm_abm_simulator.spatial.build_roads` 重建 + 重啟清 `_GRAPH_CACHE`；若非必要可不理（影響小）。

### 🟡 R2（低-中，loop 3 修正）`jam_density_per_lane` 上線 → 手動 golden 基準需重跑（非 CI 失敗）
- `uxsim_builder.py` 改傳 `jam_density_per_lane`（v2.6），多車道邊儲容/吞吐線性放大（本機 1/2/3 線實測 ×1/×2/×3，1 線不變）。
- **查證（loop 3）**：repo 內**無「會因此變更而 fail 的自動化 golden 測試」**（74 全綠佐證）；golden 是 `scripts/bench_golden.py`（拋棄式）+ server 手動基準。故影響為「外部持有的參考指紋已過期」，**非 CI 破壞**。
- **建議（未執行）**：server 用 bench_golden 重跑、更新參考基準。

### 🟡 R3（低-中）UXsim 號誌無黃燈/清道，且與 legacy 不一致
- builder 傳 `signal=[half, half]`（無 all-red）；`yellow_s` 僅 legacy `is_green` 用 → 兩引擎號誌語意不同、UXsim 無清道相位。

### 🟡 R4（低，校準非錯誤）每車道飽和容量偏高
- per-lane 後單車道 ≈ 2864 veh/h（vs 現實 1800–2000，高約 43–59%）。paper 報絕對值需校準 `reaction_time`（runtime、免重建）。

### 🟢 R5（低，code-quality）廣域 `except Exception` 吞例外約 31 處〔loop 3 新增〕
- 分布：`uxsim_engine.py` 11、`websocket.py` 6、`road_network.py` 4、`engine.py` 4、其餘零星。
- 風險：部分 `except Exception: pass`（如 readback 逐車、wkt 解析）**可能靜默遮蔽真錯**（NaN 位置、資料缺漏不報）。多數屬刻意防禦（單台車失敗不該炸整步），非 bug。
- **建議（未執行）**：抽樣檢視 `pass`-only 者，至少加 `logger.debug` 留痕，避免完全無聲。

### 🟢 R6（低，驗證缺口）部分設定參數無範圍驗證〔loop 4 新增〕
- **已驗證的**：`jam_density>0`、`deltan≥1`、`cycle_s>0`、`0≤yellow_s<cycle_s/2`、max_steps/step_minutes 等（`config.py` validate）。
- **未驗證的**：`arrival_radius_m`、`arrival_gates_per_car`、`dest_pool_per_capita`、`reaction_time` 無範圍檢查。負值/零時多為**靜默回退**（半徑過小→回退單一球場節點；非崩潰）→ 低風險，但屬驗證缺口（錯設不會被即時擋下）。
- **建議（未執行）**：在 `_validate` 加上這幾項的正數/範圍檢查，與既有風格一致。

---

## 2. 已知模型簡化（非 bug，僅記錄）

- **M1** 多車道 agent 渲染投影到單一中線（per-lane 後多車道路點視覺更密；計算正確）。
- **M2** 奇數 OSM lanes 經 `round()`（banker's）：lanes=3→每向 2、lanes=5→每向 2。
- **M3** `arrival_radius_m = 600`（toml）偏小；先前實測建議 1200–1500 紓解球場 funnel。
- **M4** shapefile/graphml `capacity = lanes×12` 為 legacy 佔位欄，runtime 覆寫為 `kappa×length`；匯出靜態 capacity 欄不可作分析依據。
- **M5** 球場前 funnel/spillback 為真實匯聚現象（非 bug）。

---

## 3. 待決 / 待辦

- **T1** Option B（lanes/speed 回退邏輯搬 runtime，改 config 免重建）— 未拍板。
- **T2** legacy `is_green` 在 60s 邊界離散取樣對 90s 週期 aliasing → **僅 legacy**；UXsim 不受影響。
- **T3** 球場塞爆路口瓶頸定位（走廊容量 vs 綠燈 vs 下游 spillback）未量化。

---

## 4. 自我審核（複審報告論述合理性）

**loop 6 調查與複審：**
- ✅ **前後端 payload 一致性（loop 5 延後項）→ 已驗證通過**：前端在 `simulation_web/frontend/`（非 src 下）。`map.js` 取用 `roads_geojson`/`arrival_radius_m`/`.signals`/`cycle_s`，`charts.js`/`simulation.js` 取用 `congestion`/`agents`——皆與後端 `engine.py:1955` 的 `init_payload` 及 snapshot 欄位**對得上，無不一致**。
- ✅ **metrics V/C 同源（M4）→ 已確認**：`metrics.py` 僅用 `r.congestion_proxy`（= n/(kappa×length)），無另一套會分歧的 V/C 計算 → 報表與物理單一來源，M4「單一容量來源」成立。
- ✅ R1–R6 本輪無新矛盾；無高風險新發現。專案持續呈現健康。

**loop 5 調查與複審：**
- 🔎 **R7 候選（manifest 路徑注入）→ 調查後排除（虛驚）**：`/api/scenarios` 上傳端點（`app.py:170-187`）對 `key` 做 `re.sub` 嚴格淨化、`road_graphml` 為**伺服器端計算路徑**（`SCENARIOS_DIR/{key}_roads.graphml`）非使用者原始輸入，且 graphml 內容經格式驗證 + 失敗清除。`register_manifest` 唯一呼叫者即此端點，路徑安全 → **無注入，不列為缺陷**。此端點防護其實完善（淨化+路徑控制+驗證）。
- ⏸ 前後端 payload 欄位一致性：本輪 grep 找錯層（欄位在 engine/snapshot 非 web/），**未能驗證，延後**。
- ✅ R1–R6 本輪無新矛盾。
- 📊 **5 輪小結（誠實定調）**：尚**未發現任何高嚴重度功能性 bug**；現存項目皆為 ① 待辦（R1 重建/R2 golden）、② 校準（R4）、③ 穩健性/code-quality（R5/R6）。專案整體健康；報告應持此基調，勿把低風險項渲染成嚴重缺陷。

**loop 4 對 loop 3 的複審結果：**
- ➕ 新增 R6（設定驗證缺口）。`websocket.py` 本輪複查：有 `_send_lock` 序列化送出 + `_run_task.cancel()` 停止取消 → 防禦合理，**未發現 race/資源洩漏**（正面結論）。
- ✅ R1–R5 本輪重讀無新矛盾；R5 所述「readback except 屬刻意防禦」與本輪 websocket 防禦風格一致，論述穩固。
- 📌 注意：R6 與 R5 都偏「code-quality/穩健性」，非功能性 bug；報告應避免把這類列得像嚴重缺陷（已用 🟢 低標示）。

**loop 3 對 loop 2 的複審結果：**
- ✏ **修正 R2 措辭**：loop 1–2 寫「golden 基準與物理不一致」暗示有基準會壞；loop 3 查證 repo 內**無自動化 golden 測試**（74 全綠），golden 僅 bench 腳本/手動 → 改為「外部參考指紋過期，非 CI 破壞」，嚴重度降為「低-中」。
- ➕ 新增 R5（except 吞例外）；`demand.py` 本輪複查**無 bug**（邊界有守，列為正面結論）。
- ✅ R1、R3、R4 本輪重讀無新矛盾。

**loop 2 對 loop 1 的複審結果（保留）：**
- ✏ **修正 R1 的誇大**：loop 1 把 R1 標為「中-高」、暗示影響主要道路；loop 2 查證後**降為「中」**——`default` 只影響未列出的少數路型，**不碰 primary/secondary**。原措辭過度放大影響面，已修正。
- ⬆ **R1 信心 升級**：loop 1 為「依口述、未核 toml」；loop 2 已核對本機 toml（default lanes 1→2）+ graphml 日期（6/15 早於變更）→ **確認屬實**。
- ✅ 其餘 R2–R4、M1–M5 論述本輪重讀無矛盾；R4 數字（43–59%）已補精確區間。

**高信心（已驗證）**：per-lane 線性、容量公式 assert、號誌機制（uxsim.py:277）、compileall/pytest/git 狀態、R1（toml+graphml 日期）。
**中信心（推論）**：M5/T3 funnel=spillback 未對特定節點實測。
**盲點**：① 無 ruff（風格/未用變數未檢）；② 重模擬在 server，未涵蓋執行期錯誤（OOM/數值）。

---

## 5. 下一個 loop 的查核重點

1. 抽樣讀 `uxsim_engine._readback` 的 `pass`-only except 內容，判斷是否遮蔽真錯（R5 細查）。
2. 檢查 `intervention`/介入事件套用（降容量/降速）是否正確改到 UXsim link、且可還原（呼應介入擴充計畫）。
3. 檢查 detectors（相機）計數在 deltan=1 與重生背景車情境下是否重複/漏計。
4. 持續複盤：維持「無高風險 bug」的誠實定調；考慮精簡報告中過舊的逐輪複審段落以維持可讀性。
