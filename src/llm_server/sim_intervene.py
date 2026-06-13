"""sim_intervene.py — 把使用者的自然語言指令對應到「受限動作集」（NL 介入）。

受限動作（只有這些，安全）：
- avoid_area(town)：避開某行政區一帶（車輛重新規劃路線繞開）。
- demand_surge(town, count)：從某區突然湧入 count 台車。
- none：無法對應（不做任何事）。

用所選 LLM + 結構化輸出 schema 解析；LLM 不可用時退回關鍵字解析（仍可動）。
"""

from __future__ import annotations

import re

from . import llm_client

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["avoid_area", "demand_surge", "none"]},
        "town": {"type": "string"},
        "count": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["action"],
}

_SYSTEM = (
    "你是交通模擬的『介入指令解析器』。把使用者的中文指令對應到受限動作集之一："
    "avoid_area（避開某區）、demand_surge（某區湧入 count 台車）、none（無法對應）。"
    "town 必須是提供的『可用行政區』之一；count 為整數。只輸出 JSON。"
)


def _keyword_parse(text: str, towns: list[str]) -> dict:
    """LLM 不可用時的後備：關鍵字 + 區名比對。"""
    town = next((t for t in sorted(towns, key=len, reverse=True) if t and t in text), "")
    num = re.search(r"(\d+)", text)
    count = int(num.group(1)) if num else 0
    if any(k in text for k in ("避", "封", "繞", "別走", "不要走")):
        return {"action": "avoid_area", "town": town, "count": 0}
    if any(k in text for k in ("湧入", "增加", "多", "湧進", "新增")) and (count or town):
        return {"action": "demand_surge", "town": town, "count": count or 100}
    return {"action": "none", "town": town, "count": count}


def run_intervene(text: str, available_towns: list[str]) -> dict:
    """回傳 {action, town, count}。**關鍵字優先**（確定性、對受限指令最可靠），
    關鍵字判不出來（none）才用 LLM 解析模糊措辭。並把 town 正規化到可用區。"""
    parsed = _keyword_parse(text, available_towns)
    if parsed.get("action") == "none":
        try:
            from . import json_utils
            prompt = (f"可用行政區：{available_towns}\n使用者指令：{text}\n"
                      "請輸出對應動作 JSON（action/town/count）。")
            raw = llm_client.generate(prompt, system=_SYSTEM, options={"seed": 42},
                                      think="low", fmt=_SCHEMA, label="intervene")
            llm_parsed = json_utils.loads_lenient(raw)
            if isinstance(llm_parsed, dict) and llm_parsed.get("action") in ("avoid_area", "demand_surge"):
                parsed = llm_parsed
        except Exception:  # noqa: BLE001
            pass

    # town 正規化到可用區（含部分比對）
    town = str(parsed.get("town", "") or "")
    if town and town not in available_towns:
        town = next((t for t in sorted(available_towns, key=len, reverse=True) if t and t in town), "")
    return {"action": parsed.get("action", "none"), "town": town,
            "count": int(parsed.get("count", 0) or 0)}
