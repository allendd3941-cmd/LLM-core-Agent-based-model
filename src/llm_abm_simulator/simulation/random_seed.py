"""random_seed.py — 統一亂數來源，保證可重現。

對齊計畫的「deterministic seed」需求：所有需要隨機的地方（agent 起點挑選、
mock profile 生成）都使用這裡建立的 ``random.Random`` 實例，
同一個 seed 兩次執行必產生完全相同的軌跡。
"""

from __future__ import annotations

import random


def make_rng(seed: int) -> random.Random:
    """建立一個獨立、可重現的亂數產生器。"""
    return random.Random(seed)
