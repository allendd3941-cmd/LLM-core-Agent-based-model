"""response_parser.py — robust 解析 LLM/decision 回應。

完整鏡像 GAML 的解析容錯邏輯（extract_origin_from_body / extract_action_mode_from_body /
extract_vehicle_type_from_body / apply_*_response），並對齊既有 LLM pipeline 的實際輸出
（見 output/decision_making_output_1.txt 與 prompts/decision_making_prompt.txt）：

    {"agents": [{"agent name": "...", "action mode": "fast",
                 "residential_location": "東區", "vehicle_type": "機車"}]}

注意：既有 simulation_web/backend/llm_bridge.py 找的是 ``action_mode`` / ``agent_id``，
與真實輸出的 ``"action mode"`` / ``"agent name"`` 不符，因此解析不到 — 本檔修正此問題，
支援含空格與底線兩種 key、中英文 key，以及非純 JSON（含 markdown 圍欄/前後雜訊）的回應。
"""

from __future__ import annotations

import logging
from typing import Any

from llm_server import json_utils

logger = logging.getLogger(__name__)

# 各欄位的 key 別名（對齊 GAML reply.keys contains 一連串判斷）
_ORIGIN_KEYS = ("origin", "residential_location", "origin_town", "origin_taz", "出發點", "起點")
_MODE_KEYS = ("action_mode", "action mode", "mode", "type")
_VEHICLE_KEYS = ("vehicle_type", "vehicle type", "車種", "vehicle_ownership")
_REASON_KEYS = ("reason", "理由", "原因", "why")
_NAME_KEYS = ("agent name", "agent_name", "name", "profile_name")
_ID_KEYS = ("agent_id", "agent name", "agent_name", "name")
_ROW_LIST_KEYS = ("agents", "decisions", "initial_vehicles", "requested_agents")


# ---------------------------------------------------------------------------
# JSON 抽取（容忍非純 JSON 的 LLM 文字）
# ---------------------------------------------------------------------------
def coerce_json(body: Any) -> Any:
    """把回應 body 轉成 Python 物件。

    強韌解析（委派 llm_server.json_utils）：支援 dict/list、純 JSON、```json 圍欄、前後雜訊、
    尾逗號/Python 字面量等語法雜訊；**陣列被截斷時，逐一救回已完整的物件成 list**，
    而不是整包放棄。無法解析時回傳原字串（由 parse_rows 視為無 row）。
    """
    if isinstance(body, (dict, list)):
        return body
    if not isinstance(body, str):
        return body

    parsed = json_utils.loads_lenient(body)
    if parsed is not None:
        return parsed
    # 結構壞掉/截斷 → 逐物件搶救成 list（讓 parse_rows 仍能拿到前面完整的 agent）
    salvaged = json_utils.salvage_objects(body)
    if salvaged:
        return salvaged
    return body


# ---------------------------------------------------------------------------
# 欄位正規化（鏡像 GAML normalize_*）
# ---------------------------------------------------------------------------
def normalize_town_name(raw: Any, available_towns: list[str], default: str) -> str:
    """掃描字串中是否含任一可用行政區名，命中即回傳；否則回 default。

    依名稱長度由長到短比對，避免「安南區」被較短的「南區」搶先命中
    （GAML 原版以宣告順序比對會有此歧義，這裡修正之）。
    """
    cleaned = str(raw) if raw is not None else ""
    for town in sorted((t for t in available_towns if t), key=len, reverse=True):
        if town in cleaned:
            return town
    return default


def normalize_vehicle_type(raw: Any, default: str = "汽車") -> str:
    cleaned = str(raw) if raw is not None else ""
    if "機車" in cleaned:
        return "機車"
    if "汽車" in cleaned:
        return "汽車"
    return "機車" if "機車" in default else "汽車"


def _first_key(row: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in row and str(row[k]).strip() != "":
            return row[k]
    return None


# ---------------------------------------------------------------------------
# 單列解析
# ---------------------------------------------------------------------------
def parse_row(row: dict, available_towns: list[str], default_origin: str) -> dict[str, Any]:
    """從單一 agent row 解析出 id/name/origin/action_mode/vehicle_type。"""
    name = _first_key(row, _NAME_KEYS)
    agent_id = _first_key(row, _ID_KEYS)

    origin_raw = _first_key(row, _ORIGIN_KEYS)
    origin = normalize_town_name(origin_raw, available_towns, default_origin) if origin_raw is not None else ""

    mode_raw = _first_key(row, _MODE_KEYS)
    action_mode = ""
    if isinstance(mode_raw, dict):
        action_mode = str(mode_raw.get("mode_name") or mode_raw.get("mode") or "")
    elif mode_raw is not None:
        action_mode = str(mode_raw)

    vt_raw = _first_key(row, _VEHICLE_KEYS)
    vehicle_type = normalize_vehicle_type(vt_raw) if vt_raw is not None else ""

    reason_raw = _first_key(row, _REASON_KEYS)
    reason = str(reason_raw).strip() if reason_raw is not None else ""

    return {
        "agent_id": str(agent_id) if agent_id is not None else "",
        "profile_name": str(name) if name is not None else "",
        "origin_town": origin,
        "action_mode": action_mode,
        "vehicle_type": vehicle_type,
        "reason": reason,
    }


def parse_rows(body: Any, available_towns: list[str], default_origin: str) -> list[dict[str, Any]]:
    """解析整個回應，回傳 agent row 解析結果清單。

    支援：
    - dict 內含 agents/decisions/initial_vehicles/requested_agents list。
    - 直接是 list of rows。
    - 單一 dict（視為一列）。
    - 非純 JSON 字串（coerce_json 處理）。
    """
    parsed = coerce_json(body)
    rows: list[dict] = []

    if isinstance(parsed, dict):
        for key in _ROW_LIST_KEYS:
            if key in parsed and isinstance(parsed[key], list):
                rows = [r for r in parsed[key] if isinstance(r, dict)]
                break
        if not rows and any(k in parsed for k in (_ORIGIN_KEYS + _MODE_KEYS)):
            rows = [parsed]
    elif isinstance(parsed, list):
        rows = [r for r in parsed if isinstance(r, dict)]

    return [parse_row(r, available_towns, default_origin) for r in rows]
