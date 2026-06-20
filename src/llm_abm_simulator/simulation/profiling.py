"""profiling.py — 每步分段計時（opt-in，由 [scaling].profile_steps 開關）。

設計：
- ``enabled=False`` 時近乎零成本：``phase()`` 直接 yield、``add()``/``count()`` 只是一個分支。
- 粗階段用 context manager ``with prof.phase("move"): ...``（每步呼叫幾次）。
- 熱迴圈（per-agent）用便宜累加器 ``add()/count()``（不要把 context manager 塞進數萬台的迴圈）。
- ``flush(cycle)`` 每步印一行結構化、可解析的計時：
    step 3 prof: decide=98.2s move=412.1s(reroute=405.0s n=21030) flow=2.1s perceive=3.4s detect=1.3s snap=0.8s total=519.7s
- 純 stdlib（time.perf_counter + logging）；要看「哪個函式/哪一行」用 py-spy attach 程序（免改碼）。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# flush 時主階段的固定順序（reroute/respawn 是 move 的子項，以括號附在 move 後）
_MAIN_PHASES = ("decide", "move", "flow", "perceive", "detect", "snap")


class StepProfiler:
    """每步分段計時器。關閉時近乎零成本；不影響任何模擬結果（只量時間、印 log）。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self._times: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    @contextmanager
    def phase(self, name: str):
        if not self.enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._times[name] = self._times.get(name, 0.0) + (time.perf_counter() - t0)

    def add(self, name: str, seconds: float) -> None:
        """熱迴圈累加耗時（如 reroute）；關閉時不做事。"""
        if self.enabled:
            self._times[name] = self._times.get(name, 0.0) + seconds

    def count(self, name: str, n: int = 1) -> None:
        """熱迴圈累加次數（如 reroute_n）；關閉時不做事。"""
        if self.enabled:
            self._counts[name] = self._counts.get(name, 0) + n

    def flush(self, cycle: int) -> None:
        """印出本步分段計時並清空。關閉或無資料時不印。"""
        if not self.enabled or not self._times:
            self._times.clear()
            self._counts.clear()
            return
        total = sum(self._times.get(k, 0.0) for k in _MAIN_PHASES)
        parts: list[str] = []
        for k in _MAIN_PHASES:
            if k not in self._times:
                continue
            seg = f"{k}={self._times[k]:.2f}s"
            if k == "move":
                subs = []
                if "reroute" in self._times:
                    subs.append(f"reroute={self._times['reroute']:.2f}s n={self._counts.get('reroute_n', 0)}")
                if "respawn" in self._times:
                    subs.append(f"respawn={self._times['respawn']:.2f}s n={self._counts.get('respawn_n', 0)}")
                if subs:
                    seg += "(" + " ".join(subs) + ")"
            parts.append(seg)
        logger.info("step %d prof: %s total=%.2fs", cycle, " ".join(parts), total)
        self._times.clear()
        self._counts.clear()
