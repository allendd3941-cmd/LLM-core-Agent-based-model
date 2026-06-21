# 規模化設計（讓 LLM 決策跑得動城市尺度）

本文件說明「**讓 LLM 驅動的微觀交通 ABM 擴展到大量 agent（1000+）且可即時互動**」的設計。

核心問題：原本**每步對全部 agent 同步呼叫一次 LLM** → agent 多就(1)上下文塞不下、(2)太慢。
解法是三個互補的機制，讓 **LLM 成本與「agent×步數」脫鉤 → 變成「決策事件數」**：

1. **LLM 後端：vLLM**（OpenAI 相容、continuous batching 高並行） — ✅ 已實作
2. **事件觸發決策**（只有需要時才叫 LLM） — ✅ 已實作
3. **同步並行批次推論**（scatter–gather） — ✅ 已實作

---

## 1. LLM 後端：vLLM（✅ 已實作）

`llm_server/llm_client.py` 把「怎麼把 prompt 送出去」集中成單一入口 `generate()`，
LLM 呼叫點（decision_making / agent_profile / rag / sim_*）共用，**統一走 vLLM**
OpenAI 相容 `/v1/chat/completions`（continuous batching 高並行）。
> 註：`perception` 已改為**確定性模板**（不再呼叫 LLM），故每批只剩 **decision_making** 一次 LLM 呼叫。
> `generate()` 支援 `fmt`（結構化輸出 JSON schema → vLLM `guided_json` 受限解碼）。

連線設定只在 `.env`（VLLM_MODEL 必填）：
```env
VLLM_URL=http://127.0.0.1:8001
VLLM_MODEL=<HF 模型名>   # 須與 `vllm serve` 啟動的模型一致
```

**transport**：system→system message、prompt→user message，並把 Ollama 風格 options 映射成
OpenAI 參數（`temperature`/`seed`/`max_tokens`←`num_predict`/`top_k`→`extra_body`）；
結構化輸出走 `guided_json`。

### vLLM 伺服器啟動（在 Linux/GPU 機，與模擬器分開的環境）
```bash
uv venv .vllm-env --python 3.12
uv pip install --python .vllm-env vllm ninja   # ninja 給 FlashInfer 取樣 kernel 的 JIT 編譯用
# activate 後 .vllm-env/bin 才進 PATH（vllm 與 ninja 都才找得到）
source .vllm-env/bin/activate
vllm serve <HF模型> \
  --port 8001 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.95 \
  --max-num-seqs 64 \
  --enable-prefix-caching
```
> ⚠️ **不要用 `uv run --python .vllm-env vllm serve`**：在專案目錄裡 `uv run` 是「專案感知」的，會去
> 動/重建專案自己的 `.venv`（裡面沒有 vllm/ninja）而非用 `.vllm-env`，導致 `Failed to spawn: vllm`。
> **務必 `source .vllm-env/bin/activate` 後再 `vllm serve …`**（這樣 `.vllm-env/bin` 進 PATH，vllm 與 ninja 都找得到）。
> **為何要 ninja**：vLLM 用 FlashInfer 做 top-k/top-p 取樣，會在啟動時 JIT 編譯一個 CUDA kernel，需要 `ninja`；
> 沒裝會在載完模型後崩於 `FileNotFoundError: 'ninja'`（`Engine core initialization failed`）。
> JIT 還需要 `nvcc`（CUDA toolkit）；若補完 ninja 又報缺 nvcc，可改用**不編譯**的後備：
> `VLLM_USE_FLASHINFER_SAMPLER=0 vllm serve …`（改用 PyTorch 原生取樣，免 ninja/nvcc，最省事）。
> `--max-num-seqs` 是真並行度上限；`--max-model-len` 設小可留更多 VRAM 給並行。模型用 HF 格式（非 GGUF）。
> **`--enable-prefix-caching`**：重用「跨請求共用的 prompt 前綴」KV，城市尺度（同步上千個共用前綴的決策請求）可省大量 prefill；
> ✅ **已實作（Option A）**：決策 prompt 已把「每步都一樣的全域路況」移到 system＋template 之後當共用前綴
> （`decision_making.py`：`global_situation_text` 放前、per-batch RAG 與各車狀況放後）→ 整步所有批共用此前綴、可被快取。
> 要再往上拉命中率：縮短各車狀況/persona、或把 RAG 也改每步一次（Option B）——這些會改 LLM 行為，建議先用 `calibrate.py` 量再決定。
> 若前綴快取命中率偏低（如僅 ~20%），多半是 prompt 把每批/每車變動內容放太前面、或 KV 太小被逐出 → 拉高 `--gpu-memory-utilization`（0.9→0.95）給 KV 更多空間。
> **與專案對齊**：server `--max-model-len` 要 ≥ `config/simulation.toml [llm_budget].max_model_len`；`--max-num-seqs` 要 ≥ `[scaling].concurrency`。
> 很新的 GPU（如 RTX 5090 / Blackwell, sm_120）需較新的 vllm + CUDA 12.8+ 的 torch；若見 `no kernel image`/
> `sm_120 not supported` 即為版本太舊，需升級 vllm/torch。
> vLLM 是現成的高吞吐推論伺服器，**不是本專案的研究貢獻**；貢獻在「應用層的事件觸發 + 並行批次整合」。

