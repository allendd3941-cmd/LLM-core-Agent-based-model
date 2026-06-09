"""json_utils.py — 強韌解析 LLM 產出的 JSON。

LLM（尤其小模型）常吐出「結構壞掉」的 JSON：尾逗號、被截斷的陣列、Python 字面量
（True/False/None）、智慧引號、前後夾雜說明文字、```json 圍欄等。

本模組目標：**盡量救回有用內容，而不是一失敗就回預設值**。最關鍵的是
``salvage_objects``：即使整體 JSON 壞掉或被截斷，仍會把「已完整的物件」一個個掃出來
（例如 agent 清單只生成到一半，仍救回前面完整的那幾個 agent）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 常見「物件清單」的包裝鍵（agent profile / decision / summary…）
_LIST_KEYS = ("agents", "decisions", "summaries", "initial_vehicles",
              "requested_agents", "items", "data", "results")


def _strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 圍欄；沒有就回原字串（去頭尾空白）。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def repair(text: str) -> str:
    """修補常見的非法 JSON 寫法（不改動字串內容語意，僅修語法雜訊）。"""
    # 智慧引號 → 直引號
    text = (text.replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'"))
    # Python/JS 字面量 → JSON（用 word boundary 避免改到字串內的字）
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\b(?:None|NULL|Null)\b", "null", text)
    # 移除 } 或 ] 前的尾逗號（含換行）
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def loads_lenient(text: Any) -> Any | None:
    """盡力把 LLM 輸出解析成 Python 物件；無法解析回 None（不丟例外）。"""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    t = _strip_fences(text)
    for candidate in (t, repair(t)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _balanced_object(text: str, start: int) -> tuple[str | None, int]:
    """從 text[start]（應為 '{'）取出一個括號平衡的物件字串。

    回傳 (block, end_index)；若到字尾仍未平衡（被截斷）回 (None, len(text))。
    正確處理字串內的引號與跳脫，故不會被字串裡的 { } 騙到。
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
    return None, len(text)


def salvage_objects(text: Any) -> list[dict]:
    """從（可能壞掉/被截斷的）LLM 輸出救回一串 JSON 物件。

    策略：
    1. 先試完整解析；若是物件清單（list 或含 agents/summaries… 鍵）→ 直接回。
    2. 若解析失敗（最常見：陣列被截斷）→ 掃描文字，逐一抽出「頂層平衡的 {...}」並各自
       寬鬆解析，能解析的就收下。被截斷的最後一個不完整物件會被自動丟掉，前面完整的全保留。
    """
    obj = loads_lenient(text)
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    if isinstance(obj, dict):
        for k in _LIST_KEYS:
            if isinstance(obj.get(k), list):
                return [o for o in obj[k] if isinstance(o, dict)]
        return [obj]

    # 完整解析失敗 → 逐物件搶救
    if not isinstance(text, str):
        return []
    t = _strip_fences(text)
    # 從第一個 '[' 之後開始掃（跳過外層包裝的 '{' 與 "agents":），沒有陣列就從頭掃
    arr = t.find("[")
    i = arr + 1 if arr >= 0 else 0
    out: list[dict] = []
    n = len(t)
    while i < n:
        b = t.find("{", i)
        if b < 0:
            break
        block, end = _balanced_object(t, b)
        if block is None:        # 截斷的尾巴，停止
            break
        parsed = loads_lenient(block)
        if isinstance(parsed, dict):
            out.append(parsed)
        i = end
    return out
