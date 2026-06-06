"""scheduler.py — cycle 時間推進與停止條件。

對齊 GAML：每個 cycle 代表 step_minutes 分鐘，跑滿 max_steps 後停止
（GAML stop_at_max_steps）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Scheduler:
    """模擬時鐘。"""

    max_steps: int
    step_minutes: int
    cycle: int = 0

    def advance(self) -> int:
        """推進一個 cycle，回傳新的 cycle 編號。"""
        self.cycle += 1
        return self.cycle

    @property
    def elapsed_minutes(self) -> int:
        return self.cycle * self.step_minutes

    @property
    def finished(self) -> bool:
        return self.cycle >= self.max_steps

    def reset(self) -> None:
        self.cycle = 0
