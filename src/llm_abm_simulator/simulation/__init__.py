"""simulation — 模擬引擎與相關元件。

- ``random_seed`` 統一亂數來源（同 seed → 同軌跡）。
- ``scheduler``   cycle / elapsed 時間推進與停止條件。
- ``metrics``     壅塞與分佈指標 + CSV 輸出（取代 GAMA CSV）。
- ``engine``      生命週期：initialize / step / run / pause / resume / reset。
"""

from __future__ import annotations

from .engine import SimulationEngine

__all__ = ["SimulationEngine"]