---

## 1.5 城市尺度路由（反向終點樹）+ 連線存活（✅ 已實作）

**反向終點樹路由**（`[scaling].route_trees`，預設 true；false＝退回逐車 `find_path` 對照/除錯）：
**init 與「中途壅塞重算」都用** scipy `csgraph.dijkstra` 反向最短路樹——同一 action_mode 共用一張權重圖、
每個終點只算一次反向樹、每車沿前驅讀路徑(惰性只建用到的 mode×終點)。事件車去球場(1 終點)、背景車終點
收斂到**區代表節點**(~區數個終點)。權重 = mode + **當前壅塞** + road_class_bias + **avoid_circles** + 固定 salt,
與 `find_path` **共用同一成本公式**(`spatial/routing.py` `_edge_cost`)→ 兩者等價,只差 per-car jitter。
- **init**(`engine._place_routes_via_trees`)：congestion=0、無 avoid_circles → 自由流樹。
- **中途重算**(`engine._reroute_via_trees`，每步移動前批次)：用當前壅塞 + avoid_circles 建樹,取代逐車
  networkx。把「每步上萬次 Dijkstra」→「數十棵樹」(城市尺度每步從分鐘級 → 秒級)。
- **reroute cooldown**：同車 `[scaling].reroute_cooldown_minutes`(預設 10 分鐘)內不重算,降 churn。事件+背景一視同仁。
- **會改結果**:同 mode+終點走相同(當前壅塞下)最短路、無 jitter;背景車去區代表節點。`route_trees=false` 可回到逐車對照。
- 路由是 CPU(非 GPU)工作;5090 對路由沒幫助,靠演算法(樹)。
- **cooldown 改「模擬分鐘」**：`cooldown_minutes`(LLM 決策)、`reroute_cooldown_minutes`(重算)皆以分鐘計、與 step_minutes 無關。

**WebSocket init keepalive**(`web/websocket.py`)：`initialize` 放背景執行緒跑、每隔幾秒送進度,
避免長 init(無資料流動)被瀏覽器/代理 idle timeout 斷線;並接住斷線後的 `RuntimeError`,安靜收尾不噴 traceback。

## 2. 事件觸發決策（✅ 已實作）

**只有「跟路徑策略有關的事件」才叫 LLM**；移動物理（紅燈、跟車、巡航）都不叫。

| 情況 | 觸發 LLM？ |
|---|---|
| 路段中段壅塞（is_crowded 上升緣） | ✅ |
| 前方路徑新出現塞點（road_ahead 變壅塞） | ✅ |
| 被管制封路（政策沙盒，未來） | ✅ |
| 紅燈 / 路口排隊（號誌，未來） | ❌ |
| 順暢巡航 / 已抵達 | ❌ |

- **cooldown / 遲滯**：同車觸發一次後 `cooldown_steps` 步內或同一壅塞 episode 內不重複觸發（避免卡長龍狂叫）。
- **初始 mode = 規則式核心預設**：開場不對 1000 台一次決策；LLM 在第一次觸發才介入。記憶 `summary` 一律確定性模板、不呼叫 LLM（見 `docs/MEMORY_zh-TW.md`）。
- **規則式核心不變**：規則式便宜且確定性，維持「每步決策」；事件觸發機制只套用在 LLM 核心。
- **背景常態車流（ambient）**：一律規則式核心、每步決策、不吃 LLM、不存記憶；不進事件觸發、不算事件 KPI（只造成路網層負載）。見 `docs/AMBIENT_zh-TW.md`。

> 待你確認：對「整趟都沒遇到壅塞」的車，LLM 要不要至少決策一次/週期性決策？見文末。

---

## 3. 同步並行批次推論（scatter–gather，✅ 已實作）

決策**仍在該步同步完成**（算完才推前端一次），但批次**並行**跑 → 比「一個大 call」快、又每批塞得進 context。

