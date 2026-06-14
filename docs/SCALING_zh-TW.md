# 規模化設計（讓 LLM 決策跑得動城市尺度）

本文件說明「**讓 LLM 驅動的微觀交通 ABM 擴展到大量 agent（1000+）且可即時互動**」的設計。

核心問題：原本**每步對全部 agent 同步呼叫一次 LLM** → agent 多就(1)上下文塞不下、(2)太慢。
解法是三個互補的機制，讓 **LLM 成本與「agent×步數」脫鉤 → 變成「決策事件數」**：

1. **後端 adapter（可切 Ollama / vLLM）** — ✅ 已實作
2. **事件觸發決策**（只有需要時才叫 LLM） — ✅ 已實作
3. **同步並行批次推論**（scatter–gather） — ✅ 已實作

---

## 1. 後端 adapter（✅ 已實作）

`llm_server/llm_client.py` 把「怎麼把 prompt 送出去」集中成單一入口 `generate()`，
3 個 LLM 呼叫點（decision_making / agent_profile / memory_summary）共用。
> 註：`perception` 已改為**確定性模板**（不再呼叫 LLM），故每批只剩 **decision_making** 一次 LLM 呼叫。
> `generate()` 另支援 `fmt`（結構化輸出 JSON schema：Ollama `format` / vLLM `guided_json`）。詳見 `docs/CHANGES_LLM_PIPELINE_zh-TW.md`。

| 後端 | 端點 | 用途 |
|---|---|---|
| `ollama`（預設） | 原生 `/api/generate` | **行為與原本完全一致**、零回歸；開發/桌面 |
| `vllm` | OpenAI 相容 `/v1/chat/completions` | **continuous batching 高並行**；Linux/GPU demo |

切換只改 `.env`：
```env
LLM_BACKEND=ollama        # 或 vllm
VLLM_URL=http://127.0.0.1:8001
VLLM_MODEL=<HF 模型名>
```

**generate vs chat**：Ollama 路徑維持原生 generate（單一 prompt，行為不變）；vLLM 路徑走 chat
（system→system message、prompt→user message，並把 Ollama options 映射成 OpenAI 參數
`temperature`/`seed`/`max_tokens`←`num_predict`/`top_k`→`extra_body`）。chat 對 69更對齊。

### vLLM 伺服器啟動（在 Linux/GPU 機，與模擬器分開的環境）
```bash
uv venv .vllm-env --python 3.12
uv pip install --python .vllm-env vllm ninja   # ninja 給 FlashInfer 取樣 kernel 的 JIT 編譯用
# activate 後 .vllm-env/bin 才進 PATH（vllm 與 ninja 都才找得到）
source .vllm-env/bin/activate
vllm serve <HF模型> \
  --port 8001 --max-model-len 8192 \
  --gpu-memory-utilization 0.9 --max-num-seqs 64
```
> ⚠️ **不要用 `uv run --python .vllm-env vllm serve`**：在專案目錄裡 `uv run` 是「專案感知」的，會去
> 動/重建專案自己的 `.venv`（裡面沒有 vllm/ninja）而非用 `.vllm-env`，導致 `Failed to spawn: vllm`。
> **務必 `source .vllm-env/bin/activate` 後再 `vllm serve …`**（這樣 `.vllm-env/bin` 進 PATH，vllm 與 ninja 都找得到）。
> **為何要 ninja**：vLLM 用 FlashInfer 做 top-k/top-p 取樣，會在啟動時 JIT 編譯一個 CUDA kernel，需要 `ninja`；
> 沒裝會在載完模型後崩於 `FileNotFoundError: 'ninja'`（`Engine core initialization failed`）。
> JIT 還需要 `nvcc`（CUDA toolkit）；若補完 ninja 又報缺 nvcc，可改用**不編譯**的後備：
> `VLLM_USE_FLASHINFER_SAMPLER=0 vllm serve …`（改用 PyTorch 原生取樣，免 ninja/nvcc，最省事）。
> `--max-num-seqs` 是真並行度上限；`--max-model-len` 設小可留更多 VRAM 給並行。模型用 HF 格式（非 GGUF）。
> 很新的 GPU（如 RTX 5090 / Blackwell, sm_120）需較新的 vllm + CUDA 12.8+ 的 torch；若見 `no kernel image`/
> `sm_120 not supported` 即為版本太舊，需升級 vllm/torch。
> vLLM 是現成的高吞吐推論伺服器，**不是本專案的研究貢獻**；貢獻在「應用層的事件觸發 + 並行批次整合」。

