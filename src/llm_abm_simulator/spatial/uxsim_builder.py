"""uxsim_builder.py — 從本專案的 RoadNetwork（OSM graphml）建出 UXsim World。

把 networkx 路網逐節點 / 逐邊轉成 UXsim 的 Node / Link：
- 節點用 EPSG:3826 公尺座標（``addNode(x, y)``），與既有距離 / 幾何一致。
- 邊用 ``road_id``（= f"{u}_{v}"，每有向邊唯一）當 link 名，帶 length / free_flow_speed(m/s) /
  number_of_lanes / jam_density；供 UXsim 的 kinematic-wave 物理 + 內建 DUO 路由使用。

只負責「供給側網路」轉換；需求（車輛）與行為（LLM）由 engine 在其上驅動（見 UXsim 遷移計畫）。
link 名 = road_id，使 engine 的偵測器 / 介入（set_links_avoid 等）能用同一識別子對應。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .road_network import RoadNetwork

logger = logging.getLogger(__name__)

# veh/m/lane（UXsim 預設值；Phase 5/6 會用相機資料校準）。
DEFAULT_JAM_DENSITY = 0.2


def crop_to_region(network: "RoadNetwork", center_latlng: tuple[float, float],
                   radius_km: float) -> "RoadNetwork":
    """把路網裁切到「以 center 為圓心、radius_km 內」的最大強連通子圖，回傳新的 RoadNetwork。

    UXsim 內建 route choice 用全對最短路（記憶體 ∝ 節點²）；完整 OSM 台南 15,833 節點 ≈ 9 GiB。
    demo 聚焦球場事件、驗證相機在 5km 內 → 裁切到球場區域（建議 6–8km，含相機+路由緩衝）
    使節點降到 ~1.5k–4.8k、記憶體 ~0.1–1 GiB、route 更新也快很多。``radius_km<=0`` 表不裁切。
    """
    from .. import config
    from . import road_network as rn

    if radius_km <= 0:
        return network
    from pyproj import Transformer
    to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
    cx, cy = to_m.transform(center_latlng[1], center_latlng[0])
    r2 = (radius_km * 1000.0) ** 2
    g = network.graph
    keep = [n for n in g.nodes()
            if (lambda xy: (xy[0] - cx) ** 2 + (xy[1] - cy) ** 2 <= r2)(network.node_xy(n))]
    sub = rn._largest_scc(g.subgraph(keep).copy())
    logger.info("路網裁切 ≤%.1fkm：%d→%d 節點 / %d 邊（最大強連通）",
                radius_km, g.number_of_nodes(), sub.number_of_nodes(), sub.number_of_edges())
    return rn._wrap(sub)


def build_world(
    network: "RoadNetwork",
    *,
    tmax: int,
    deltan: int = 1,
    seed: int = 0,
    jam_density: float = DEFAULT_JAM_DENSITY,
    signals: Any = None,
    reaction_time: float = 1.0,
    duo_update_time: float = 600.0,
    duo_update_weight: float = 0.5,
    duo_noise: float = 0.01,
    route_choice_principle: str = "homogeneous_DUO",
    route_choice_update_gradual: bool = False,
    instantaneous_TT_timestep_interval: int = 5,
    no_cyclic_routing: bool = False,
    hard_deterministic_mode: bool = True,
    print_mode: int = 0,
) -> Any:
    """從 ``RoadNetwork`` 建一個已載入網路的 UXsim ``World``（不含車輛）。

    Args:
        tmax: 模擬總時長（秒）。
        deltan: 平台聚合單位（1 = 每車獨立，個體 LLM agent 必需；成本 ∝ 1/deltan²）。
        seed: 隨機種子（搭配 hard_deterministic_mode 保可重現）。
        jam_density: 每車道堵塞密度 veh/m。
        signals: 可選的 ``SignalSystem``；雙相位號誌節點→``addNode(signal=[半,半], signal_offset=off)``、
            其入口邊→``addLink(signal_group=該方向相位組)``。映射合成方向相位（ax 軸±45°=組0、垂直=組1）。
    """
    import math

    from uxsim import World

    W = World(
        name="tainan", deltan=deltan, tmax=tmax, reaction_time=reaction_time,
        duo_update_time=duo_update_time, duo_update_weight=duo_update_weight, duo_noise=duo_noise,
        route_choice_principle=route_choice_principle,
        route_choice_update_gradual=route_choice_update_gradual,
        instantaneous_TT_timestep_interval=instantaneous_TT_timestep_interval,
        no_cyclic_routing=no_cyclic_routing,
        random_seed=seed, hard_deterministic_mode=hard_deterministic_mode,
        print_mode=print_mode, save_mode=0, show_mode=0,
    )

    sig = signals if (signals is not None and getattr(signals, "enabled", False)) else None
    half = int(sig.cycle_s / 2) if sig else 0

    def _two_phase(node: str) -> bool:
        return bool(sig) and half > 0 and sig.is_signalized(node) \
            and sig._signals.get(node, {}).get("two", True)

    g = network.graph
    n_sig = 0
    for node_id in g.nodes():
        x_m, y_m = network.node_xy(node_id)
        if _two_phase(node_id):
            off = int(sig._signals[node_id].get("off", 0))
            W.addNode(str(node_id), float(x_m), float(y_m), signal=[half, half], signal_offset=off)
            n_sig += 1
        else:
            W.addNode(str(node_id), float(x_m), float(y_m))

    n_links = 0
    for (u, v), road in network.roads.items():
        sg = 0
        if _two_phase(v):   # 入口邊的相位組由「進場方位角 vs 該號誌 ax 軸」決定
            ux, uy = network.node_xy(u)
            vx, vy = network.node_xy(v)
            bearing = math.degrees(math.atan2(vy - uy, vx - ux)) % 360.0
            sg = sig.group(v, bearing)
        W.addLink(
            road.road_id, str(u), str(v),
            length=max(float(road.length), 1.0),
            free_flow_speed=max(float(road.speed_car) / 3.6, 1.0),   # km/h → m/s
            number_of_lanes=max(1, int(round(float(road.lanes)))),
            jam_density=jam_density, signal_group=sg,
        )
        n_links += 1

    logger.info("UXsim World 建置完成：%d 節點（%d 號誌）/ %d link（deltan=%d, tmax=%d）",
                g.number_of_nodes(), n_sig, n_links, deltan, tmax)
    return W
