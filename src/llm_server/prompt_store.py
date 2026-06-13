"""prompt_store.py — runtime 可覆寫的 prompt 管理（demo 時前端可改 prompt 做不同模擬）。

各 LLM 模組（agent_profile / decision_making）在 import 時把預設 prompt 註冊進來，呼叫時
用 ``get(name)`` 讀（有覆寫用覆寫、否則用預設）。前端可即時 set_override / 還原預設。

安全：結構化輸出（decision 的 format schema）仍會強制輸出形狀，故使用者把 prompt 改壞時
仍吐合法 JSON、不會整個崩。覆寫為 process 全域（單人 demo）。
"""

from __future__ import annotations

_DEFAULTS: dict[str, str] = {}
_OVERRIDES: dict[str, str] = {}
_LABELS: dict[str, str] = {"agent_profile": "人物生成 Prompt", "decision_making": "決策 Prompt"}


def register_default(name: str, text: str) -> None:
    _DEFAULTS.setdefault(name, text)


def get(name: str) -> str:
    return _OVERRIDES.get(name) or _DEFAULTS.get(name, "")


def set_override(name: str, text: str | None) -> None:
    """設覆寫；text 空字串/None → 還原預設。"""
    if text and text.strip():
        _OVERRIDES[name] = text
    else:
        _OVERRIDES.pop(name, None)


def snapshot() -> dict[str, dict]:
    """給前端：每個 prompt 的 label / 預設 / 目前值 / 是否被覆寫。"""
    return {
        name: {
            "label": _LABELS.get(name, name),
            "default": default,
            "current": get(name),
            "overridden": name in _OVERRIDES,
        }
        for name, default in _DEFAULTS.items()
    }