```
engine.step()：
  感知（確定性、無 LLM）
  → 偵測「觸發」的 agent（觸發器 + cooldown）
  → 若有：切批（每批 ≤ B）→ 執行緒池並行送 C 批 → 等齊 → 依 agent_id 排序套用新 mode
       若無：略過（這步 0 次 LLM、全速）
  → 移動 / 重算 flow / 更新記憶 / 推 snapshot
```
- **同步等待**：沒有背景 worker 在移動中改狀態 → 無 race；單執行緒邏輯、好維護。
- **並行**：LLM 呼叫是 I/O bound，執行緒池同時送多批，真並行度受後端約束（Ollama `NUM_PARALLEL` / vLLM `--max-num-seqs`）。
- **determinism**：一步內 gather 後**按 agent_id 套用** → 物理可重現（LLM 文字本身非確定，照舊）。
- **先不做桶去重**（之後當「更大規模」的可選旋鈕）。

---

## 4. 可調參數（規劃）
```toml
[scaling]
cooldown_steps = 5      # 同車觸發後幾步內不重複觸發
batch_size = 30         # B：每批最多幾個 agent（**上限**；實際會被 [llm_budget] 的 token 預算再壓低）
concurrency = 4         # C：同時並行幾批（搭配後端真並行上限）

[llm_budget]            # 依 token 預算動態切批，保證 decision prompt 不超過 max_model_len
max_model_len = 8192    # 對齊 vLLM --max-model-len
reserve_output_tokens = 1024
prompt_overhead_tokens = 800
chars_per_token = 2.0   # 字元→token 粗估比；用 `python -m llm_abm_simulator.calibrate` 量更準
```
（後端連線 `LLM_BACKEND` / `VLLM_URL` 在 `.env`；門檻沿用 `[perception]` 的 `crowded_road_threshold`、`[memory]` 的 `feel_congested_proxy`。）
> 實際每批量＝`min([scaling].batch_size, 由 [llm_budget] 反推的安全批量)`，引擎每步 INFO 日誌會印出採用值。

---

## 5. 分階段
| 階段 | 內容 | 狀態 |
|---|---|---|
| 0 | `llm_client` adapter + 後端切換 | ✅ 完成 |
| 1 | 事件觸發 + cooldown + 規則式初始 mode（persona 池確定性指派） | ✅ 完成 |
| 2 | 並行多批（執行緒池 scatter–gather）+ 確定性回填 | ✅ 完成 |
| 3 | 視 Ollama 是否飽和 → 換 vLLM；量完整 scalability 曲線 | ⏳ |
| 後續 | 桶去重、小決策模型、分批出發時間分佈 | 之後 |

> **實作備註**：
> - **init persona 指派**改成確定性（`profile_pool.assign_to_agents`，agent i ← pool[i]），
>   開場不呼叫 LLM 決策；初始 action_mode 用規則式核心。Ollama 不可用時 fallback 規則式人物指派。
> - **批次決策**只送該批 agent 的 persona（`profile_pool.personas_json`），與 agents_status 對齊。
> - **安全開關** `[scaling].event_triggered_decisions=false` 可一鍵退回「每步決策全部」舊行為。
> - **已知小限制**：並行批次同時寫 `output/*_output_N.txt` 時，pipeline 內的全域 `count` 有競爭，
>   decision 輸出檔編號可能交錯（決策本身正確；僅影響「decision 輸出檢視」的檔案順序）。

---

## 6. 引擎規模化（往 1～2 萬台事件車；2026-06）

LLM 那層已可擴（上面的事件觸發+批次）；真正擋住大規模的是**引擎的純計算與 I/O**。以下優化讓
「擋住 2 萬台」的牆退場（皆可開關、不改物理結果者直接做）：

- **① 節點→行政區索引一次**（`engine._build_town_node_index`/`_node_in_town`）：放置不再每台車掃全節點做
  shapely（O(節點×車數)）→ init 時把每個節點歸到「覆蓋它的區」一次（與 `random_node_in_town` 的
  `covers` 判定一致 → **結果與逐台版完全相同**），之後 O(1) 抽。實測 init 2 萬台 **~1 小時 → ~1 分鐘**。
  - **建表本身的加速（往全台南數萬節點）**：原本每區迴圈對每節點重複建 `Point`、且全掃 → 改成
    **(i) Point 預先建一次重用**、**(ii) shapely `STRtree` 以 bbox 先篩候選節點再 `covers` 確認**，
    把建表從 O(節點×區) 降到 ~O(節點)。候選索引昇冪排序映回 `nodes` → **covered 集合與順序與全掃版完全相同**
    （determinism 不變）。實測:研究範圍網（1萬節點）建表 6.4s → 約 0.3s；全台南網每次重設約 1–2s（原會 ~30s）。
- **③ 鄰近車數網格化**（`engine._build_nearby_grid`/`_count_nearby`，`[perception].nearby_mode`）：
  每步 O(車數²) 全比對 → 公尺方格桶 O(車數)。`grid`（預設、近似、只餵 LLM 感知）/ `exact`（精確、可還原舊值對照）。
