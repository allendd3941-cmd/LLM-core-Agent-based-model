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
4 個呼叫點（perception / decision_making / agent_profile / memory_summary）共用，**prompt 內容不變**。

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
uv pip install --python .vllm-env vllm
uv run --python .vllm-env vllm serve <HF模型> \
  --port 8001 --max-model-len 8192 \
  --gpu-memory-utilization 0.9 --max-num-seqs 64
```
> `--max-num-seqs` 是真並行度上限；`--max-model-len` 設小可留更多 VRAM 給並行。模型用 HF 格式（非 GGUF）。
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
- **初始 mode = 規則式預設**（mock）：開場不對 1000 台一次決策；LLM 在第一次觸發才介入。
- **mock 模式不變**：mock 便宜且確定性，維持「每步決策」；觸發機制只套用在 LLM 模式。

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
batch_size = 30         # B：每批最多幾個 agent（吃 context 預算）
concurrency = 4         # C：同時並行幾批（搭配後端真並行上限）
```
（後端連線 `LLM_BACKEND` / `VLLM_URL` 在 `.env`；門檻沿用 `[perception]` 的 `crowded_road_threshold`、`[memory]` 的 `feel_congested_proxy`。）

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
>   開場不呼叫 LLM 決策；初始 active_mode 用規則式（mock）。Ollama 不可用時 fallback mock 人物。
> - **批次決策**只送該批 agent 的 persona（`profile_pool.personas_json`），與 agents_status 對齊。
> - **安全開關** `[scaling].event_triggered_decisions=false` 可一鍵退回「每步決策全部」舊行為。
> - **已知小限制**：並行批次同時寫 `output/*_output_N.txt` 時，pipeline 內的全域 `count` 有競爭，
>   decision 輸出檔編號可能交錯（決策本身正確；僅影響「decision 輸出檢視」的檔案順序）。

---

## 6. 研究定位（誠實）
vLLM / continuous batching 是現成基礎設施，不是貢獻。**貢獻在應用層**：
> 「以**事件觸發 + 同步並行批次**的 LLM 決策管線，使 LLM 驅動的微觀交通 ABM 的 LLM 成本 ∝ 決策事件數
> 而非 agent×步數，讓城市尺度可即時互動」+ 量測到的 scalability 曲線。
