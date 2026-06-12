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
from ..domain import agent as agent_mod
from ..domain.agent import VehicleAgent
from ..domain.events import RouteStatus
from ..domain.state import AgentSnapshot, RoadSnapshot, SimulationState
from ..decisions.base import DecisionPolicy
from ..decisions.llm_adapter import LLMDecisionPolicy
from ..decisions.mock_policy import MockDecisionPolicy
from ..spatial import gis_loader, geojson, routing
from ..spatial import signals as signals_mod
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
        self.signals: signals_mod.SignalSystem = signals_mod.SignalSystem({}, 90.0, 3.0, enabled=False)
        self._elapsed_seconds: float = 0.0   # 號誌相位用：模擬已過秒數（= cycle × step_minutes × 60）
        self.agents: list[VehicleAgent] = []
        self.towns: list = []
        self._stadium_xy: tuple[float, float] = (0.0, 0.0)
        self._stadium_latlng: tuple[float, float] = (0.0, 0.0)
        self._dest_node: str | None = None
        self._available_towns: list[str] = []
        self._prev_avg_congestion: float | None = None   # 上一步全市平均壅塞（算 trend 用）

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
        self.signals = signals_mod.load_signal_system()
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
        """指派 profile/起點/車種/初始 mode。

        事件觸發模式（預設）：用規則式（mock）建立確定性基線，再用 persona 池**確定性覆寫**
        name/車種（不呼叫 LLM 做初始決策、開場不爆量）；初始 active_mode 維持規則式。
        關閉事件觸發（舊行為）：用 LLM init 決策；失敗 fallback mock。
        """
        sc = config.SCALING_CONFIG

        assignments = {}
        if self.cfg.use_llm and not sc.event_triggered_decisions:
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
                agent.apply_active_mode(a.active_mode)   # 套用 mode 的數值 + 路徑策略
            agent.api_status = "init_response_applied" if self.last_decision_source == "llm" else "mock"

        # LLM 模式（不分事件觸發）：persona 池為「出生地 / name / 車種」的單一真實來源，
        # 確定性覆寫之（初始 active_mode 維持上面的規則式或 LLM init 結果）。
        # 出生地只在此處設一次；之後每步決策不會、也不應再更動出生地（agent 只出生一次）。
        if self.cfg.use_llm:
            from ..decisions import profile_pool
            if profile_pool.assign_to_agents(self.agents, config.PROFILE_CONFIG.pool_size,
                                             self._available_towns, self.cfg.default_origin_town):
                self.last_decision_source = "llm"
                for agent in self.agents:
                    agent.api_status = "persona_assigned"
            else:
                logger.warning("persona 池不可用（Ollama？），沿用既有人物")

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
        path = routing.find_path(self.network, origin_node, self._dest_node,
                                 agent.routing_strategy(), seed=self.cfg.seed)
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
        self._elapsed_seconds = cycle * self.cfg.step_minutes * 60.0  # 號誌相位基準時間

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
        # 記下本步全市平均壅塞，供「下一步」算 congestion_trend
        self._prev_avg_congestion = env["average_congestion_proxy"]

        # 7. memory + CSV
        llm_summary = config.SUMMARY_CONFIG.use_llm_summary
        for agent in self.agents:
            agent.update_memory(cycle, self.cfg.step_minutes, config.MEMORY_CONFIG,
                                llm_summary_mode=llm_summary)
        self._maybe_llm_summaries(cycle)
        self.recorder.append_agent_rows(cycle, self.agents)
        self.recorder.append_road_rows(cycle, self.network.all_roads())

        return self._snapshot(cycle, env, mode_dist, status_dist)

    def _maybe_llm_summaries(self, cycle: int) -> None:
        """開啟 [summary] 時，每 N 步或有人抵達就用小模型批次重算 trip_summary。

        失敗（匯入/呼叫/解析）一律保留各 agent 既有的模板摘要，不中斷模擬。
        """
        sc = config.SUMMARY_CONFIG
        if not sc.use_llm_summary or not self.agents:
            return
        just_arrived = any(a.route_status == RouteStatus.ARRIVED for a in self.agents)
        if cycle % sc.summary_every_n_steps != 0 and not just_arrived:
            return
        try:
            from llm_server.memory_summary import run_memory_summary
        except ImportError as e:
            logger.warning("memory_summary 匯入失敗，改用模板摘要：%s", e)
            return
        facts = [a.memory_facts() for a in self.agents if a.long_term_memory]
        summaries = run_memory_summary(facts, sc.summary_model)
        if not summaries:
            return
        for a in self.agents:
            s = summaries.get(a.agent_id)
            if s:
                a.long_term_memory["trip_summary"] = s
                a.summary_source = "llm"

    def _llm_environment(self, env: dict[str, Any]) -> dict[str, Any]:
        """給 LLM 決策用的精簡質性全域環境。

        只留決策相關欄位；展示用統計（agent_count / active_road_count / crowded_road_count /
        average_congestion_proxy 等裸值）繼續給 recorder/前端/CSV，不進 LLM。
        詳見 docs/ENVIRONMENT_zh-TW.md。
        """
        return {
            "cycle": env["cycle"],
            "destination_town": env["destination_town"],
            "overall_traffic": agent_mod._traffic_feel(
                env["average_congestion_proxy"], False, config.MEMORY_CONFIG),
            "congestion_trend": env["congestion_trend"],
            "congestion_hotspots": env["congestion_hotspots"],
        }

    def _apply_step_decisions(self, env: dict[str, Any], cycle: int) -> None:
        """每步決策。詳見 docs/SCALING_zh-TW.md。

        - mock 模式：每步對全部 agent 決策（便宜、確定性，不變）。
        - LLM 模式 + 事件觸發（預設）：只對「踩到壅塞/前方塞」的 agent 重決，分批並行。
        - LLM 模式 + 關閉事件觸發：退回「每步對全部 agent 決策」的舊行為。
        """
        sc = config.SCALING_CONFIG

        if not self.cfg.use_llm:
            self.last_decision_source = "mock"
            self._apply_decisions(self._mock.decide_step(self.agents, env, cycle))
            return

        if not sc.event_triggered_decisions:  # 舊行為：每步決策全部
            decisions = self._llm.decide_step(self.agents, self._llm_environment(env), cycle)
            if decisions and self._llm.last_call_ok:
                self.last_decision_source = "llm"
            else:
                decisions = self._mock.decide_step(self.agents, env, cycle)
                self.last_decision_source = "mock"
            self._apply_decisions(decisions)
            return

        # 事件觸發：只決策觸發的 agent（順暢的車維持現有 mode）
        self.last_decision_source = "llm"
        triggered = self._triggered_agents(cycle)
        if not triggered:
            return  # 沒人觸發 → 不呼叫 LLM
        self._apply_decisions(self._llm_decide_batched(triggered, self._llm_environment(env), cycle))

    def _apply_decisions(self, decisions: dict[str, Any]) -> None:
        """依 agent_id 順序套用決策（確定性，與批次回來的順序無關）。"""
        for agent in self.agents:
            d = decisions.get(agent.agent_id)
            if d is None:
                continue
            if d.active_mode:
                agent.apply_active_mode(d.active_mode)
            if d.vehicle_type:
                agent.apply_vehicle_type(d.vehicle_type)
            if d.reason:
                agent.decision_reason = d.reason

    def _triggered_agents(self, cycle: int) -> list[VehicleAgent]:
        """回傳本步「需要重決」的 agent：壅塞訊號上升緣 + 過了 cooldown。"""
        sc = config.SCALING_CONFIG
        thr = self.cfg.crowded_road_threshold
        out: list[VehicleAgent] = []
        for a in self.agents:
            if a.route_status in (RouteStatus.ARRIVED, RouteStatus.ERROR):
                a._prev_congestion_signal = False
                continue
            # 腳下壅塞 或 前方路徑有塞點（congestion_proxy / road_ahead 已於 pre-move 感知算好）
            signal = (a.congestion_proxy >= thr) or (a.road_ahead not in ("", agent_mod.AHEAD_CLEAR))
            if signal and not a._prev_congestion_signal and cycle >= a._decision_cooldown_until:
                out.append(a)
                a._decision_cooldown_until = cycle + sc.cooldown_steps
            a._prev_congestion_signal = signal
        return out

    def _llm_decide_batched(self, agents: list[VehicleAgent], env: dict[str, Any],
                            cycle: int) -> dict[str, Any]:
        """把觸發的 agent 分批、並行送 LLM（同步等齊再回傳合併決策）。"""
        sc = config.SCALING_CONFIG
        batches = [agents[i:i + sc.batch_size] for i in range(0, len(agents), sc.batch_size)]
        merged: dict[str, Any] = {}
        if len(batches) <= 1:
            merged.update(self._llm.decide_step(batches[0], env, cycle) if batches else {})
            return merged
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(sc.concurrency, len(batches))) as ex:
            futures = [ex.submit(self._llm.decide_step, b, env, cycle) for b in batches]
            for f in futures:
                try:
                    merged.update(f.result())
                except Exception as e:  # noqa: BLE001  單批失敗 → 該批維持現有 mode
                    logger.warning("批次決策失敗：%s", e)
        return merged

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
        agent.waiting_at_signal = False   # 每步先清，_advance_along_path 停在紅燈時才設 True

        # 壅塞 → 重算路徑（避開壅塞）；tolerate_congestion 的 recompute_on_crowded=False 不重算
        if agent.is_crowded and agent.recompute_on_crowded and agent.current_node and agent.destination_node:
            new_path = routing.find_path(
                self.network, agent.current_node, agent.destination_node,
                agent.routing_strategy(), seed=self.cfg.seed,
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
            # 號誌 gating：剛好停在路口節點（edge_progress==0）、要進入下一條邊前先看燈。
            # 控制相位的是「進場方向」＝ 前一節點→本節點；紅燈 → 停在路口、本步不再前進。
            if agent.edge_progress == 0.0 and agent.path_index > 0:
                node = path[agent.path_index]
                if self.signals.is_signalized(node):
                    bearing = self._bearing(path[agent.path_index - 1], node)
                    if not self.signals.is_green(node, bearing, self._elapsed_seconds):
                        agent.waiting_at_signal = True
                        agent.selected_action = "wait_at_signal"
                        break
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

    def _bearing(self, u: str, v: str) -> float:
        """節點 u→v 的方位角（度，0..360，公尺座標）。號誌進場方向判定用。"""
        ux, uy = self.network.node_xy(u)
        vx, vy = self.network.node_xy(v)
        return math.degrees(math.atan2(vy - uy, vx - ux)) % 360.0

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
        agent.current_road_name = road.road_name if road else ""
        agent.current_road_class = agent_mod.clean_highway(road.highway) if road else ""
        agent.congestion_proxy = road.congestion_proxy if road else 0.0
        agent.next_road_id = road.road_id if road else "unknown"
        agent.current_town = self._town_of_point(agent.x, agent.y)
        agent.distance_to_destination = math.hypot(
            agent.x - self._stadium_xy[0], agent.y - self._stadium_xy[1]
        )
        agent.nearby_agent_count = self._count_nearby(agent)
        # 送 LLM 的環境感知質性標籤（腳下壅塞 / 速度感 / 前方路況）
        self._refresh_env_labels(agent, road)

    def _refresh_env_labels(self, agent: VehicleAgent, road) -> None:
        """算好 agent 當下環境的質性標籤（traffic_here / speed_status / road_ahead）。"""
        mem = config.MEMORY_CONFIG
        pc = config.PERCEPTION_CONTEXT
        crowded = agent.congestion_proxy >= self.cfg.crowded_road_threshold
        agent.traffic_here = agent_mod._traffic_feel(agent.congestion_proxy, crowded, mem)
        if agent.route_status == RouteStatus.ARRIVED:
            agent.speed_status = agent_mod.SPEED_ARRIVED
        else:
            limit = road.speed_limit_for(agent.vehicle_type) if road else agent.desired_speed
            agent.speed_status = agent_mod.speed_status_label(agent.speed_kmh, limit, pc)
        agent.road_ahead = self._road_ahead(agent, mem.feel_congested_proxy, pc.lookahead_distance_m)

    def _road_ahead(self, agent: VehicleAgent, congested_proxy: float, lookahead_m: float) -> str:
        """沿 current_path 從下一段起往前看 lookahead_m，回傳第一個壅塞段的描述。

        只看「前方」（不含腳下這條，腳下已由 traffic_here 表示）。無壅塞或已抵達→順暢/空。
        """
        if agent.route_status in (RouteStatus.ARRIVED, RouteStatus.ERROR):
            return ""
        path = agent.current_path
        i = agent.path_index
        if not path or i >= len(path) - 1:
            return agent_mod.AHEAD_CLEAR
        # 先把目前這條邊的剩餘距離算進「到前方的距離」
        cur = self.network.road_between(path[i], path[i + 1]) if self.network else None
        acc = max((cur.length if cur else 0.0) - agent.edge_progress, 0.0)
        for k in range(i + 1, len(path) - 1):
            seg = self.network.road_between(path[k], path[k + 1])
            if seg is None:
                continue
            if seg.congestion_proxy >= congested_proxy:
                return agent_mod.road_ahead_label(acc, seg.road_name)
            acc += seg.length
            if acc >= lookahead_m:
                break
        return agent_mod.AHEAD_CLEAR

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
        # 讓 LLM 看懂「塞在哪、變好還變壞」：壅塞趨勢 + 行政區級熱點（全域只送一份）
        env["congestion_trend"] = self._congestion_trend(env["average_congestion_proxy"])
        env["congestion_hotspots"] = self._congestion_hotspots()
        return env

    def _congestion_trend(self, current_avg: float) -> str:
        """全市平均壅塞 vs 上一步 → 改善中 / 持平 / 惡化中。"""
        prev = self._prev_avg_congestion
        if prev is None:
            return "持平"
        delta = current_avg - prev
        if delta > 0.02:
            return "惡化中"
        if delta < -0.02:
            return "改善中"
        return "持平"

    def _congestion_hotspots(self) -> list[dict[str, Any]]:
        """行政區級壅塞熱點（top-K）。

        壅塞只存在於有 agent 的路段（flow 來自 agent），故由 agent 的所在區 + 路況聚合即可，
        成本為 O(agent 數)、不掃全路網、不乘 context。只列出「有壅塞」的行政區。
        """
        from collections import defaultdict
        thr = self.cfg.crowded_road_threshold
        crowded_roads: dict[str, set] = defaultdict(set)
        cong_by_town: dict[str, list] = defaultdict(list)
        for a in self.agents:
            if not a.current_town or a.route_status == RouteStatus.ARRIVED:
                continue
            cong_by_town[a.current_town].append(a.congestion_proxy)
            if a.congestion_proxy >= thr and a.current_road_id:
                crowded_roads[a.current_town].add(a.current_road_id)
        mem = config.MEMORY_CONFIG
        rows: list[dict[str, Any]] = []
        for town, congs in cong_by_town.items():
            crowded = len(crowded_roads.get(town, ()))
            if crowded == 0:
                continue
            avg = sum(congs) / len(congs)
            rows.append({
                "town": town,
                "level": agent_mod._traffic_feel(avg, False, mem),
                "crowded_roads": crowded,
            })
        rows.sort(key=lambda r: r["crowded_roads"], reverse=True)
        return rows[:config.PERCEPTION_CONTEXT.hotspots_top_k]

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
                waiting_at_signal=a.waiting_at_signal,
                trip_summary=a.long_term_memory.get("trip_summary", ""),
                summary_source=a.summary_source,
                decision_reason=a.decision_reason,
            ))
        # 只送有流量的道路（前端據此即時上色），避免每步送數萬條。
        # 附幾何座標：前端對「非主要道路底圖沒有的路」也能疊畫出壅塞。
        roads_snap = []
        for r in self.network.all_roads():
            if r.current_flow <= 0:
                continue
            coords = []
            if r.geometry_wgs84 is not None:
                coords = [[round(x, 6), round(y, 6)] for x, y in r.geometry_wgs84.coords]
            roads_snap.append(RoadSnapshot(
                road_id=r.road_id, flow=r.current_flow, capacity=r.capacity,
                congestion_proxy=round(r.congestion_proxy, 4),
                color=congestion_color(r.congestion_proxy), coords=coords))
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
        self._prev_avg_congestion = None
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
            "signals": self.signals.phase_payload(),
            "stadium": {"lat": self._stadium_latlng[0], "lng": self._stadium_latlng[1]},
            "agent_profiles": self._load_agent_profiles(),
            "config": {
                "max_steps": self.cfg.max_steps,
                "step_minutes": self.cfg.step_minutes,
                "nb_agents": self.cfg.nb_agents,
                "decision_source": self.last_decision_source,
                "ui": config.UI_CONFIG.to_payload(),
            },
        }

    def _load_agent_profiles(self) -> dict[str, Any]:
        """回傳 {name: {identity, traits}} 供前端 inspect（以 identity.name 對應 profile_name）。

        讀 persona 池（robust 解析，見 decisions/profile_pool）。池不存在回 {}。
        """
        from ..decisions import profile_pool
        profiles: dict[str, Any] = {}
        for a in profile_pool.load_pool():
            ident = a.get("identity") or {}
            name = str(ident.get("name") or "").strip()
            if name:
                profiles[name] = {"identity": ident, "traits": a.get("traits") or {}}
        return profiles
