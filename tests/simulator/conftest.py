"""pytest 共用設定：把 src/ 加入 import path，提供共用 fixture。"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# 讓測試不必先 pip install -e 也能 import llm_abm_simulator
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llm_abm_simulator.config import DEFAULT_CONFIG  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient():
    """測試一律關閉背景常態車流，讓 engine 生命週期/計數/determinism 測試聚焦事件車（快、可重現）。"""
    from llm_abm_simulator import config
    config.set_runtime_ambient_count(0)
    yield
    config.set_runtime_ambient_count(None)


@pytest.fixture
def small_config():
    """較小、較快的測試情境（少 agent、少步數）。"""
    return dataclasses.replace(DEFAULT_CONFIG, nb_agents=5, max_steps=8, use_llm=False, seed=42)
