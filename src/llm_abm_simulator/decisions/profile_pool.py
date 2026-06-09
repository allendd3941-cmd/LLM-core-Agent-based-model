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


def ensure_and_slice(n_needed: int, pool_size: int, force: bool = False) -> str:
    """確保池足夠並回傳「前 n_needed 個 persona」的 JSON 字串（餵 decision prompt 用）。

    - force=True 或池為空 → 生成 max(pool_size, n_needed) 個成新池。
    - 池不足 n_needed → 補生差額並追加。
    - 否則直接重用。
    n_needed 超過池大小時（生成也可能不足）以循環重用補滿，確保切片長度 = n_needed。
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

    if not pool:
        return json.dumps({"agents": []}, ensure_ascii=False)  # 生成失敗 → 空，上層 fallback

    if n_needed <= len(pool):
        chosen = pool[:n_needed]
    else:  # 仍不足（生成失敗或 pool_size 太小）→ 循環重用
        chosen = [pool[i % len(pool)] for i in range(n_needed)]
    return json.dumps({"agents": chosen}, ensure_ascii=False)


def clear_pool() -> None:
    """刪掉池檔（前端「重新生成人物」用：刪檔 + 重新初始化即會重生）。"""
    path = _pool_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("刪除 persona 池失敗：%s", e)
