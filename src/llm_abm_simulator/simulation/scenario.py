"""scenario.py — 單次模擬情境設定。

把使用者/前端可調整的少數選項從完整 SimulationConfig 中分出來，
方便 web 層與測試以簡單參數建立情境，再展開成 SimulationConfig。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..config import DEFAULT_CONFIG, SimulationConfig


@dataclass
class Scenario:
    """一次 demo 執行的可調整選項。"""

    nb_agents: int = 10
    use_llm: bool = False
    seed: int = 42

    def to_config(self, base: SimulationConfig | None = None) -> SimulationConfig:
        """展開成完整 SimulationConfig（以 DEFAULT_CONFIG 為底覆寫）。"""
        base = base or DEFAULT_CONFIG
        return dataclasses.replace(
            base,
            nb_agents=self.nb_agents,
            use_llm=self.use_llm,
            seed=self.seed,
        )