---

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
- **初始 mode = 規則式核心預設**：開場不對 1000 台一次決策；LLM 在第一次觸發才介入。觸發時順手用 LLM 重寫該車記憶 summary（見 `docs/MEMORY_zh-TW.md`）。
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
>   開場不呼叫 LLM 決策；初始 active_mode 用規則式核心。Ollama 不可用時 fallback 規則式人物指派。
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
- **③ 鄰近車數網格化**（`engine._build_nearby_grid`/`_count_nearby`，`[perception].nearby_mode`）：
  每步 O(車數²) 全比對 → 公尺方格桶 O(車數)。`grid`（預設、近似、只餵 LLM 感知）/ `exact`（精確、可還原舊值對照）。
- **⑦ `current_town` 查表 O(1)**（`engine._current_town`，`[perception].town_mode`）：原本每步每台車對 37 區做
  點在多邊形內（O(車數×區數)，2 萬台 ≈ 每步 148 萬次 shapely）→ **重用 ① 的索引反向表**（`_node_town`：節點→區），
  current_town = 所在節點所屬區、查表 O(1)（反向表在 ① 既有索引迴圈內順手建，額外成本 ≈ 0）。
  `node`（預設）/ `exact`（精確內插位置、可還原舊值）。近似只在**行政區交界**差一個區，區內部相同；不影響軌跡。
- **④ 記憶摘要分批**（`engine._summarize_memory`）：原本把所有觸發車塞一個 prompt（大規模爆 context）→
  比照決策用 `_budget_batch_size` 分批、可並行。**不設「每步重決上限」**（依使用者決定，保留所有觸發車重決）。
- **⑤ persona 池記憶體快取**（`profile_pool` `_POOL_CACHE`）：`personas_json` 每決策批次都呼叫 → 不再每批
  重讀+重解析池檔（2 萬 persona 大檔尤其有感）；`save_pool` 更新、`clear_pool` 清。
- **⑥ 前端 zoom/可視範圍裁切**（`engine._visible_agents`/`set_view`，`[ui].render_individual_max`/`agent_min_zoom`）：
  車數 ≤ 門檻 → 逐台送/畫；超過 → zoom out 只送道路壅塞、zoom in 只送「可視範圍內」的車（公尺框過濾，
  經緯度只算這批）。把 WS 流量與前端繪製綁在「可視範圍」而非總車數。`set_view` 收到後**立即回推 `snapshot_now()`**
  → zoom/pan 即時顯示（不等慢的模擬步）；送訊息加 `asyncio.Lock` 序列化。前端：道路線寬依 zoom 縮放+半透明、
  車輛畫在高 z `agentPane`（永遠在道路之上）。詳見 `docs/DEMO_FEATURES_zh-TW.md`。

> 驗證:`nearby_mode="exact"` + `town_mode="exact"` 時模擬結果與舊版一致(回歸基準);① 經 determinism / 計數測試確認未破。
> 路徑規劃(每台一次 Dijkstra)實測便宜(~14ms/台),**未改**;若日後每步重算路成瓶頸再評估「終點最短路徑樹」。

## 7. 研究定位（誠實）
vLLM / continuous batching 是現成基礎設施，不是貢獻。**貢獻在應用層**：
> 「以**事件觸發 + 同步並行批次**的 LLM 決策管線，使 LLM 驅動的微觀交通 ABM 的 LLM 成本 ∝ 決策事件數
> 而非 agent×步數，讓城市尺度可即時互動」+ 量測到的 scalability 曲線。
