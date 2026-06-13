"""calibrate.py — 量測 decision prompt 的實際 token 用量，建議 vLLM 設定與安全 batch。

用法：

    python -m llm_abm_simulator.calibrate                 # 用字元粗估（不需額外套件）
    python -m llm_abm_simulator.calibrate --model <HF模型> # 有 transformers 時用該模型真 tokenizer 精算

流程：用 mock 引擎跑幾步產生「真實的 payload + persona」，組出與正式 decision prompt 完全一致的
文字，量出「固定開銷」與「每 agent token」，再依 [llm_budget].max_model_len 反推：
  - 對齊現有 [scaling].batch_size 需要多大的 max-model-len
  - 在現有 max_model_len 下安全的 batch_size
並印出可直接用的 ``vllm serve`` 參數建議。純量測、不呼叫 LLM、不改任何狀態。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging

from . import config


def _make_tokenizer(model: str | None):
    """有 transformers + 該模型時回真 tokenizer，否則回 None（改用字元粗估）。"""
    if not model:
        return None
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(model)
    except Exception as e:  # noqa: BLE001
        logging.warning("無法載入 %s 的 tokenizer（%s），改用字元粗估", model, e)
        return None


def _count(tok, text: str, chars_per_token: float) -> int:
    if tok is not None:
        try:
            return len(tok.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return round(len(text) / chars_per_token)


def _build_decision_prompt(env: dict, statuses: list, personas: list) -> str:
    """組出與 llm_server.decision_making 正式送出完全一致的 prompt 文字。"""
    from llm_server import decision_making as dm
    from llm_server.perception import run_perception

    perception = run_perception({"environment": env, "agents_status": statuses})
    agent_profile = json.dumps({"agents": personas}, ensure_ascii=False)
    user = f'{dm.USER_PROMPT} \n\n {perception}\n "agent profile資料"如下:\n {agent_profile}'
    return dm.SYSTEM_PROMPT + "\n" + user


def measure(n_sample: int = 40, model: str | None = None) -> dict:
    # 量測用規則式核心（use_llm=False）→ 不會觸發 LLM 記憶摘要
    from .decisions import profile_pool
    from .simulation.engine import SimulationEngine

    cfg = dataclasses.replace(config.DEFAULT_CONFIG, nb_agents=n_sample, use_llm=False, max_steps=5)
    eng = SimulationEngine(cfg)
    eng.initialize()
    for _ in range(3):
        eng.step()
    agents = eng.agents
    env = eng._llm_environment(eng._environment_summary(3))
    statuses = [a.build_api_payload() for a in agents]

    pool = profile_pool.load_pool()
    if pool:
        personas_all = [pool[i % len(pool)] for i in range(len(agents))]
    else:
        logging.warning("persona 池不存在，persona 大小用預設估計（建議先生成池再校準）")
        personas_all = [{"identity": {"name": "範例", "age": "三十歲", "occupation": "上班族",
                                      "vehicle_ownership": "汽車", "residential_location": "東區"},
                         "traits": {"attitudes": ["願意遵守交通規則"], "habits": ["習慣提早出發"],
                                    "decision_making_tendencies": ["遇塞會找替代道路"],
                                    "economic_preferences_and_tradeoffs": ["願為省時付費"]}}] * len(agents)

    b = config.LLM_BUDGET
    tok = _make_tokenizer(model)
    cpt = b.chars_per_token

    # 用「1 個 agent」與「n 個 agent」兩點，回歸出固定開銷與每 agent token
    def prompt_tokens(k: int) -> int:
        k = max(1, min(k, len(agents)))
        return _count(tok, _build_decision_prompt(env, statuses[:k], personas_all[:k]), cpt)

    k1, k2 = 1, len(agents)
    t1, t2 = prompt_tokens(k1), prompt_tokens(k2)
    per_agent = max(1.0, (t2 - t1) / (k2 - k1)) if k2 > k1 else float(t1)
    overhead = max(0.0, t1 - per_agent)  # 固定開銷（模板+全域）

    avail = b.max_model_len - b.reserve_output_tokens - b.prompt_overhead_tokens
    safe_batch = max(1, int((b.max_model_len - b.reserve_output_tokens - overhead) // per_agent))
    need_len_for_cfg = int(overhead + config.SCALING_CONFIG.batch_size * per_agent
                           + b.reserve_output_tokens)

    return {
        "tokenizer": ("真實:" + model) if tok else "字元粗估",
        "sample_agents": len(agents),
        "overhead_tokens": round(overhead),
        "per_agent_tokens": round(per_agent, 1),
        "max_model_len": b.max_model_len,
        "reserve_output_tokens": b.reserve_output_tokens,
        "cfg_batch_size": config.SCALING_CONFIG.batch_size,
        "cfg_concurrency": config.SCALING_CONFIG.concurrency,
        "safe_batch_for_current_len": safe_batch,
        "need_max_model_len_for_cfg_batch": need_len_for_cfg,
    }


def _round_up(n: int, step: int = 1024) -> int:
    return ((n + step - 1) // step) * step


def main() -> None:
    parser = argparse.ArgumentParser(description="量測 token 用量並建議 vLLM 設定 / 安全 batch")
    parser.add_argument("--agents", type=int, default=40, help="取樣 agent 數（越多回歸越準）")
    parser.add_argument("--model", type=str, default=None, help="HF 模型名：給定則用其真 tokenizer 精算")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    r = measure(n_sample=args.agents, model=args.model)
    rec_len = _round_up(r["need_max_model_len_for_cfg_batch"])

    print("=" * 60)
    print("LLM token 量測結果（decision prompt）")
    print("=" * 60)
    print(f"  tokenizer            : {r['tokenizer']}")
    print(f"  取樣 agent           : {r['sample_agents']}")
    print(f"  固定開銷             : ~{r['overhead_tokens']} tok（模板+全域）")
    print(f"  每 agent             : ~{r['per_agent_tokens']} tok（status+persona）")
    print("-" * 60)
    print(f"  目前 max_model_len   : {r['max_model_len']}（保留輸出 {r['reserve_output_tokens']}）")
    print(f"  目前 batch_size      : {r['cfg_batch_size']}")
    print(f"  → 此 max_model_len 下安全 batch ≤ {r['safe_batch_for_current_len']}")
    if r["cfg_batch_size"] > r["safe_batch_for_current_len"]:
        print(f"  ⚠ 你的 batch_size={r['cfg_batch_size']} 會超過預算！建議降到 {r['safe_batch_for_current_len']}，"
              f"或把 max_model_len 提高到 {rec_len}")
    print("-" * 60)
    print("建議（要維持目前 batch_size 的話）：")
    print(f"  [llm_budget] max_model_len = {rec_len}")
    print(f"  vllm serve <HF模型> --port 8001 \\")
    print(f"    --max-model-len {rec_len} --gpu-memory-utilization 0.9 \\")
    print(f"    --max-num-seqs {max(r['cfg_concurrency'], 64)}")
    print("  （--max-num-seqs 設成 ≥ concurrency 的寬鬆值即可，vLLM 會依 VRAM 自動收斂）")
    print("=" * 60)


if __name__ == "__main__":
    main()