- **⑦ `current_town` 查表 O(1)**（`engine._current_town`，`[perception].town_mode`）：原本每步每台車對 37 區做
  點在多邊形內（O(車數×區數)，2 萬台 ≈ 每步 148 萬次 shapely）→ **重用 ① 的索引反向表**（`_node_town`：節點→區），
  current_town = 所在節點所屬區、查表 O(1)（反向表在 ① 既有索引迴圈內順手建，額外成本 ≈ 0）。
  `node`（預設）/ `exact`（精確內插位置、可還原舊值）。近似只在**行政區交界**差一個區，區內部相同；不影響軌跡。
- **④ 記憶摘要**（已移除）：原本在重決策時用 LLM 重寫記憶 `summary`（曾比照決策分批）→ 因邊際價值低且多一次 LLM 呼叫，**整支移除**；`summary` 現一律確定性模板（見 `MEMORY_zh-TW.md`）。
- **⑤ persona 池記憶體快取**（`profile_pool` `_POOL_CACHE`）：`personas_json` 每決策批次都呼叫 → 不再每批
  重讀+重解析池檔（2 萬 persona 大檔尤其有感）；`save_pool` 更新、`clear_pool` 清。
- **⑥ 前端 zoom/可視範圍裁切**（`engine._visible_agents`/`set_view`，`[ui].render_individual_max`/`agent_min_zoom`）：
  車數 ≤ 門檻 → 逐台送/畫；超過 → zoom out 只送道路壅塞、zoom in 只送「可視範圍內」的車（公尺框過濾，
  經緯度只算這批）。把 WS 流量與前端繪製綁在「可視範圍」而非總車數。`set_view` 收到後**立即回推 `snapshot_now()`**
  → zoom/pan 即時顯示（不等慢的模擬步）；送訊息加 `asyncio.Lock` 序列化。前端：道路線寬依 zoom 縮放+半透明、
  車輛畫在高 z `agentPane`（永遠在道路之上）。詳見 `docs/DEMO_FEATURES_zh-TW.md`。
- **⑧ 跨連線/重設快取路網與索引**（`road_network._GRAPH_CACHE`、`engine._SPATIAL_INDEX_CACHE`，`[scaling].cache_network`）：
  每開分頁、每次 `set_agents`/`apply_config`/`set_ambient`/重設都會重建引擎；原本每次都重解析 24MB graphml
  XML、重建 ① 的節點→區索引（皆只取決於「場景＝圖+行政區+球場」，與車數/seed 無關）。改成**模組層快取**（鍵＝
  graphml 路徑+mtime，檔案重建即失效，只留最新場景一份）→ 命中即跳過 XML 解析與建表；每引擎仍**各自**持有可變
  `Road.current_flow`（圖在模擬中唯讀）。**不改結果**（共用唯讀確定性產物）；第 2 個分頁起、每次重設的開場時間大幅下降。
- **⑨ init 路由並行（multiprocessing）**（`engine._parallel_init_routes`，`[scaling].init_workers`/`parallel_init_min_agents`）：
  init 要為每台車各算一次起點→終點 Dijkstra（~0.4s/台），原為單執行緒。把「**會抽 rng 的放置**」（主程序依序、保
  determinism）與「**無 rng 的純路徑運算**」（init 時各路段 congestion=0、`find_path` 為純函式、jitter 用 `crc32`
  跨進程一致）拆開後，後者丟**程序池並行**（worker 用 `initializer` 各載一次圖；`pool.map` 保序、依 index 對回
  agent）。預設 `init_workers=0`（單程序）；>1 且車數達門檻才啟用。**Linux 用 fork（圖 copy-on-write、受益最大）**，
  Windows 用 spawn（worker 各載一次圖，仍 spawn-safe）。**不改結果**（已用同 seed 對拍：單程序＝並行逐台相同）。

> 驗證:`nearby_mode="exact"` + `town_mode="exact"` 時模擬結果與舊版一致(回歸基準);① 經 determinism / 計數測試確認未破。
> ⑧⑨ 皆以 `cache_network`/`init_workers` 旗標可關，並以同 seed 對拍確認「快取開/關、單程序/並行」軌跡逐一相同。
> 路徑規劃**每步重算**(每台一次 Dijkstra)實測便宜(~14ms/台),**未改**;init 階段的整批路由則由 ⑨ 並行加速。
> 若日後每步重算路成瓶頸再評估「終點最短路徑樹」（會改路徑語意，需重建基準；見記憶 routing-optimization-plan）。

## 7. 研究定位（誠實）
vLLM / continuous batching 是現成基礎設施，不是貢獻。**貢獻在應用層**：
> 「以**事件觸發 + 同步並行批次**的 LLM 決策管線，使 LLM 驅動的微觀交通 ABM 的 LLM 成本 ∝ 決策事件數
> 而非 agent×步數，讓城市尺度可即時互動」+ 量測到的 scalability 曲線。
