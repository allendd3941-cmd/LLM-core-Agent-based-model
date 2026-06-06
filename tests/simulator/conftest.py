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


@pytest.fixture
def small_config():
    """較小、較快的測試情境（少 agent、少步數）。"""
    return dataclasses.replace(DEFAULT_CONFIG, nb_agents=5, max_steps=8, use_llm=False, seed=42)
