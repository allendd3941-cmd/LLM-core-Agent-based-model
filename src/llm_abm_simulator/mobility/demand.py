"""demand.py — 事件需求生成：用「生產約束重力模型」分配 agent 出生地。

把「人是誰（persona 原型）」與「人從哪來（出生地）」**解耦**：出生地不再由 persona 的
residential_location 決定，而是依各區人口 + 對場館的距離衰減（spatial interaction / gravity model）
加權抽樣。換圖層時自動依該圖層的人口分布生成需求，與 persona 原型互不打架。

模型（單一目的地、生產約束）：
    weight_i = population_i × f(d_i)
        f(d) = exp(−beta · d_km)         （decay="exp"）
             = d_km^(−beta)               （decay="power"）
    P_i = weight_i / Σ weight            （各區被抽中的機率）
每個 agent 依 P 抽一個出生區（seeded，可重現）。

設計取向（SIGSPATIAL）：beta 可調＝距離敏感度，前端做成 slider 即可即時展示「催客圈」變化。
無人口資料（population 全 0）或停用 → 回傳 None，由引擎回退既有出生地指派。
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import DemandConfig
    from ..domain.town import Town

logger = logging.getLogger(__name__)


def gravity_weights(towns: list["Town"], venue_xy: tuple[float, float],
                    cfg: "DemandConfig") -> list[tuple["Town", float]] | None:
    """算各區重力權重 [(town, weight)]。無任何正人口 → 回 None（交由上層 fallback）。"""
    vx, vy = venue_xy
    out: list[tuple[Town, float]] = []
    total = 0.0
    for t in towns:
        if t.population <= 0 or t.centroid_metric is None:
            continue
        d_m = math.hypot(t.centroid_metric.x - vx, t.centroid_metric.y - vy)
        d_km = max(d_m / 1000.0, cfg.min_distance_km)
        decay = math.exp(-cfg.beta * d_km) if cfg.decay == "exp" else d_km ** (-cfg.beta)
        w = t.population * decay
        if w > 0:
            out.append((t, w))
            total += w
    if not out or total <= 0:
        return None
    return out


def assign_origin_towns(agents, towns: list["Town"], venue_xy: tuple[float, float],
                        rng, cfg: "DemandConfig") -> bool:
    """依重力模型替每個 agent 設 ``origin_town``（seeded、可重現）。

    回傳是否成功；False＝無人口資料/停用，引擎應保留既有出生地指派。
    """
    if not cfg.enabled:
        return False
    weighted = gravity_weights(towns, venue_xy, cfg)
    if weighted is None:
        logger.warning("無人口資料可用，重力需求生成略過（保留既有出生地）。")
        return False

    names = [t.town_name for t, _ in weighted]
    cum: list[float] = []
    acc = 0.0
    for _, w in weighted:
        acc += w
        cum.append(acc)
    total = cum[-1]

    for a in agents:
        r = rng.random() * total
        # 二分搜尋落點 → 對應區
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if r <= cum[mid]:
                hi = mid
            else:
                lo = mid + 1
        a.origin_town = names[lo]
    logger.info("重力需求生成完成：%d agents 分配到 %d 個有人口的區（beta=%.3f, decay=%s）",
                len(agents), len(names), cfg.beta, cfg.decay)
    return True


def _weighted_index(cum: list[float], total: float, rng) -> int:
    """二分搜尋抽一個落點 → 對應索引（seeded、可重現）。"""
    r = rng.random() * total
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if r <= cum[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def sample_od_pairs(towns: list["Town"], n: int, rng, cfg: "DemandConfig"
                    ) -> list[tuple[str, str]] | None:
    """替**背景常態車流**抽 n 組起訖鄉鎮對（雙邊重力模型，seeded、可重現）。

    與事件車流（單一目的地）不同，背景車是城市裡的常態移動：
        起點 i ∝ population_i                              （trip generation／production）
        終點 j ∝ population_j × f(d_ij)  且 j≠i            （trip distribution，gravity）
            f(d) = exp(−beta·d_km)（decay="exp"）或 d_km^(−beta)（"power"）
    這正是運輸規劃四步驟模型的 generation + distribution。無人口資料 → 回 None（背景流停用）。
    """
    if n <= 0:
        return None
    pops = [t for t in towns if t.population > 0 and t.centroid_metric is not None]
    if len(pops) < 2:
        return None
    names = [t.town_name for t in pops]
    cx = [t.centroid_metric.x for t in pops]
    cy = [t.centroid_metric.y for t in pops]

    # 起點累積（∝ 人口）
    o_cum: list[float] = []
    acc = 0.0
    for t in pops:
        acc += t.population
        o_cum.append(acc)
    o_total = o_cum[-1]

    out: list[tuple[str, str]] = []
    for _ in range(n):
        oi = _weighted_index(o_cum, o_total, rng)
        # 終點累積（∝ 人口 × 對起點的距離衰減；排除同區）
        d_cum: list[float] = []
        acc = 0.0
        for j, t in enumerate(pops):
            if j == oi:
                acc += 0.0
                d_cum.append(acc)
                continue
            d_m = math.hypot(cx[j] - cx[oi], cy[j] - cy[oi])
            d_km = max(d_m / 1000.0, cfg.min_distance_km)
            decay = math.exp(-cfg.beta * d_km) if cfg.decay == "exp" else d_km ** (-cfg.beta)
            acc += t.population * decay
            d_cum.append(acc)
        d_total = d_cum[-1]
        if d_total <= 0:
            di = (oi + 1) % len(pops)
        else:
            di = _weighted_index(d_cum, d_total, rng)
        out.append((names[oi], names[di]))
    return out


def sample_dest_town(towns: list["Town"], origin_xy: tuple[float, float],
                     rng, cfg: "DemandConfig") -> str | None:
    """從 origin_xy 以重力抽一個目的地鄉鎮（∝ 人口 × 距離衰減）。背景車重生用。無資料回 None。"""
    pops = [t for t in towns if t.population > 0 and t.centroid_metric is not None]
    if not pops:
        return None
    ox, oy = origin_xy
    cum: list[float] = []
    acc = 0.0
    for t in pops:
        d_m = math.hypot(t.centroid_metric.x - ox, t.centroid_metric.y - oy)
        d_km = max(d_m / 1000.0, cfg.min_distance_km)
        decay = math.exp(-cfg.beta * d_km) if cfg.decay == "exp" else d_km ** (-cfg.beta)
        acc += t.population * decay
        cum.append(acc)
    if acc <= 0:
        return None
    return pops[_weighted_index(cum, acc, rng)].town_name


def sample_residence(towns: list["Town"], rng) -> str | None:
    """人口加權抽一個居住區（∝ population，與距離無關）。散場目的地（egress）的後備用：
    persona 對不到行政區、或規則式車（無 persona）時,以此給一個合理的居住地。無人口資料回 None。"""
    pops = [t for t in towns if t.population > 0]
    if not pops:
        return None
    cum: list[float] = []
    acc = 0.0
    for t in pops:
        acc += t.population
        cum.append(acc)
    if acc <= 0:
        return None
    return pops[_weighted_index(cum, acc, rng)].town_name


def expected_distribution(towns: list["Town"], venue_xy: tuple[float, float],
                          cfg: "DemandConfig", top_k: int = 10) -> list[tuple[str, float]]:
    """回傳各區的期望來客占比（給前端/分析顯示），降冪取 top_k。無資料回 []。"""
    weighted = gravity_weights(towns, venue_xy, cfg)
    if weighted is None:
        return []
    total = sum(w for _, w in weighted)
    rows = sorted(((t.town_name, w / total) for t, w in weighted), key=lambda x: x[1], reverse=True)
    return rows[:top_k]
