"""profile_pool.py — agent persona「原型池」：生成一次、穩定快取、抽樣重用。

設計（對齊使用者決策；詳見 docs/PYTHON_SIMULATOR_zh-TW.md「Persona 池」）：
- persona 是「人物原型（archetype）」，存成穩定的正規化 JSON 池檔（output/agent_profile_output_1.txt）。
- ``pool_size`` 是**原型數上限**（例如 500），與「模擬車數（nb_agents）」分離：
  車數超過池大小時，由呼叫端以 ``pool[i % len]`` **循環重複抽樣**（不會為了車多而生更多 persona）。
- **分批生成**：一次 LLM 呼叫吐不出大量 persona（輸出會截斷），故把 pool_size 切成多批
  （每批數量依輸出預算自動推算），每批帶不同 seed（避免每批生出相同的人、又可重現），
  沿用 ``[scaling].concurrency`` 並行；只在「首次」建池，之後重用、不重生。
- 要整批換人 → 前端「重新生成人物」按鈕（刪池檔再重新初始化即可）。

robust：池檔與 LLM 生成輸出都用 ``llm_server.json_utils`` 強韌解析（結構壞掉/截斷也救得回），
不會因為一筆壞 JSON 就整池作廢。生成失敗回空池，由上層 fallback 到規則式核心。
"""

from __future__ import annotations

import json
import logging

from .. import config

logger = logging.getLogger(__name__)

POOL_FILENAME = "agent_profile_output_1.txt"

# 記憶體快取：避免每個決策批次都重讀+重解析池檔（2 萬 persona 的大檔尤其有感）。
# 在 save_pool 更新、clear_pool 清空，確保「重新生成人物」仍正常。
_POOL_CACHE: list[dict] | None = None


def _pool_path():
    return config.OUTPUT_DIR / POOL_FILENAME


def _is_persona(o: object) -> bool:
    return isinstance(o, dict) and "identity" in o


def load_pool() -> list[dict]:
    """讀正規化 persona 池（robust 解析）。檔案不存在/無法解析回 []。

    首次讀檔後**快取在記憶體**，之後直接回快取（`personas_json` 每決策批次都呼叫 → 不再重讀大檔）。
    """
    global _POOL_CACHE
    if _POOL_CACHE is not None:
        return _POOL_CACHE
    from llm_server import json_utils

    path = _pool_path()
    if not path.exists():
        _POOL_CACHE = []
        return _POOL_CACHE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("讀取 persona 池失敗：%s", e)
        return []
    _POOL_CACHE = [o for o in json_utils.salvage_objects(text) if _is_persona(o)]
    return _POOL_CACHE


def save_pool(pool: list[dict]) -> None:
    global _POOL_CACHE
    path = _pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": pool}, ensure_ascii=False, indent=2), encoding="utf-8")
    _POOL_CACHE = list(pool)   # 同步快取，避免下次又重讀檔


# 一個 persona JSON 約略字元數（identity 7 短欄 + traits 4 短句 + 標點）；用來依輸出預算推每批數量。
_PERSONA_CHARS_EST = 320


def _gen_batch_size() -> int:
    """每批生成幾個 persona —— **依輸出預算自動推算**（不寫死）。

    persona 生成的瓶頸是**輸出**長度（一次吐太多會截斷）。可用輸出 token ≈
    effective_max_model_len − prompt_overhead；每 persona token ≈ _PERSONA_CHARS_EST / chars_per_token。
    取 0.6 安全係數、clamp 到 [5, 40]，避免單批過長被截斷、也避免批次過多過慢。
    會隨前端所選模型的 context 自動調整（與決策的 token 預算同源）。
    """
    b = config.LLM_BUDGET
    out_budget = max(1, config.effective_max_model_len() - b.prompt_overhead_tokens)
    per = max(1.0, _PERSONA_CHARS_EST / b.chars_per_token)
    return max(5, min(int(out_budget * 0.6 / per), 40))


def _generate(count: int) -> list[dict]:
    """分批呼叫 LLM 生成共 count 個 persona，robust 解析後合併（失敗回 []）。

    每批 ≤ ``_gen_batch_size()`` 個、各帶不同 seed（42+批次序號 → 多樣又可重現），
    沿用 ``[scaling].concurrency`` 並行；結果**依批次序號順序合併**（與並行與否無關，池可重現）。
    """
    if count <= 0:
        return []
    try:
        from llm_server import json_utils
        from llm_server.agent_profile import run_agent_profile
    except ImportError as e:
        logger.warning("無法匯入 agent_profile 生成器：%s", e)
        return []

    bsize = _gen_batch_size()
    sizes: list[int] = []
    remaining = count
    while remaining > 0:
        take = min(bsize, remaining)
        sizes.append(take)
        remaining -= take

    def gen_one(idx: int, size: int) -> list[dict]:
        try:
            raw = run_agent_profile(output=False, agent_count=size, seed=42 + idx)
        except Exception as e:  # noqa: BLE001  生成可能因 Ollama/vLLM 不可用等失敗
            logger.warning("persona 批次 %d 生成失敗：%s", idx, e)
            return []
        return [o for o in json_utils.salvage_objects(raw) if _is_persona(o)]

    concurrency = max(1, config.SCALING_CONFIG.concurrency)
    results: list[list[dict]] = [[] for _ in sizes]
    if concurrency > 1 and len(sizes) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(concurrency, len(sizes))) as ex:
            fut_to_idx = {ex.submit(gen_one, i, s): i for i, s in enumerate(sizes)}
            for fut, idx in fut_to_idx.items():
                try:
                    results[idx] = fut.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning("persona 批次 %d 取結果失敗：%s", idx, e)
    else:
        for i, s in enumerate(sizes):
            results[i] = gen_one(i, s)

    pool = [p for batch in results for p in batch]
    logger.info("persona 原型生成：目標 %d、實得 %d（%d 批 ×≤%d，concurrency=%d）",
                count, len(pool), len(sizes), bsize, concurrency)
    return pool[:count]


