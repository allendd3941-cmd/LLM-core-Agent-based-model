"""rag_query.py — RAG 多重查詢的「domain 查詢建構器」。

把每批決策的模擬狀態（環境感知文字 + 這批 persona）拆成數條聚焦的子查詢，
交給通用檢索引擎 ``rag_store.retrieve_multi`` 各自檢索、RRF 融合。

職責分界（重要）：
- ``rag_store`` 是**通用檢索引擎**，不認識交通/persona。
- 本檔負責**領域知識** → 把狀態翻成查詢字串；rag_store 只負責「拿字串去搜」。

三條子查詢：
- 路況：取【全域路況】（descriptive，當前壅塞情勢）。
- 任務：固定描述「在壅塞下選交通方式/車種的行為傾向」，含五種 active_mode（英文 key + 中文）。
- 人格：聚合這批 persona 的職業/車種/特質，取高頻拼成短句。

註：任務子查詢的英文 key（fast…）對中文知識庫檢索是**惰性**的（char n-gram 撞不到中文文件），
保留是為了與 DECISION_SCHEMA 一致、人類可讀；真正做比對的是中文描述，故中文寫得豐富些。
"""

from __future__ import annotations

import json
from collections import Counter

from . import perception

# 任務子查詢（幾乎固定）：對齊 decision_making 的五種 active_mode enum。
Q_TASK = (
    "在壅塞與時間壓力下，選擇交通方式與車種的行為傾向。交通方式有五種："
    "fast（想要快一點、爭取時間）、"
    "tolerate_congestion（繼續塞車也沒關係、不改道）、"
    "avoid_congestion（避開壅塞、改道繞行）、"
    "comfortable（穩定舒適、走主要幹道）、"
    "short_distance（想走短一點、抄近路）"
)

_TRAIT_KEYS = ("attitudes", "habits", "decision_making_tendencies",
               "economic_preferences_and_tradeoffs")


def _parse_agents(profile_json: str) -> list[dict]:
    """把 agent_profile_data（``{"agents":[...]}`` JSON 字串）解析成 persona list。

    正常路徑是合法 JSON（profile_pool 以 json.dumps 產生）；壞掉/截斷時用 json_utils
    強韌救援；都失敗回 []（降級，不 crash）。
    """
    if not profile_json:
        return []
    try:
        obj = json.loads(profile_json)
        if isinstance(obj, dict) and isinstance(obj.get("agents"), list):
            return [a for a in obj["agents"] if isinstance(a, dict)]
    except (ValueError, TypeError):
        pass
    try:  # 保底：救援殘缺 JSON（manual 測試路徑可能餵進原始 LLM 文字）
        from . import json_utils
        return [o for o in json_utils.salvage_objects(profile_json)
                if isinstance(o, dict) and "identity" in o]
    except Exception:  # noqa: BLE001  救援本身失敗也只降級
        return []


def q_situation(perception_text: str) -> str:
    """路況子查詢：當前全域壅塞情勢。"""
    return perception.global_situation_text(perception_text)


def q_task() -> str:
    """任務子查詢：決策目標（固定）。"""
    return Q_TASK


def q_persona(profile_json: str, top: int = 4) -> str:
    """人格子查詢：聚合這批 persona 的高頻職業/車種/特質，拼成短句；無 persona 回 ''。"""
    agents = _parse_agents(profile_json)
    if not agents:
        return ""
    occ: Counter = Counter()
    veh: Counter = Counter()
    traits: Counter = Counter()
    for a in agents:
        idn = a.get("identity") or {}
        if idn.get("occupation"):
            occ[str(idn["occupation"]).strip()] += 1
        if idn.get("vehicle_ownership"):
            veh[str(idn["vehicle_ownership"]).strip()] += 1
        tr = a.get("traits") or {}
        for key in _TRAIT_KEYS:
            for item in (tr.get(key) or []):
                if item:
                    traits[str(item).strip()] += 1

    parts: list[str] = []
    g_occ = "、".join(w for w, _ in occ.most_common(top) if w)
    if g_occ:
        parts.append(g_occ)
    g_veh = "、".join(w for w, _ in veh.most_common(2) if w)
    if g_veh:
        parts.append(f"{g_veh}為主")
    g_tr = "、".join(w for w, _ in traits.most_common(top) if w)
    if g_tr:
        parts.append(g_tr)
    return "；".join(parts)


def build_subqueries(perception_text: str, profile_json: str) -> dict[str, str]:
    """組三條子查詢 {標籤: 查詢字串}；空的子查詢直接略過（不貢獻檢索）。"""
    subs: dict[str, str] = {}
    s = q_situation(perception_text)
    if s:
        subs["路況"] = s
    subs["任務"] = q_task()
    p = q_persona(profile_json)
    if p:
        subs["人格"] = p
    return subs


_HYDE_SYS = "你是交通疏運與管制領域的助理，只輸出一段建議文字。"


def hyde_expand(query: str, generate) -> str:
    """HyDE：把查詢改寫成「假想的權威文件片段」再去檢索，橋接 descriptive↔prescriptive 落差。

    ``generate`` 為可呼叫物件（通常傳 ``llm_client.generate``）。生成失敗/空 → 回原 query（降級，不影響檢索）。
    僅在 rag_store.hyde_active() 為真時被呼叫（長文件才划算）。
    """
    if not query:
        return query
    prompt = (
        "針對以下交通情境，寫一段 2~3 句、像『交通管制計畫／疏運手冊』會出現的具體建議文字"
        "（陳述句、不要列點、不要前言）：\n" + query
    )
    try:
        out = generate(prompt, system=_HYDE_SYS, think="low", label="hyde")
        out = (out or "").strip()
        return out or query
    except Exception:  # noqa: BLE001  HyDE 失敗一律降級回原 query
        return query
