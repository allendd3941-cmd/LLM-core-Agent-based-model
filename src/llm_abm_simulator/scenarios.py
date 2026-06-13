"""scenarios.py — 場景（圖層）抽象與登錄表：可抽換的模擬場景。

「場景」是把『模擬需要的所有空間輸入』抽象成一份合約，引擎不再寫死台南：
  - county_filter：行政區界要保留哪個縣市（TOWN_MOI 為全台，用此篩選）
  - road_graphml：路網（OSM graphml）
  - population_csv：各區人口（重力需求生成用）
  - signals_json：號誌 artifact（可無）
  - dest_lat/lng + dest_town：事件目的地（None → 用內建球場 shapefile，保留預設台南行為）
  - center/zoom：前端地圖初始視角

換場景＝換上面這份合約。預設＝台南亞太棒球場（與原行為完全一致）。
新城市/尺度由 `spatial/build_scenario.py` 產生 bundle（county + 目的地 + OSM 路網）。
單人 demo：active 場景為 process 全域（前端下拉切換 → 重新初始化引擎）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

SCENARIOS_DIR = config.DATA_DIR / "scenarios"   # builder 產生的場景 manifest（*.json）放這


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    county_filter: str = "臺南|台南"
    road_graphml: Path = config.ROAD_GRAPHML
    population_csv: Path = config.TOWN_POPULATION_CSV
    signals_json: Path = config.DATA_DIR / "tainan_signals.json"
    dest_lat: float | None = None        # None → 用 STADIUM_SHP（預設台南場景）
    dest_lng: float | None = None
    dest_town: str = "安定區"
    center_lat: float = 23.06
    center_lng: float = 120.23
    zoom: int = 12

    def to_summary(self) -> dict:
        return {"key": self.key, "name": self.name}


_REGISTRY: dict[str, Scenario] = {}
_ACTIVE_KEY: str = "tainan_stadium"


def register(s: Scenario) -> None:
    _REGISTRY[s.key] = s


def active() -> Scenario:
    return _REGISTRY[_ACTIVE_KEY]


def set_active(key: str) -> bool:
    global _ACTIVE_KEY
    if key in _REGISTRY:
        _ACTIVE_KEY = key
        logger.info("切換場景 → %s（%s）", key, _REGISTRY[key].name)
        return True
    return False


def all_summaries() -> list[dict]:
    return [s.to_summary() for s in _REGISTRY.values()]


def register_manifest(d: dict) -> Scenario:
    """由 manifest dict 建立並註冊一個 Scenario（builder 產生 / UI 上傳共用）。"""
    s = Scenario(
        key=d["key"], name=d.get("name", d["key"]),
        county_filter=d.get("county_filter", "臺南|台南"),
        road_graphml=Path(d["road_graphml"]),
        population_csv=Path(d.get("population_csv", config.TOWN_POPULATION_CSV)),
        signals_json=Path(d["signals_json"]) if d.get("signals_json") else config.DATA_DIR / "_none_.json",
        dest_lat=d.get("dest_lat"), dest_lng=d.get("dest_lng"),
        dest_town=d.get("dest_town", ""),
        center_lat=d.get("center_lat", 23.06), center_lng=d.get("center_lng", 120.23),
        zoom=d.get("zoom", 12),
    )
    register(s)
    return s


def _load_manifests() -> None:
    """讀 data/scenarios/*.json（builder / UI 上傳產生）並註冊。"""
    if not SCENARIOS_DIR.exists():
        return
    for p in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            register_manifest(json.loads(p.read_text(encoding="utf-8")))
            logger.info("載入場景 manifest：%s", p.stem)
        except (OSError, KeyError, json.JSONDecodeError) as e:
            logger.warning("場景 manifest 解析失敗 %s：%s", p, e)


# ---- 內建場景 ----
# 預設：台南亞太棒球場（dest_lat/lng=None → 用既有 STADIUM_SHP，行為與原本完全一致）
register(Scenario(key="tainan_stadium", name="台南亞太棒球場（預設）", dest_town="安定區"))
# 示範：同城換事件地點（台南火車站，在路網覆蓋內、與球場不同方位 → 不同 OD/壅塞）
register(Scenario(key="tainan_station", name="台南火車站（示範事件）",
                  dest_lat=22.9971, dest_lng=120.2128, dest_town="東區",
                  center_lat=23.00, center_lng=120.21, zoom=13))
_load_manifests()