def ensure_pool(n_needed: int, pool_size: int, force: bool = False) -> list[dict]:
    """確保原型池有 ~``pool_size`` 個 persona（**上限／cap**），回傳池 list（生成失敗回 []）。

    ``pool_size`` 是「原型數上限」：生成剛好 pool_size 個原型，模擬車數（``n_needed``）超過時
    由呼叫端以 ``pool[i % len]`` **循環重用**——**不會為了車多而生更多 persona**。
    - force=True 或池為空 → 分批生成 pool_size 個成新池。
    - 池不足 pool_size → 補生差額並追加。否則直接重用。
    （``n_needed`` 僅保留相容、不再決定池大小；池大小一律由 ``pool_size`` 決定。）
    """
    pool = [] if force else load_pool()
    target = max(1, pool_size)
    if not pool:
        pool = _generate(target)
        if pool:
            save_pool(pool)
    elif len(pool) < target:
        pool = pool + _generate(target - len(pool))
        if pool:
            save_pool(pool)
    return pool


def ensure_and_slice(n_needed: int, pool_size: int, force: bool = False) -> str:
    """確保池足夠並回傳「前 n_needed 個 persona」的 JSON 字串（餵 decision prompt 用）。"""
    pool = ensure_pool(n_needed, pool_size, force)
    if not pool:
        return json.dumps({"agents": []}, ensure_ascii=False)  # 生成失敗 → 空，上層 fallback
    chosen = pool[:n_needed] if n_needed <= len(pool) else [pool[i % len(pool)] for i in range(n_needed)]
    return json.dumps({"agents": chosen}, ensure_ascii=False)


def assign_to_agents(agents, pool_size: int,
                     available_towns: list[str] | None = None,
                     default_origin: str = "") -> bool:
    """init 用：依序把 persona 指派給 agent（agent i ← pool[i]），設 profile_name + vehicle_type + 出生點。

    這是「確定性 persona 指派」，不呼叫 LLM 做決策 → 純事件觸發模式下 init 不爆量。
    出生點直接由 persona 的 ``identity.residential_location`` 決定（用 response_parser 正規化成
    available_towns 內的行政區），讓「自帶出生地」**不分事件觸發與否**都生效；無法匹配時保留
    呼叫前既有的 origin_town（通常是 mock 隨機指派的合法區），其次才退到 default_origin。
    回傳是否成功（池可用）；失敗（生成不到，如 Ollama 掛）回 False，由上層 fallback 到 mock。
    """
    pool = ensure_pool(len(agents), pool_size)
    if not pool:
        return False
    from . import response_parser
    towns = list(available_towns or [])
    for i, a in enumerate(agents):
        ident = pool[i % len(pool)].get("identity") or {}
        name = str(ident.get("name") or "").strip()
        a.profile_name = name or a.agent_id
        vt = str(ident.get("vehicle_ownership", ""))
        if "機車" in vt:
            a.vehicle_type = "機車"
        elif "汽車" in vt:
            a.vehicle_type = "汽車"
        # 由 persona 的 residential_location 指定出生點（不分事件觸發與否）。
        res = ident.get("residential_location")
        if towns and res is not None and str(res).strip():
            a.origin_town = response_parser.normalize_town_name(
                res, towns, a.origin_town or default_origin)
    return True


def personas_json(agents) -> str:
    """為「這批」agent 組 decision prompt 用的 agent_profile JSON（依 profile_name 對應，缺則按序）。

    讓每批決策只送該批 agent 的 persona（與其 agents_status 對齊），不送整池、不漏不錯配。
    """
    pool = load_pool()
    by_name = {}
    for p in pool:
        nm = str((p.get("identity") or {}).get("name") or "").strip()
        if nm:
            by_name[nm] = p
    chosen = []
    for i, a in enumerate(agents):
        p = by_name.get(a.profile_name) or (pool[i % len(pool)] if pool else None)
        if p is not None:
            chosen.append(p)
    return json.dumps({"agents": chosen}, ensure_ascii=False)


def clear_pool() -> None:
    """刪掉池檔（前端「重新生成人物」用：刪檔 + 重新初始化即會重生）。"""
    global _POOL_CACHE
    _POOL_CACHE = None   # 清快取，下次 load_pool 會重讀/觸發重生
    path = _pool_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("刪除 persona 池失敗：%s", e)
