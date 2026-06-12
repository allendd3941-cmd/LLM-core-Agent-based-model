"""profile_pool.py — agent persona 池：生成一次、穩定快取、按需切片。

設計（對齊使用者決策）：
- persona 存成穩定的「正規化 JSON 池檔」（output/agent_profile_output_1.txt）。
- 調整 agent 數只是「取池裡前 n 個」，**不重生、不覆寫**。
- agent 數超過池大小（pool_size）才**自動補生**差額並追加進池。
- 要整批換人 → 前端「重新生成人物」按鈕（websocket 刪掉池檔再重新初始化即可）。

robust：池檔與 LLM 生成輸出都用 ``llm_server.json_utils`` 強韌解析（結構壞掉/截斷也救得回），
不會因為一筆壞 JSON 就整池作廢。生成失敗回空池，由上層 fallback。
"""

from __future__ import annotations

import json
import logging

from .. import config

logger = logging.getLogger(__name__)

POOL_FILENAME = "agent_profile_output_1.txt"


def _pool_path():
    return config.OUTPUT_DIR / POOL_FILENAME


def _is_persona(o: object) -> bool:
    return isinstance(o, dict) and "identity" in o


def load_pool() -> list[dict]:
    """讀正規化 persona 池（robust 解析）。檔案不存在/無法解析回 []。"""
    from llm_server import json_utils

    path = _pool_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("讀取 persona 池失敗：%s", e)
        return []
    return [o for o in json_utils.salvage_objects(text) if _is_persona(o)]


def save_pool(pool: list[dict]) -> None:
    path = _pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": pool}, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate(count: int) -> list[dict]:
    """呼叫 LLM 生成 count 個 persona，robust 解析成 list（失敗回 []）。"""
    if count <= 0:
        return []
    try:
        from llm_server import json_utils
        from llm_server.agent_profile import run_agent_profile
    except ImportError as e:
        logger.warning("無法匯入 agent_profile 生成器：%s", e)
        return []
    try:
        raw = run_agent_profile(output=False, agent_count=count)
    except Exception as e:  # noqa: BLE001  生成可能因 Ollama 不可用等失敗
        logger.warning("persona 生成失敗：%s", e)
        return []
    return [o for o in json_utils.salvage_objects(raw) if _is_persona(o)]


def ensure_pool(n_needed: int, pool_size: int, force: bool = False) -> list[dict]:
    """確保池有 ≥ max(pool_size, n_needed) 個 persona，回傳池 list（生成失敗回 []）。

    - force=True 或池為空 → 生成 max(pool_size, n_needed) 個成新池。
    - 池不足 n_needed → 補生差額並追加。否則直接重用。
    """
    pool = [] if force else load_pool()
    target = max(pool_size, n_needed)
    if not pool:
        pool = _generate(target)
        if pool:
            save_pool(pool)
    elif len(pool) < n_needed:
        pool = pool + _generate(n_needed - len(pool))
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
    path = _pool_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("刪除 persona 池失敗：%s", e)
