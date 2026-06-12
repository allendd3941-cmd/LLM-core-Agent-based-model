"""signals.py — runtime 號誌系統：相位計算與紅綠判定。

讀 ``data/tainan_signals.json``（由 ``build_signals.py`` 離線建立），對外提供：
- ``is_signalized(node)``：節點是否為號誌路口。
- ``group(node, bearing)``：進場方位角屬於哪個相位組（0＝ref 路軸 / 1＝垂直路軸）。
- ``is_green(node, bearing, elapsed_s)``：某進場方向在模擬第 ``elapsed_s`` 秒是否為綠燈。
- ``phase_payload()``：給前端的靜態號誌設定（位置 + 相位軸 + offset + 全域 cycle/yellow）。

相位模型（方向相位，一次只放行一個路軸）：
    tc = (elapsed_s + offset) % cycle
    half = cycle / 2
    相位組 0 綠：tc ∈ [0, half - yellow)        其餘為紅/清道
    相位組 1 綠：tc ∈ [half, cycle - yellow)
所以同一路口「一組綠時另一組必為紅」，且 yellow 尾段兩組皆紅（清道），與現實一致。

⚠ 台南無真實時相，cycle/yellow 為 config ``[signals]`` 的合成值（runtime 可調，不需重建 artifact）；
   artifact 只烤入「哪些節點是號誌 + 相位軸 ax + offset」。檔案不存在 → 視為無任何號誌（行為同現狀）。
"""

from __future__ import annotations

import json
import logging

from .. import config

logger = logging.getLogger(__name__)

SIGNALS_JSON = config.DATA_DIR / "tainan_signals.json"

# 進場方位角與 ref 路軸夾角 ≤ 此值（度）→ 歸相位組 0，否則組 1。
_GROUP_HALF_WIDTH = 45.0


def _circ_dist_180(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


class SignalSystem:
    """號誌相位查詢（純函式式，無 per-step 狀態；相位完全由 elapsed_s 決定，故確定性、可重現）。"""

    def __init__(self, signals: dict[str, dict], cycle_s: float, yellow_s: float,
                 enabled: bool = True) -> None:
        self._signals = signals
        self.cycle_s = max(1.0, float(cycle_s))
        self.yellow_s = max(0.0, float(yellow_s))
        self.enabled = enabled and bool(signals)

    # ------------------------------------------------------------------
    def is_signalized(self, node: str) -> bool:
        return self.enabled and node in self._signals

    def group(self, node: str, bearing_deg: float) -> int:
        """進場方位角 → 相位組（0＝ref 路軸，1＝垂直路軸）。非號誌節點回 0。"""
        cfg = self._signals.get(node)
        if cfg is None:
            return 0
        return 0 if _circ_dist_180(bearing_deg % 180.0, cfg["ax"]) <= _GROUP_HALF_WIDTH else 1

    def is_green(self, node: str, bearing_deg: float, elapsed_s: float) -> bool:
        """某進場方向在第 elapsed_s 秒是否綠燈。非號誌節點或關閉 → 永遠 True（可通行）。"""
        if not self.enabled:
            return True
        cfg = self._signals.get(node)
        if cfg is None:
            return True
        if not cfg.get("two", True):
            return True  # 單軸/匝道型號誌：車輛恆綠（主要為行人/示意），不卡車流
        g = 0 if _circ_dist_180(bearing_deg % 180.0, cfg["ax"]) <= _GROUP_HALF_WIDTH else 1
        tc = (elapsed_s + cfg["off"]) % self.cycle_s
        half = self.cycle_s / 2.0
        if g == 0:
            return 0.0 <= tc < (half - self.yellow_s)
        return half <= tc < (self.cycle_s - self.yellow_s)

    # ------------------------------------------------------------------
    def phase_payload(self) -> dict:
        """給前端的靜態號誌設定（一次下發，前端自行用 elapsed 動畫相位）。"""
        return {
            "cycle_s": self.cycle_s,
            "yellow_s": self.yellow_s,
            "group_half_width": _GROUP_HALF_WIDTH,
            "signals": [
                {"id": nid, "lat": c["lat"], "lng": c["lng"],
                 "ax": c["ax"], "off": c["off"], "two": c.get("two", True)}
                for nid, c in self._signals.items()
            ],
        }

    def __len__(self) -> int:
        return len(self._signals)


def load_signal_system(cfg: config.SignalConfig | None = None) -> SignalSystem:
    """載入號誌系統；artifact 不存在或停用 → 回傳「空」系統（is_green 恆 True，行為同現狀）。"""
    cfg = cfg or config.SIGNAL_CONFIG
    if not cfg.enabled:
        return SignalSystem({}, cfg.cycle_s, cfg.yellow_s, enabled=False)
    if not SIGNALS_JSON.exists():
        logger.info("找不到 %s，號誌系統停用（行為同無號誌）。可執行 build_signals 產生。", SIGNALS_JSON)
        return SignalSystem({}, cfg.cycle_s, cfg.yellow_s, enabled=False)
    try:
        data = json.loads(SIGNALS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("號誌 artifact 載入失敗（%s），停用號誌系統", e)
        return SignalSystem({}, cfg.cycle_s, cfg.yellow_s, enabled=False)
    signals = data.get("signals", {})
    logger.info("號誌系統就緒：%d 號誌路口，cycle=%.0fs yellow=%.0fs", len(signals), cfg.cycle_s, cfg.yellow_s)
    return SignalSystem(signals, cfg.cycle_s, cfg.yellow_s, enabled=True)
