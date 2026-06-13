# LLM Pipeline 效能與可預測性修正（本次改動說明）

日期：2026-06-13

## 動機

批次跑 LLM 決策時發現三個問題：
1. **每批打兩次 LLM**（perception + decision），其中 perception 只是把「已結構化的資料」叫 LLM 再講一遍 → 冗餘。
2. **token 預算不可控**：實測 `batch_size=30` 的 decision prompt 會**超過 8192 context**（安全只能 ~24），會在 vLLM 上溢位。
3. **輸出長度不固定**：自由文字 JSON 難解析、輸出 token 不可預測。

本次依先前討論的 5 點做了專業修正。**只動這 5 點**，不含其他重構。

---

## 5 項改動

### 1. perception 改「確定性模板」，刪除冗餘 LLM 程序
- **改動**：[`src/llm_server/perception.py`](../src/llm_server/perception.py) 不再呼叫 LLM，改由結構化 payload（`environment` + `agents_status`）直接組出環境感知文字。
- **刪除**：`src/llm_server/prompts/perception_prompt.txt`（已無用）。
- **效果**：每批 LLM 呼叫 **2 次 → 1 次**；省掉每批 ~1.3k token 的 perception 模板與其延遲；輸出零隨機、可重現。
- 簽章不變（`run_perception(gama_body, output=False)`），呼叫端無須改；[`llm_adapter`](../src/llm_abm_simulator/decisions/llm_adapter.py) 改為 `run_perception(payload)`（不再落檔）。

### 2. 依 token 預算「動態切批」
- **新增設定** `config/simulation.toml` `[llm_budget]` 與 [`config.LLMBudgetConfig`](../src/llm_abm_simulator/config.py)：
  `max_model_len` / `reserve_output_tokens` / `prompt_overhead_tokens` / `chars_per_token`。
- **引擎**：[`engine._budget_batch_size`](../src/llm_abm_simulator/simulation/engine.py) 依「實際 status+persona 字元 ÷ chars_per_token」估每 agent token，反推「安全 batch」，取 `min([scaling].batch_size, 預算可容納量)`。
- 保證每批 decision prompt 不超過 `max_model_len`。`[scaling].batch_size` 變成**上限**。
- 每步 INFO 日誌會印出實際採用的 batch：`step N · LLM 重決 X 台 → Y 批 ×Z 並行（batch≤B）`。

### 3. 結構化輸出（受限解碼）
- [`llm_client.generate`](../src/llm_server/llm_client.py) 新增 `fmt`（JSON schema）：Ollama 走 `format`、vLLM 走 `extra_body.guided_json`。
- [`decision_making.DECISION_SCHEMA`](../src/llm_server/decision_making.py) 定義決策輸出形狀（`active mode` 用 enum），強制模型只能吐合法 JSON → 輸出 token 可預測、解析成功率提升。
- 對齊：移除 `decision_making_prompt.txt` 範例中的 `residential_location`（出生地已由 persona 決定，決策輸出不再需要它）。

### 4. 全域環境每步只算一次
- 現況即如此：`_apply_step_decisions` 對該步只算一次 `_llm_environment(env)`，再分享給所有批次。
- **誠實說明**：因為各批是「獨立的 LLM 呼叫」，全域文字仍會出現在每批的 prompt 裡（這是必要的，~58 token）；無法跨獨立呼叫只送一次。本點屬「計算只做一次」的確認，非大改。

### 5. 校準 CLI
- 新增 [`python -m llm_abm_simulator.calibrate`](../src/llm_abm_simulator/calibrate.py)：用 mock 引擎產生真實 payload+persona，量出「固定開銷」與「每 agent token」，輸出：
  - 目前 `max_model_len` 下的**安全 batch**
  - 維持目前 `batch_size` 需要多大的 `max_model_len`
  - 可直接用的 `vllm serve` 參數建議
- 有 `transformers` + `--model <HF模型>` 時用**真 tokenizer 精算**，否則用 `chars_per_token` 粗估。

---

## 驗證結果

- perception 模板：輸出正確、可讀（全域 + 各車局部）。
- 校準 CLI：正確抓出 `batch_size=30` 超 8192（安全 ≤24），建議 `max_model_len=10240`。
- 結構化輸出端到端：`use_llm` 模式下 `decision_source=llm`、`last_call_ok=True`，agent 取得 active_mode + reason（llama3.2:1b 在 `format` 受限下吐出合法 JSON）。
- think 容錯（先前修正）仍在：不支援 thinking 的模型自動略過 `think`。

## 使用方式

1. 跑校準取得建議：
   ```
   python -m llm_abm_simulator.calibrate --agents 40            # 粗估
   python -m llm_abm_simulator.calibrate --agents 40 --model <HF模型>  # 精算
   ```
2. 依建議調 `config/simulation.toml` 的 `[llm_budget].max_model_len`（與 vLLM `--max-model-len` 對齊）。
3. vLLM 啟動時 `--max-model-len` 用建議值、`--max-num-seqs` 設 ≥ `[scaling].concurrency`。

## 注意 / 限制

- `chars_per_token=2.0` 是保守粗估；中文實際 token 可能更多。要精準請用 `--model` 跑真 tokenizer 校準。
- 第 4 點的全域文字在獨立批次呼叫間無法只送一次（已誠實說明）。
- 未更動 RAG 相關程式碼（不在本次範圍）。
