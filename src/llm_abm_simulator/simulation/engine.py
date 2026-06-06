"""engine.py — 交通 ABM 模擬引擎（取代 GAMA 模擬主迴圈）。

擁有完整模擬狀態，對齊 GAML 主模型的 init 與每 cycle reflex 行為：

    每 step：套用決策 → 感知（速限/壅塞/鄰近）→ 移動（壅塞時重算路徑）→
             重算道路 flow/congestion/weight → 指標/分佈 → 記錄 memory + CSV → 快照。

決策來源透過 DecisionPolicy 抽象（mock 預設，可切 LLM）；LLM 不可用時自動 fallback
到 mock，不會 crash。同一個 seed 兩次執行產生相同軌跡。

說明（與 GAML 的差異）：GAML 以 `is_crowded = nearby>0` 觸發 recompute_path，
本實作改以「道路 congestion_proxy ≥ crowded_road_threshold」觸發，語意更貼近「避開壅塞」
且大幅減少不必要的最短路徑重算，讓互動式 demo 維持流暢。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from .. import config
from ..config import SimulationConfig
from ..domain.agent import VehicleAgent
from ..domain.events import RouteStatus
from ..domain.state import AgentSnapshot, RoadSnapshot, SimulationState
from ..decisions.base import DecisionPolicy
from ..decisions.llm_adapter import LLMDecisionPolicy
from ..decisions.mock_policy import MockDecisionPolicy
from ..spatial import gis_loader, geojson, routing
from ..spatial.road_network import RoadNetwork, load_road_network
from . import metrics
from .random_seed import make_rng
from .scheduler import Scheduler

logger = logging.getLogger(__name__)


def congestion_color(proxy: float) -> str:
    if proxy < 0.3:
        return "#00C853"
    if proxy < 0.7:
        return "#FFD600"
    if proxy < 0.9:
        return "#FF6D00"
    return "#D50000"


class SimulationEngine:
    """單一模擬執行個體（每個 WebSocket 連線一個）。"""

    def __init__(self, cfg: SimulationConfig | None = None) -> None:
        self.cfg = cfg or config.DEFAULT_CONFIG
        self.rng = make_rng(self.cfg.seed)
        self.scheduler = Scheduler(self.cfg.max_steps, self.cfg.step_minutes)
        self.recorder = metrics.MetricsRecorder(self.cfg)

        self.network: RoadNetwork | None = None
        self.agents: list[VehicleAgent] = []
        self.towns: list = []
        self._stadium_xy: tuple[float, float] = (0.0, 0.0)
        self._stadium_latlng: tuple[float, float] = (0.0, 0.0)
        self._dest_node: str | None = None
        self._available_towns: list[str] = []

        # 決策來源
        self._mock = MockDecisionPolicy(self.cfg, self.rng)
        self._llm = LLMDecisionPolicy(self.cfg)
        self.last_decision_source = "mock"

        self.running = False
        self.is_initialized = False

    # ==================================================================
    # 初始化（對齊 GAML init）
    # ==================================================================
    def initialize(self) -> None:
        logger.info("初始化模擬引擎…")
        self.towns = gis_loader.load_towns(self.cfg)
        self._available_towns = [t.town_name for t in self.towns]
        stadium_pt, self._stadium_latlng = gis_loader.load_stadium_point()
        self._stadium_xy = (stadium_pt.x, stadium_pt.y)

        self.network = load_road_network(self.cfg)
        self._dest_node = self.network.nearest_node(*self._stadium_xy)

        self._build_agents()
        self._initial_decisions()
        for agent in self.agents:
            self._place_agent(agent)

        self.recorder.init_csv()
        self.scheduler.reset()
        self.is_initialized = True
        logger.info("初始化完成：%d agents，目的地節點 %s", len(self.agents), self._dest_node)

    def _build_agents(self) -> None:
        self.agents = [
            VehicleAgent.from_config(f"vehicle_{i + 1:03d}", self.cfg)
            for i in range(self.cfg.nb_agents)
        ]

    def _initial_decisions(self) -> None:
        """指派 profile/起點/車種/初始 mode；LLM 失敗自動 fallback mock。"""
        assignments = {}
        if self.cfg.use_llm:
            assignments = self._llm.initialize_agents(self.agents, self._available_towns)
            if assignments and self._llm.last_call_ok:
                self.last_decision_source = "llm"
            else:
                logger.warning("LLM init 不可用，改用 mock")
                assignments = {}
        if not assignments:
            assignments = self._mock.initialize_agents(self.agents, self._available_towns)
            self.last_decision_source = "mock"

        for agent in self.agents:
            a = assignments.get(agent.agent_id)
            if a is None:
                continue
            agent.profile_name = a.profile_name or agent.agent_id
            agent.origin_town = a.origin_town or self.cfg.default_origin_town
            agent.apply_vehicle_type(a.vehicle_type)
            if a.active_mode:
                agent.active_mode = a.active_mode
            agent.api_status = "init_response_applied" if self.last_decision_source == "llm" else "mock"

    def _place_agent(self, agent: VehicleAgent) -> None:
        """把 agent 放到起點行政區內的路網節點，計算到球場的初始路徑。"""
        assert self.network is not None
        town = self._town_by_name(agent.origin_town)
        if town is not None:
            origin_node = self.network.random_node_in_town(town, self.rng)
        else:
            origin_node = self.network.nearest_node(*self._stadium_xy)

        agent.current_node = origin_node
        agent.destination_node = self._dest_node
        agent.x, agent.y = self.network.node_xy(origin_node)
        agent.destination_town = self.cfg.destination_town_name
        path = routing.find_path(self.network, origin_node, self._dest_node, agent.routing_weights())
        agent.current_path = path
        agent.path_index = 0
        agent.edge_progress = 0.0
        if path and len(path) > 1:
            agent.route_status = RouteStatus.MOVING
        else:
            agent.route_status = RouteStatus.ARRIVED if path else RouteStatus.ERROR
        self._refresh_agent_perception(agent, pre_move=True)

    # ==================================================================
    # 單步（對齊 GAML 每 cycle reflex）
    # ==================================================================
    def step(self) -> SimulationState:
        if not self.is_initialized or self.network is None:
            raise RuntimeError("引擎尚未初始化")

        cycle = self.scheduler.advance()

        # 1. 感知快照（用上一步遺留的道路壅塞）
        for agent in self.agents:
            self._refresh_agent_perception(agent, pre_move=True)

        # 2. 決策（LLM 或 mock；LLM 失敗 fallback）
        env = self._environment_summary(cycle)
        self._apply_step_decisions(env, cycle)

        # 3. 感知速度 + 移動（壅塞時重算路徑）
        for agent in self.agents:
            self._perceive_speed(agent)
            self._move_agent(agent)

        # 4. 重算道路 flow / congestion / weight
        self._recompute_flows()

        # 5. 移動後感知快照（供 memory / 輸出）
        for agent in self.agents:
            self._refresh_agent_perception(agent, pre_move=False)

        # 6. 指標 + 分佈
        env = self._environment_summary(cycle)
        mode_dist, status_dist = metrics.distributions(self.agents)
        self.recorder.record_cycle(cycle, env, mode_dist, status_dist)

        # 7. memory + CSV
        for agent in self.agents:
            agent.travel_memory.append(agent.build_memory_entry(cycle))
        self.recorder.append_agent_rows(cycle, self.agents)
        self.recorder.append_road_rows(cycle, self.network.all_roads())

        return self._snapshot(cycle, env, mode_dist, status_dist)

    def _apply_step_decisions(self, env: dict[str, Any], cycle: int) -> None:
        decisions = {}
        if self.cfg.use_llm:
            decisions = self._llm.decide_step(self.agents, env, cycle)
            if decisions and self._llm.last_call_ok:
                self.last_decision_source = "llm"
            else:
                decisions = {}
        if not decisions:
            decisions = self._mock.decide_step(self.agents, env, cycle)
            self.last_decision_source = "mock"

        for agent in self.agents:
            d = decisions.get(agent.agent_id)
            if d is None:
                continue
            if d.active_mode:
                agent.apply_active_mode(d.active_mode)
            if d.vehicle_type:
                agent.apply_vehicle_type(d.vehicle_type)

    # ==================================================================
    # 感知 / 移動
    # ==================================================================
    def _perceive_speed(self, agent: VehicleAgent) -> None:
        """依道路速限與壅塞調整速度（對齊 GAML perceive_environment）。"""
        if agent.route_status in (RouteStatus.ARRIVED, RouteStatus.ERROR):
            agent.speed_kmh = 0.0
            return
        base = agent.desired_speed
        road = self._current_road(agent)
        if road is not None:
            limit = road.speed_limit_for(agent.vehicle_type)
            if limit > 0.0 and base > limit:
                base = limit
            elif limit <= 0.0 and base > self.cfg.missing_road_speed_cap_kmh:
                base = self.cfg.missing_road_speed_cap_kmh
        agent.is_crowded = agent.congestion_proxy >= self.cfg.crowded_road_threshold
        agent.speed_kmh = base * (self.cfg.crowded_speed_factor if agent.is_crowded else 1.0)

    def _move_agent(self, agent: VehicleAgent) -> None:
        """沿路徑移動一個 step；壅塞時重算路徑（對齊 GAML move）。"""
        assert self.network is not None
        if agent.route_status in (RouteStatus.ARRIVED, RouteStatus.ERROR):
            agent.distance_moved_last_step = 0.0
            agent.selected_action = "arrived" if agent.route_status == RouteStatus.ARRIVED else "error"
            return

        # 壅塞 → 重算路徑（避開壅塞）
        if agent.is_crowded and agent.current_node and agent.destination_node:
            new_path = routing.find_path(
                self.network, agent.current_node, agent.destination_node, agent.routing_weights()
            )
            if len(new_path) > 1:
                agent.current_path = new_path
                agent.path_index = 0
                agent.edge_progress = 0.0
                agent.selected_action = "goto_destination_recompute_path"
            else:
                agent.selected_action = "goto_destination"
        else:
            agent.selected_action = "goto_destination"

        before = (agent.x, agent.y)
        remaining = agent.speed_kmh * (self.cfg.step_minutes / 60.0) * 1000.0  # 公尺
        self._advance_along_path(agent, remaining)
        agent.distance_moved_last_step = math.hypot(agent.x - before[0], agent.y - before[1])

        # 抵達判定：到達目的地節點（路徑走完）即視為抵達——球場 point 與最近路網節點之間
        # 有固定偏移，無法以對球場 point 的直線距離歸零，故以「抵達 target node」為準
        # （對齊 GAML 的 goto target node）。或直線距離已在門檻內。
        dist = math.hypot(agent.x - self._stadium_xy[0], agent.y - self._stadium_xy[1])
        path_done = agent.path_index >= len(agent.current_path) - 1
        if path_done or dist < self.cfg.arrival_distance_threshold_m:
            agent.route_status = RouteStatus.ARRIVED
            agent.selected_action = "arrived"
        else:
            agent.route_status = RouteStatus.MOVING

    def _advance_along_path(self, agent: VehicleAgent, distance_m: float) -> None:
        """沿節點路徑前進指定公尺，必要時在最後一段邊上做線性內插。"""
        assert self.network is not None
        path = agent.current_path
        remaining = distance_m
        while remaining > 0 and agent.path_index < len(path) - 1:
            u = path[agent.path_index]
            v = path[agent.path_index + 1]
            road = self.network.road_between(u, v)
            edge_len = max(road.length if road else 1.0, 1.0)
            left_in_edge = edge_len - agent.edge_progress  # 這條邊還剩多少（支援跨步推進長邊）
            if left_in_edge <= remaining:
                # 走完這條邊，進到下一個節點
                remaining -= left_in_edge
                agent.path_index += 1
                agent.edge_progress = 0.0
                agent.current_node = v
                agent.x, agent.y = self.network.node_xy(v)
            else:
                # 在邊上部分前進：累積 edge_progress 並線性內插座標
                agent.edge_progress += remaining
                frac = agent.edge_progress / edge_len
                ux, uy = self.network.node_xy(u)
                vx, vy = self.network.node_xy(v)
                agent.x = ux + (vx - ux) * frac
                agent.y = uy + (vy - uy) * frac
                remaining = 0.0

    def _recompute_flows(self) -> None:
        """依 agent 當前佔用的 edge 統計 flow，更新每條道路（對齊 GAML road.update_flow）。"""
        assert self.network is not None
        self.network.reset_flows()
        counts: dict[tuple[str, str], int] = {}
        for agent in self.agents:
            if agent.route_status != RouteStatus.MOVING:
                continue
            if agent.path_index < len(agent.current_path) - 1:
                u = agent.current_path[agent.path_index]
                v = agent.current_path[agent.path_index + 1]
                counts[(u, v)] = counts.get((u, v), 0) + 1
        for (u, v), flow in counts.items():
            road = self.network.road_between(u, v)
            if road is not None:
                road.update_flow(flow, self.cfg.capacity_fallback_vehicle_count,
                                 self.cfg.flow_weight_multiplier)

    # ==================================================================
    # 感知快照輔助
    # ==================================================================
    def _current_road(self, agent: VehicleAgent):
        if self.network is None:
            return None
        if agent.path_index < len(agent.current_path) - 1:
            return self.network.road_between(
                agent.current_path[agent.path_index], agent.current_path[agent.path_index + 1]
            )
        return None

    def _refresh_agent_perception(self, agent: VehicleAgent, pre_move: bool) -> None:
        """更新 agent 的 current road/town、congestion、鄰近數、距離。"""
        assert self.network is not None
        road = self._current_road(agent)
        agent.current_road_id = road.road_id if road else ""
        agent.congestion_proxy = road.congestion_proxy if road else 0.0
        agent.next_road_id = road.road_id if road else "unknown"
        agent.current_town = self._town_of_point(agent.x, agent.y)
        agent.distance_to_destination = math.hypot(
            agent.x - self._stadium_xy[0], agent.y - self._stadium_xy[1]
        )
        agent.nearby_agent_count = self._count_nearby(agent)

    def _count_nearby(self, agent: VehicleAgent) -> int:
        r2 = agent.perception_radius ** 2
        count = 0
        for other in self.agents:
            if other is agent or other.route_status == RouteStatus.ARRIVED:
                continue
            if (other.x - agent.x) ** 2 + (other.y - agent.y) ** 2 <= r2:
                count += 1
        return count

    def _town_of_point(self, x: float, y: float) -> str:
        from shapely.geometry import Point
        pt = Point(x, y)
        for town in self.towns:
            if town.contains_point(pt):
                return town.town_name
        # fallback：最近形心
        best = ""
        best_d = float("inf")
        for town in self.towns:
            if town.centroid_metric is None:
                continue
            d = (town.centroid_metric.x - x) ** 2 + (town.centroid_metric.y - y) ** 2
            if d < best_d:
                best_d, best = d, town.town_name
        return best

    def _town_by_name(self, name: str):
        for town in self.towns:
            if town.town_name == name:
                return town
        return None

    # ==================================================================
    # 摘要 / 快照
    # ==================================================================
    def _environment_summary(self, cycle: int) -> dict[str, Any]:
        assert self.network is not None
        env = metrics.overall_environment(self.network.all_roads(), self.cfg, len(self.agents))
        env["cycle"] = cycle
        env["elapsed_minutes"] = cycle * self.cfg.step_minutes
        return env

    def _snapshot(self, cycle: int, env: dict[str, Any],
                  mode_dist: dict[str, int], status_dist: dict[str, int]) -> SimulationState:
        assert self.network is not None
        agents_snap = []
        for a in self.agents:
            lat, lng = self._xy_to_latlng(a.x, a.y)
            agents_snap.append(AgentSnapshot(
                agent_id=a.agent_id, profile_name=a.profile_name, lat=lat, lng=lng,
                route_status=str(a.route_status), active_mode=a.active_mode,
                vehicle_type=a.vehicle_type, speed_kmh=round(a.speed_kmh, 2),
                congestion_proxy=round(a.congestion_proxy, 4),
                distance_to_destination=round(a.distance_to_destination, 1),
                nearby_agent_count=a.nearby_agent_count,
                origin_town=a.origin_town, destination_town=a.destination_town,
                current_town=a.current_town, current_road_id=a.current_road_id,
            ))
        # 只送有流量的道路（前端據此即時上色），避免每步送數萬條
        roads_snap = [
            RoadSnapshot(road_id=r.road_id, flow=r.current_flow, capacity=r.capacity,
                         congestion_proxy=round(r.congestion_proxy, 4),
                         color=congestion_color(r.congestion_proxy))
            for r in self.network.all_roads() if r.current_flow > 0
        ]
        return SimulationState(
            cycle=cycle, elapsed_minutes=env["elapsed_minutes"], max_steps=self.cfg.max_steps,
            running=self.running, finished=self.scheduler.finished,
            decision_source=self.last_decision_source,
            agents=agents_snap, roads=roads_snap,
            metrics={
                "active_road_count": env["active_road_count"],
                "crowded_road_count": env["crowded_road_count"],
                "average_congestion_proxy": env["average_congestion_proxy"],
                "history": self.recorder.history,
            },
            mode_distribution=mode_dist, status_distribution=status_dist,
        )

    def _xy_to_latlng(self, x: float, y: float) -> tuple[float, float]:
        # 用最近節點的 lat/lng 近似（避免每點都做投影轉換）；節點密集，誤差極小。
        node = self.network.nearest_node(x, y) if self.network else None
        if node is None:
            return (0.0, 0.0)
        return self.network.node_latlng(node)

    # ==================================================================
    # 控制
    # ==================================================================
    def pause(self) -> None:
        self.running = False

    def resume(self) -> None:
        self.running = True

    def reset(self) -> None:
        """重設並重新初始化（同 seed → 同初始狀態）。"""
        self.running = False
        self.rng = make_rng(self.cfg.seed)
        self._mock = MockDecisionPolicy(self.cfg, self.rng)
        self.recorder.reset()
        self.agents = []
        self.is_initialized = False
        self.initialize()

    def run_to_completion(self) -> list[SimulationState]:
        """同步跑到結束，回傳每步快照（測試/CLI 用）。"""
        states = []
        while not self.scheduler.finished:
            states.append(self.step())
        return states

    # ==================================================================
    # 前端初始化資料（GeoJSON）
    # ==================================================================
    def init_payload(self) -> dict[str, Any]:
        assert self.network is not None
        return {
            "type": "init",
            "towns_geojson": gis_loader.load_towns_geojson(),
            "roads_geojson": geojson.roads_to_geojson(self.network, only_major=True),
            "stadium": {"lat": self._stadium_latlng[0], "lng": self._stadium_latlng[1]},
            "config": {
                "max_steps": self.cfg.max_steps,
                "step_minutes": self.cfg.step_minutes,
                "nb_agents": self.cfg.nb_agents,
                "decision_source": self.last_decision_source,
            },
        }
