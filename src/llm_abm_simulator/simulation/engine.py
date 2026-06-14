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
from .. import scenarios
from ..config import SimulationConfig
from ..domain import agent as agent_mod
from ..domain.agent import VehicleAgent
from ..domain.events import RouteStatus
from ..domain.state import AgentSnapshot, RoadSnapshot, SimulationState
from ..decisions import registry as core_registry
from ..decisions.base import DecisionPolicy
from ..decisions.llm_adapter import LLMDecisionPolicy
from ..decisions.mock_policy import MockDecisionPolicy
from ..mobility import demand as demand_mod
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
        self._dest_town: str = self.cfg.destination_town_name  # 由場景覆寫（initialize 時設）
        self._available_towns: list[str] = []
        self._prev_avg_congestion: float | None = None   # 上一步全市平均壅塞（算 trend 用）
        self._avoid_circles: list[tuple[float, float, float]] = []   # NL 介入：避讓區 (x,y,radius_m)

        # 背景常態車流（ambient）與「路網層」交通評估累積器
        self._ambient_count: int = 0
        self._road_peak: dict[str, dict[str, Any]] = {}   # road_id → {name, peak_proxy, peak_flow}
        self._event_vehsteps: int = 0                     # 事件車「車·步」累積（路網負載占比用）
        self._ambient_vehsteps: int = 0                   # 背景車「車·步」累積

        # 規模化：節點→行政區索引、鄰近空間網格、前端可視範圍
        self._town_nodes: dict[str, list[str]] = {}       # 區名 → 覆蓋它的節點清單（init 一次性建，放置 O(1)）
        self._node_town: dict[str, str] = {}              # 節點 → 所屬區（同一索引反向；current_town O(1)）
        self._nearby_grid: dict[tuple[int, int], int] | None = None  # 每步重建的鄰近計數網格
        self._nearby_cell: float = 1.0
        self._view: dict[str, float] | None = None        # 前端回報的可視範圍（公尺框 + zoom）；大規模裁切用
        self._to_metric = None                            # lazy pyproj transformer（set_view 用）

        # Decision 即時日誌（走 WebSocket 取代讀 txt 檔）：本步重決的車 + 解析健康度
        self._decision_log: list[dict[str, Any]] = []
        self._decision_health: dict[str, Any] = {}

        # 決策核心（可選；見 decisions/registry.py）：規則式（rule）/ LLM 認知核心（llm）
        self._mock = MockDecisionPolicy(self.cfg, self.rng)   # 規則式核心（背景車流也用它）
        self._llm = LLMDecisionPolicy(self.cfg)
        self.last_decision_source = "rule"

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

        self._dest_town = scenarios.active().dest_town or self.cfg.destination_town_name
        self._avoid_circles = []   # 每次初始化清掉介入
        self.network = load_road_network(self.cfg)
        self.signals = signals_mod.load_signal_system()
        self._dest_node = self.network.nearest_node(*self._stadium_xy)
        self._build_town_node_index()   # ① 節點→行政區一次性索引（放置 O(1)）

        self._road_peak = {}
        self._event_vehsteps = 0
        self._ambient_vehsteps = 0

        self._build_agents()
        self._initial_decisions()
        # 事件車出生地解耦：用重力模型（人口+距離衰減）覆寫出生地；停用/無人口資料則保留既有指派。
        demand_mod.assign_origin_towns(self.agents, self.towns, self._stadium_xy,
                                       self.rng, config.DEMAND_CONFIG)
        # 背景常態車流：用雙邊重力 OD 生成不指定事件終點的常態車流（一律規則式、無記憶）。
        self._build_ambient_agents()
        for agent in self.agents:
            self._place_agent(agent)
        self._assign_departures()   # 事件車分批出發（時空需求）；window=0 → 全部 cycle 0 出發

        self.recorder.init_csv()
        self.scheduler.reset()
        self.is_initialized = True
        logger.info("初始化完成：%d 事件車 + %d 背景車，事件目的地節點 %s",
                    len(self.agents) - self._ambient_count, self._ambient_count, self._dest_node)

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
            self.last_decision_source = "rule"

        for agent in self.agents:
            a = assignments.get(agent.agent_id)
            if a is None:
                continue
            agent.profile_name = a.profile_name or agent.agent_id
            agent.origin_town = a.origin_town or self.cfg.default_origin_town
            agent.apply_vehicle_type(a.vehicle_type)
            if a.active_mode:
                agent.apply_active_mode(a.active_mode)   # 套用 mode 的數值 + 路徑策略
            agent.api_status = "init_response_applied" if self.last_decision_source == "llm" else "rule"

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

    def _build_town_node_index(self) -> None:
        """① 一次性把每個節點歸到「覆蓋它的行政區」清單。

        判定（``geom.covers``）與成員順序刻意與 ``random_node_in_town`` 的 ``inside`` 完全一致，
        確保改用索引後**放置結果與逐台版完全相同**（同 seed 同軌跡）。

        效能（往全台南數萬節點）：(i) **Point 預先建一次重用**（取代原本每區迴圈重複建構）；
        (ii) **STRtree 以 bbox 先篩候選節點、再 covers 確認**，把 O(節點×區) 降到 ~O(節點)；
        候選索引昇冪排序後映射回 ``nodes`` → covered 清單的**集合與順序與全掃版完全相同**。
        """
        from shapely.geometry import Point
        from shapely import STRtree
        assert self.network is not None
        nodes = list(self.network.graph.nodes())
        pts = [Point(*self.network.node_xy(n)) for n in nodes]   # 預建一次，全程重用
        tree = STRtree(pts)
        idx: dict[str, list[str]] = {}
        node_town: dict[str, str] = {}
        for t in self.towns:
            geom = t.geometry_metric
            if geom is None:
                idx[t.town_name] = []
                continue
            # bbox 先篩候選（query 回 pts 索引）→ 昇冪還原成 nodes 原順序 → covers 確認
            cand = sorted(int(i) for i in tree.query(geom))
            covered = [nodes[i] for i in cand if geom.covers(pts[i])]
            idx[t.town_name] = covered
            for n in covered:           # 反向表：邊界節點被多區覆蓋時，第一個區勝出（確定性）
                node_town.setdefault(n, t.town_name)
        self._town_nodes = idx
        self._node_town = node_town
        logger.info("節點→行政區索引完成：%d 節點 / %d 區", len(nodes), len(idx))

    def _node_in_town(self, town_name: str) -> str:
        """在指定行政區內抽一個節點（O(1)）。與 ``random_node_in_town`` 同邏輯、同 rng 消耗：
        覆蓋清單非空 → 隨機抽；否則退到形心最近節點；再否則全域隨機。"""
        assert self.network is not None
        nodes = self._town_nodes.get(town_name)
        if nodes:
            return nodes[self.rng.randrange(len(nodes))]
        t = self._town_by_name(town_name)
        if t is not None and t.centroid_metric is not None:
            return self.network.nearest_node(t.centroid_metric.x, t.centroid_metric.y)
        return self.network._node_ids[self.rng.randrange(len(self.network._node_ids))]

    def _place_agent(self, agent: VehicleAgent) -> None:
        """把 agent 放到起點行政區內的路網節點，計算到目的地的初始路徑。

        事件車（role=event）：目的地＝事件地點（球場）。
        背景車（role=ambient）：目的地＝其 destination_town 內的隨機節點（不指定事件終點）。
        """
        assert self.network is not None
        town = self._town_by_name(agent.origin_town)
        if town is not None:
            origin_node = self._node_in_town(agent.origin_town)
        else:
            origin_node = self.network.nearest_node(*self._stadium_xy)

        if agent.role == "ambient":
            dtown = self._town_by_name(agent.destination_town)
            dest_node = (self._node_in_town(agent.destination_town)
                         if dtown is not None else self._dest_node)
        else:
            agent.destination_town = self._dest_town
            dest_node = self._dest_node

        agent.current_node = origin_node
        agent.destination_node = dest_node
        agent.x, agent.y = self.network.node_xy(origin_node)
        path = self._route(origin_node, dest_node, agent.routing_strategy())
        agent.current_path = path
        agent.path_index = 0
        agent.edge_progress = 0.0
        if path and len(path) > 1:
            agent.route_status = RouteStatus.MOVING
        else:
            agent.route_status = RouteStatus.ARRIVED if path else RouteStatus.ERROR
        self._refresh_agent_perception(agent, pre_move=True)

    # ==================================================================
    # 背景常態車流（ambient）
    # ==================================================================
    def _event_agents(self) -> list[VehicleAgent]:
        return [a for a in self.agents if a.role == "event"]

    def _ambient_agents(self) -> list[VehicleAgent]:
        return [a for a in self.agents if a.role == "ambient"]

    def _build_ambient_agents(self) -> None:
        """依雙邊重力 OD 生成背景常態車流，append 到 self.agents（尚未 place）。

        無人口資料（sample_od_pairs 回 None）→ 自動停用背景車流。背景車一律 role=ambient、
        汽機車隨機（seeded），其餘參數同預設；決策一律走規則式核心、不存記憶。
        """
        n = config.effective_ambient_count()
        if n <= 0:
            self._ambient_count = 0
            return
        pairs = demand_mod.sample_od_pairs(self.towns, n, self.rng, config.DEMAND_CONFIG)
        if not pairs:
            logger.warning("無人口資料，背景常態車流停用（fallback）。")
            self._ambient_count = 0
            return
        for i, (origin, dest) in enumerate(pairs):
            a = VehicleAgent.from_config(f"ambient_{i + 1:04d}", self.cfg)
            a.role = "ambient"
            a.profile_name = f"背景車{i + 1:04d}"
            a.origin_town = origin
            a.destination_town = dest
            a.apply_vehicle_type(self.rng.choice(config.VEHICLE_TYPES))
            a.apply_active_mode(self.rng.choice(config.ACTIVE_MODES))
            a.api_status = "ambient"
            self.agents.append(a)
        self._ambient_count = len(pairs)
        logger.info("背景常態車流：生成 %d 台（雙邊重力 OD，規則式核心）", self._ambient_count)

    def _respawn_arrived_ambient(self) -> None:
        """背景車抵達 → 從目前位置以重力抽新目的地、重新規劃（維持穩態背景負載）。

        不 teleport：以目前所在節點為新起點、依距離衰減抽新目的地（像真實駕駛完成一趟再啟程）。
        關閉 respawn 時抵達即停（不重生）。
        """
        if not config.AMBIENT_CONFIG.respawn or self.network is None:
            return
        for a in self._ambient_agents():
            if a.route_status != RouteStatus.ARRIVED or not a.current_node:
                continue
            dest = demand_mod.sample_dest_town(self.towns, (a.x, a.y), self.rng, config.DEMAND_CONFIG)
            dtown = self._town_by_name(dest) if dest else None
            dnode = (self._node_in_town(dest)
                     if dtown is not None else self._dest_node)
            path = self._route(a.current_node, dnode, a.routing_strategy())
            if len(path) <= 1:
                continue
            a.origin_town = a.current_town or a.origin_town
            a.destination_town = dest or a.destination_town
            a.destination_node = dnode
            a.current_path, a.path_index, a.edge_progress = path, 0, 0.0
            a.route_status = RouteStatus.MOVING
            a.arrival_cycle = None

    def _dest_xy(self, agent: VehicleAgent) -> tuple[float, float]:
        """agent 自己的目的地座標（事件車＝球場節點；背景車＝其目的地節點）。"""
        if agent.destination_node and self.network is not None:
            return self.network.node_xy(agent.destination_node)
        return self._stadium_xy

    def _assign_departures(self) -> None:
        """事件車分批出發：依 `[departure]` 在 [0, 視窗] 內抽每台的 `departure_cycle`（seeded、可重現）。

        `window_minutes=0` → 全部 0（同時出發＝舊行為）。未到 `departure_cycle` 的車標 `waiting_for_origin`
        （尚未進場：不移動、不算流量、不顯示）。背景車不分批。視窗 clamp 到 max_steps-1，確保都會出發。
        """
        dc = config.DEPARTURE_CONFIG
        window = max(0, round(dc.window_minutes / max(1, self.cfg.step_minutes)))
        window = min(window, max(0, self.cfg.max_steps - 1))
        for a in self._event_agents():
            if window <= 0:
                a.departure_cycle = 0
                continue
            u = self.rng.random()
            if dc.profile == "front_loaded":
                u = u * u                              # 偏早出發
            elif dc.profile == "peak":
                u = 1.0 - (1.0 - u) * (1.0 - u)        # 偏接近開賽（晚）
            a.departure_cycle = int(round(u * window))
            if a.departure_cycle > 0 and a.route_status == RouteStatus.MOVING:
                a.waiting_for_origin = True            # 尚未進場
                a.route_status = RouteStatus.CREATED

    def _activate_due_departures(self, cycle: int) -> None:
        """到出發時間的事件車轉為進場（開始移動）。"""
        for a in self.agents:
            if a.waiting_for_origin and a.departure_cycle <= cycle:
                a.waiting_for_origin = False
                if a.route_status == RouteStatus.CREATED:
                    a.route_status = RouteStatus.MOVING

    # ==================================================================
    # 單步（對齊 GAML 每 cycle reflex）
    # ==================================================================
    def step(self) -> SimulationState:
        if not self.is_initialized or self.network is None:
            raise RuntimeError("引擎尚未初始化")

        cycle = self.scheduler.advance()
        self._elapsed_seconds = cycle * self.cfg.step_minutes * 60.0  # 號誌相位基準時間
        self._activate_due_departures(cycle)   # 分批出發：到 departure_cycle 的事件車進場

        # 1. 感知快照（用上一步遺留的道路壅塞；尚未進場的車跳過）
        if self.cfg.nearby_mode == "grid":
            self._build_nearby_grid()
        for agent in self.agents:
            if not agent.waiting_for_origin:
                self._refresh_agent_perception(agent, pre_move=True)

        # 2. 決策（LLM 或 mock；LLM 失敗 fallback）
        env = self._environment_summary(cycle)
        self._apply_step_decisions(env, cycle)

        # 3. 感知速度 + 移動（壅塞時重算路徑）；尚未進場的車不動；背景車抵達即以新 OD 重生
        for agent in self.agents:
            if agent.waiting_for_origin:
                continue
            self._perceive_speed(agent)
            self._move_agent(agent)
        self._respawn_arrived_ambient()

        # 4. 重算道路 flow / congestion / weight（含背景車 → 路網層負載 + 瓶頸累積）
        self._recompute_flows()

        # 5. 移動後感知快照（供 memory / 輸出；尚未進場的車跳過）
        if self.cfg.nearby_mode == "grid":
            self._build_nearby_grid()
        for agent in self.agents:
            if not agent.waiting_for_origin:
                self._refresh_agent_perception(agent, pre_move=False)

        # 6. 指標 + 分佈（事件 KPI 只算事件車；路網層壅塞/流量含背景車）
        event_agents = self._event_agents()
        ambient_agents = self._ambient_agents()
        env = self._environment_summary(cycle)
        # 抵達週期只記一次（供旅行時間分析；只給事件車）
        for a in event_agents:
            if a.route_status == RouteStatus.ARRIVED and a.arrival_cycle is None:
                a.arrival_cycle = cycle
        env["signal_waiting"] = sum(1 for a in event_agents if a.waiting_at_signal)
        env["event_on_network"] = sum(1 for a in event_agents if a.route_status == RouteStatus.MOVING)
        env["ambient_on_network"] = sum(1 for a in ambient_agents if a.route_status == RouteStatus.MOVING)
        self._event_vehsteps += env["event_on_network"]
        self._ambient_vehsteps += env["ambient_on_network"]
        mode_dist, status_dist = metrics.distributions(event_agents)
        self.recorder.record_cycle(cycle, env, mode_dist, status_dist)
        # 記下本步全市平均壅塞，供「下一步」算 congestion_trend
        self._prev_avg_congestion = env["average_congestion_proxy"]

        # 7. memory（只給已進場的事件車；LLM 核心的摘要已於決策時重寫）+ CSV
        for agent in event_agents:
            if not agent.waiting_for_origin:
                agent.update_memory(cycle, self.cfg.step_minutes, config.MEMORY_CONFIG)
        self.recorder.append_agent_rows(cycle, self.agents)
        self.recorder.append_road_rows(cycle, self.network.all_roads())

        return self._snapshot(cycle, env, mode_dist, status_dist)

    def _summarize_memory(self, agents: list[VehicleAgent]) -> None:
        """用小模型批次把這批 agent 的單一 ``memory.summary`` 重寫一次。

        只在它們「重新決策」時呼叫（見 `_apply_step_decisions`）——記憶恰好在做決定的當下最新，
        也省 LLM（不每步、不對全車）。失敗（匯入/呼叫/解析）一律保留既有摘要/模板，不中斷模擬。
        整套統一：摘要也用前端所選的模型（current_model）；取不到才退回 [summary].summary_model。
        """
        agents = [a for a in agents if a.memory]
        if not agents:
            return
        try:
            from llm_server.memory_summary import run_memory_summary
        except ImportError as e:
            logger.warning("memory_summary 匯入失敗，沿用模板摘要：%s", e)
            return
        try:
            from llm_server import llm_config as _lc
            model = _lc.current_model() or config.SUMMARY_CONFIG.summary_model
        except ImportError:
            model = config.SUMMARY_CONFIG.summary_model

        # ④ 分批（比照決策的 token 預算切批、可並行）：避免大規模時上千台塞一個 prompt 爆 context。
        sc = config.SCALING_CONFIG
        bsize = self._budget_batch_size(agents, sc.batch_size)
        batches = [agents[i:i + bsize] for i in range(0, len(agents), bsize)]

        def _one(batch: list[VehicleAgent]) -> dict[str, str]:
            try:
                return run_memory_summary([a.memory_facts() for a in batch], model)
            except Exception as e:  # noqa: BLE001  單批失敗不影響其他批
                logger.warning("記憶摘要批次失敗：%s", e)
                return {}

        merged: dict[str, str] = {}
        if len(batches) <= 1:
            merged = _one(batches[0]) if batches else {}
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(sc.concurrency, len(batches))) as ex:
                for f in [ex.submit(_one, b) for b in batches]:
                    try:
                        merged.update(f.result())
                    except Exception as e:  # noqa: BLE001
                        logger.warning("記憶摘要批次取結果失敗：%s", e)
        if not merged:
            return
        for a in agents:
            s = merged.get(a.agent_id)
            if s:
                a.memory["summary"] = s
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
        """每步決策。詳見 docs/SCALING_zh-TW.md、docs/AMBIENT_zh-TW.md。

        - 背景車（ambient）：一律規則式核心（便宜、無 LLM、無記憶）。
        - 事件車 + 規則式核心：每步對全部事件車決策（便宜、確定性）。
        - 事件車 + LLM 核心 + 事件觸發（預設）：只對「踩到壅塞/前方塞」的事件車重決，分批並行；
          重決前順手用 LLM 重寫其記憶 summary（記憶在做決定的當下最新）。
        - 事件車 + LLM 核心 + 關閉事件觸發：退回「每步對全部事件車決策」的舊行為。
        """
        sc = config.SCALING_CONFIG
        self._decision_log = []     # 本步決策日誌（走 WS 給前端；每步重置）
        self._decision_health = {"triggered": 0, "decided": 0, "fallback": 0, "source": "rule"}
        # 只決策「已進場」的事件車（尚未出發的不決策）
        event_agents = [a for a in self._event_agents() if not a.waiting_for_origin]
        ambient_agents = self._ambient_agents()

        # 背景車一律規則式核心（不吃 LLM、不存記憶）
        if ambient_agents:
            self._apply_decisions(self._mock.decide_step(ambient_agents, env, cycle))

        if not self.cfg.use_llm:
            self.last_decision_source = "rule"
            self._apply_decisions(self._mock.decide_step(event_agents, env, cycle))
            return

        if not sc.event_triggered_decisions:  # 舊行為：每步決策全部事件車
            if event_agents:
                self._summarize_memory(event_agents)
            decisions = self._llm.decide_step(event_agents, self._llm_environment(env), cycle)
            if decisions and self._llm.last_call_ok:
                self.last_decision_source = "llm"
            else:
                decisions = self._mock.decide_step(event_agents, env, cycle)
                self.last_decision_source = "rule"
            self._apply_decisions(decisions)
            self._record_decision_log(event_agents, decisions, cycle)
            return

        # 事件觸發：只決策觸發的事件車（順暢的車維持現有 mode）
        self.last_decision_source = "llm"
        triggered = self._triggered_agents(cycle, event_agents)
        if not triggered:
            return  # 沒人觸發 → 不呼叫 LLM
        self._summarize_memory(triggered)   # 重決前先把記憶 summary 用 LLM 重寫一次
        bsize = self._budget_batch_size(triggered, sc.batch_size)  # 依 token 預算動態壓低批量
        n_batches = math.ceil(len(triggered) / bsize)
        logger.info("step %d · LLM 重決 %d 台（壅塞觸發）→ %d 批 ×%d 並行（batch≤%d）",
                    cycle, len(triggered), n_batches, min(sc.concurrency, n_batches), bsize)
        decisions = self._llm_decide_batched(triggered, self._llm_environment(env), cycle, bsize)
        self._apply_decisions(decisions)
        self._record_decision_log(triggered, decisions, cycle)

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

    def _record_decision_log(self, targeted: list[VehicleAgent],
                             decisions: dict[str, Any], cycle: int) -> None:
        """記錄本步決策日誌（走 WS 取代讀 txt 檔）+ 解析健康度（fallback 數＝解析出問題的訊號）。

        `targeted` 是本步被決策的車；`decisions` 是回傳的 {agent_id: StepDecision}。
        有拿到 active_mode 的算「成功」、其餘算 fallback（維持現 mode）。日誌上限 50 筆控前端 payload。
        """
        log: list[dict[str, Any]] = []
        decided = 0
        for a in targeted:
            d = decisions.get(a.agent_id)
            if d is not None and getattr(d, "active_mode", ""):
                decided += 1
                a.last_decision_cycle = cycle
                if len(log) < 50:
                    log.append({"name": a.profile_name or a.agent_id,
                                "mode": a.active_mode, "reason": a.decision_reason})
        self._decision_log = log
        self._decision_health = {
            "triggered": len(targeted), "decided": decided,
            "fallback": len(targeted) - decided, "source": self.last_decision_source,
        }

    def _triggered_agents(self, cycle: int, agents: list[VehicleAgent]) -> list[VehicleAgent]:
        """回傳本步「需要重決」的 agent：壅塞訊號上升緣 + 過了 cooldown（只在傳入的事件車中找）。"""
        sc = config.SCALING_CONFIG
        thr = self.cfg.crowded_road_threshold
        out: list[VehicleAgent] = []
        for a in agents:
            if a.waiting_for_origin:        # 尚未進場的車不重決
                continue
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

    def _budget_batch_size(self, agents: list[VehicleAgent], hard_cap: int) -> int:
        """依 [llm_budget] token 預算反推「安全批量」：保證每批 decision prompt 不超過 max_model_len。

        每批輸入 ≈ prompt_overhead + batch×(每 agent status+persona tokens)。
        可用於 agent 的 token = max_model_len − reserve_output − prompt_overhead。
        per-agent token 由「實際 status JSON + persona JSON 字元數 ÷ chars_per_token」估得（取樣前幾個）。
        回傳 min(hard_cap, 預算可容納量)，至少 1。
        """
        b = config.LLM_BUDGET
        if not agents:
            return hard_cap
        max_len = config.effective_max_model_len()  # runtime 選模型可覆寫
        avail = max_len - b.reserve_output_tokens - b.prompt_overhead_tokens
        if avail <= 0:
            return 1
        import json as _json
        from ..decisions import profile_pool
        pool = profile_pool.load_pool()
        persona_chars = (sum(len(_json.dumps(p, ensure_ascii=False)) for p in pool) / len(pool)) if pool else 320.0
        sample = agents[: min(len(agents), 5)]
        status_chars = sum(len(_json.dumps(a.build_api_payload(), ensure_ascii=False)) for a in sample) / len(sample)
        per_agent_tok = max(1.0, (persona_chars + status_chars) / b.chars_per_token)
        fit = int(avail // per_agent_tok)
        return max(1, min(hard_cap, fit))

    def _llm_decide_batched(self, agents: list[VehicleAgent], env: dict[str, Any],
                            cycle: int, batch_size: int | None = None) -> dict[str, Any]:
        """把觸發的 agent 分批、並行送 LLM（同步等齊再回傳合併決策）。

        batch_size 預設由 [llm_budget] token 預算決定（呼叫端算好傳入）；未給則退回 [scaling].batch_size。
        """
        sc = config.SCALING_CONFIG
        bsize = batch_size or self._budget_batch_size(agents, sc.batch_size)
        batches = [agents[i:i + bsize] for i in range(0, len(agents), bsize)]
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
            new_path = self._route(agent.current_node, agent.destination_node, agent.routing_strategy())
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

        # 抵達判定：到達目的地節點（路徑走完）即視為抵達——目的地 point 與最近路網節點之間
        # 有固定偏移，無法以對 point 的直線距離歸零，故以「抵達 target node」為準
        # （對齊 GAML 的 goto target node）。或直線距離已在門檻內。背景車用自己的目的地。
        dx, dy = self._dest_xy(agent)
        dist = math.hypot(agent.x - dx, agent.y - dy)
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
                # 累積整趟每條路的尖峰壅塞（供「路網層」Top-N 瓶頸路段分析；含背景車）
                rec = self._road_peak.get(road.road_id)
                if rec is None or road.congestion_proxy > rec["peak_proxy"]:
                    self._road_peak[road.road_id] = {
                        "road_id": road.road_id,
                        "name": road.road_name or road.road_id,
                        "peak_proxy": round(road.congestion_proxy, 4),
                        "peak_flow": int(road.current_flow),
                        "capacity": round(road.capacity, 1),
                    }

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
        agent.current_town = self._current_town(agent)
        dx, dy = self._dest_xy(agent)
        agent.distance_to_destination = math.hypot(agent.x - dx, agent.y - dy)
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

    def _build_nearby_grid(self) -> None:
        """③ 每步建一次空間網格（桶＝邊長 perception_radius 的方格，只裝未抵達的車），
        讓 grid 模式的鄰近估計變 O(1)。整步 O(n) 取代舊的 O(n²)。"""
        cell = max(1.0, self.cfg.perception_radius_m)
        grid: dict[tuple[int, int], int] = {}
        for a in self.agents:
            if a.route_status == RouteStatus.ARRIVED or a.waiting_for_origin:
                continue
            key = (int(a.x // cell), int(a.y // cell))
            grid[key] = grid.get(key, 0) + 1
        self._nearby_grid = grid
        self._nearby_cell = cell

    def _count_nearby(self, agent: VehicleAgent) -> int:
        """附近車數。grid（預設、O(1)）：查自己＋周圍 8 格的車數（近似半徑）；
        exact：精確半徑全比對（O(n)，整步 O(n²)，可還原舊值供對照）。只餵 LLM 感知。"""
        if self.cfg.nearby_mode == "exact":
            r2 = agent.perception_radius ** 2
            count = 0
            for other in self.agents:
                if other is agent or other.route_status == RouteStatus.ARRIVED or other.waiting_for_origin:
                    continue
                if (other.x - agent.x) ** 2 + (other.y - agent.y) ** 2 <= r2:
                    count += 1
            return count
        grid = self._nearby_grid
        if not grid:
            return 0   # 尚未建網格（例如 init 放置階段）；首步會正確重算
        cell = self._nearby_cell
        cx, cy = int(agent.x // cell), int(agent.y // cell)
        total = sum(grid.get((cx + dx, cy + dy), 0) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        if agent.route_status != RouteStatus.ARRIVED:
            total -= 1   # 扣掉自己（自己也在某格裡）
        return max(0, total)

    def _current_town(self, agent: VehicleAgent) -> str:
        """⑦ agent 目前所屬行政區。
        - town_mode="node"（預設、O(1)）：用所在節點 `current_node` 的所屬區（反向索引查表）；
          查不到（極少：節點不在任何區）才退回精確判定。
        - town_mode="exact"：用精確內插位置做點在多邊形內（O(車數×區數)，可還原舊值）。
        """
        if self.cfg.town_mode == "node":
            t = self._node_town.get(agent.current_node)
            if t:
                return t
        return self._town_of_point(agent.x, agent.y)

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
        env["destination_town"] = self._dest_town
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

    def _visible_agents(self) -> list[VehicleAgent]:
        """⑥ 決定本步要送前端的車（大規模渲染）：
        - 車數 ≤ [ui].render_individual_max → 全送（小 demo 任何 zoom 都看得到車）。
        - 超過 且（尚未收到視圖 或 zoom < agent_min_zoom）→ 不送車（前端只看道路壅塞）。
        - 超過 且 zoom 夠近 → 只送「可視範圍內」的車（公尺框過濾，O(n)；經緯度只算這批）。"""
        ui = config.UI_CONFIG
        active = [a for a in self.agents if not a.waiting_for_origin]   # 尚未進場的車不顯示
        if len(self.agents) <= ui.render_individual_max:
            return active
        v = self._view
        if v is None or v.get("zoom", 0.0) < ui.agent_min_zoom:
            return []
        return [a for a in active
                if v["minx"] <= a.x <= v["maxx"] and v["miny"] <= a.y <= v["maxy"]]

    def _snapshot(self, cycle: int, env: dict[str, Any],
                  mode_dist: dict[str, int], status_dist: dict[str, int]) -> SimulationState:
        assert self.network is not None
        agents_snap = []
        for a in self._visible_agents():
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
                trip_summary=a.memory.get("summary", ""),
                summary_source=a.summary_source,
                decision_reason=a.decision_reason,
                role=a.role,
                last_decision_cycle=a.last_decision_cycle,
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
                "ambient_count": self._ambient_count,
                "event_count": len(self.agents) - self._ambient_count,
                "history": self.recorder.history,
            },
            mode_distribution=mode_dist, status_distribution=status_dist,
            decisions=self._decision_log, decision_health=self._decision_health,
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

    def set_view(self, zoom: float, bounds: dict) -> None:
        """⑥ 前端回報目前可視範圍（zoom + lat/lng bounds {s,w,n,e}）；大規模時據此只送範圍內的車。
        把 lat/lng 框轉成公尺框存起來（agent 用公尺座標過濾,免每台算經緯度）。"""
        try:
            from pyproj import Transformer
            if self._to_metric is None:
                self._to_metric = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
            x1, y1 = self._to_metric.transform(float(bounds["w"]), float(bounds["s"]))
            x2, y2 = self._to_metric.transform(float(bounds["e"]), float(bounds["n"]))
            self._view = {"zoom": float(zoom),
                          "minx": min(x1, x2), "maxx": max(x1, x2),
                          "miny": min(y1, y2), "maxy": max(y1, y2)}
        except Exception as e:  # noqa: BLE001
            logger.warning("set_view 失敗：%s", e)

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
            "scenario": {
                "active": scenarios._ACTIVE_KEY,
                "name": scenarios.active().name,
                "list": scenarios.all_summaries(),
                "center": [scenarios.active().center_lat, scenarios.active().center_lng],
                "zoom": scenarios.active().zoom,
            },
            "stadium": {"lat": self._stadium_latlng[0], "lng": self._stadium_latlng[1]},
            "agent_profiles": self._load_agent_profiles(),
            "config": {
                "max_steps": self.cfg.max_steps,
                "step_minutes": self.cfg.step_minutes,
                "nb_agents": self.cfg.nb_agents,
                "decision_source": self.last_decision_source,
                "decision_cores": core_registry.summaries(),
                "current_core": "llm" if self.cfg.use_llm else "rule",
                "ambient": {
                    "enabled": config.AMBIENT_CONFIG.enabled,
                    "count": config.effective_ambient_count(),
                    "active": self._ambient_count,
                    "max": config.AMBIENT_CONFIG.max_count,
                },
                "ui": config.UI_CONFIG.to_payload(),
                "llm": self._llm_init_info(),
            },
        }

    def _route(self, origin: str, dest: str, strategy: dict) -> list[str]:
        """統一的路徑規劃入口：套用 NL 介入的避讓區（_avoid_circles）。"""
        return routing.find_path(self.network, origin, dest, strategy, seed=self.cfg.seed,
                                 avoid_circles=self._avoid_circles or None)

    # ==================================================================
    # NL 介入（受限動作集：避讓區 / 需求突增）
    # ==================================================================
    def apply_intervention(self, action: str, town: str = "", count: int = 0) -> str:
        """套用一個受限介入動作；回傳人類可讀的結果摘要。"""
        if action == "avoid_area":
            t = self._town_by_name(town)
            if t is None or t.centroid_metric is None:
                return f"找不到區域「{town}」，未套用。"
            self._avoid_circles.append((t.centroid_metric.x, t.centroid_metric.y, 2500.0))
            rerouted = 0
            for a in self.agents:
                if a.route_status == RouteStatus.MOVING and a.current_node and a.destination_node:
                    p = self._route(a.current_node, a.destination_node, a.routing_strategy())
                    if len(p) > 1:
                        a.current_path, a.path_index, a.edge_progress = p, 0, 0.0
                        rerouted += 1
            logger.info("介入：避開 %s，%d 台重算路徑", town, rerouted)
            return f"已設定避開「{town}」一帶，{rerouted} 台車重新規劃路線。"
        if action == "demand_surge":
            n = max(1, min(int(count or 0), 1000))
            base = len(self.agents)
            new: list[VehicleAgent] = []
            for i in range(n):
                ag = VehicleAgent.from_config(f"surge_{base + i + 1:04d}", self.cfg)
                ag.origin_town = town or (self._available_towns[self.rng.randrange(len(self._available_towns))]
                                          if self._available_towns else self.cfg.default_origin_town)
                self._place_agent(ag)
                new.append(ag)
            self.agents.extend(new)
            logger.info("介入：新增 %d 台車（來自 %s）", n, town or "各區")
            return f"已新增 {n} 台車{('（來自' + town + '）') if town else ''}，加入模擬。"
        return f"未識別的介入動作：{action}"

    def clear_interventions(self) -> str:
        """清除所有避讓區並讓移動中的車重新規劃（新增的車不移除）。"""
        if not self._avoid_circles:
            return "目前沒有作用中的避讓區。"
        self._avoid_circles = []
        for a in self.agents:
            if a.route_status == RouteStatus.MOVING and a.current_node and a.destination_node:
                p = self._route(a.current_node, a.destination_node, a.routing_strategy())
                if len(p) > 1:
                    a.current_path, a.path_index, a.edge_progress = p, 0, 0.0
        return "已清除避讓區，車輛恢復正常規劃。"

    def snapshot_now(self) -> SimulationState:
        """不前進、直接產生當前狀態快照（介入後即時更新前端用）。"""
        cyc = self.scheduler.cycle
        env = self._environment_summary(cyc)
        mode_dist, status_dist = metrics.distributions(self._event_agents())
        return self._snapshot(cyc, env, mode_dist, status_dist)

    def chat_context(self) -> str:
        """把當前模擬狀態組成精簡文字，供「暫停對話查詢」的 LLM 回答（唯讀）。"""
        cyc = self.scheduler.cycle
        env = self._llm_environment(self._environment_summary(cyc))
        event = self._event_agents()
        _, status = metrics.distributions(event)
        hotspots = env.get("congestion_hotspots") or []
        hs = "、".join(f"{h['town']}（{h['level']}，{h['crowded_roads']}條壅塞）" for h in hotspots) or "無"
        waiting = sum(1 for a in event if a.waiting_at_signal)
        return "\n".join([
            f"模擬時間：第 {cyc} 步（約 {cyc * self.cfg.step_minutes} 分）",
            f"事件車 {len(event)}：已抵達 {status.get('arrived', 0)}，移動中 {status.get('moving', 0)}",
            f"背景常態車流：{self._ambient_count} 台（規則式核心，造成路網基礎負載）",
            f"整體交通（含背景車）：{env.get('overall_traffic') or '未知'}；壅塞趨勢：{env.get('congestion_trend')}",
            f"壅塞熱點：{hs}",
            f"目前事件車等紅燈：{waiting} 車",
            f"事件目的地：{self._dest_town}（{scenarios.active().name}）",
        ])

    def build_analysis(self) -> dict[str, Any]:
        """模擬後的交通分析資料（chart-ready）。供前端「分析」面板。空歷史回最小結構。

        兩層（像交通局做大型活動交評）：
        - **事件層**（只算事件車）：抵達曲線 / 旅行時間 / OD vs 重力期望 / 號誌停等。
        - **路網層**（事件車＋背景車全部）：總車流量隨時間（事件/背景）、服務水準 LOS、
          Top-N 瓶頸路段、事件車佔路網負載比（邊際負載）。詳見 docs/AMBIENT_zh-TW.md。
        """
        from collections import Counter
        hist = self.recorder.history
        cycles = [h["cycle"] for h in hist]
        cum_arrived = [h.get("arrived", 0) for h in hist]
        # 每步新抵達數（cumulative 的差分）= 抵達率
        rate = [cum_arrived[0] if cum_arrived else 0]
        for i in range(1, len(cum_arrived)):
            rate.append(max(0, cum_arrived[i] - cum_arrived[i - 1]))
        sm = self.cfg.step_minutes
        event = self._event_agents()
        # 出發曲線（分批出發）：每個週期有幾台事件車進場（departure_cycle=0 計入第一個週期）
        first_cycle = cycles[0] if cycles else 1
        dep_counter = Counter(max(a.departure_cycle, first_cycle) for a in event)
        departures = [dep_counter.get(c, 0) for c in cycles]
        travel_min = [a.arrival_cycle * sm for a in event if a.arrival_cycle is not None]
        od_actual = Counter(a.origin_town for a in event if a.origin_town).most_common(12)
        od_expected = demand_mod.expected_distribution(self.towns, self._stadium_xy,
                                                       config.DEMAND_CONFIG, 12)
        total = len(event)
        arrived = len(travel_min)
        return {
            "cycles": cycles,
            "elapsed_minutes": [h["elapsed_minutes"] for h in hist],
            "cumulative_arrived": cum_arrived,
            "arrival_rate": rate,
            "departures": departures,
            "avg_congestion": [h.get("average_congestion_proxy", 0) for h in hist],
            "crowded_road_count": [h.get("crowded_road_count", 0) for h in hist],
            "signal_waiting": [h.get("signal_waiting", 0) for h in hist],
            "travel_time_minutes": travel_min,
            "od_actual": od_actual,
            "od_expected_share": od_expected,
            "summary": {
                "total_agents": total,
                "arrived": arrived,
                "arrival_pct": round(100.0 * arrived / total, 1) if total else 0.0,
                "avg_travel_min": round(sum(travel_min) / arrived, 1) if arrived else 0.0,
                "total_signal_stops": sum(h.get("signal_waiting", 0) for h in hist),
            },
            "network": self._network_analysis(hist),
        }

    @staticmethod
    def _los_grade(proxy: float) -> str:
        """壅塞 proxy → 服務水準 LOS 等級（A 最順、F 壅塞；運輸界常用 V/C 對應的概念粗映射）。"""
        for thr, g in ((0.2, "A"), (0.4, "B"), (0.6, "C"), (0.75, "D"), (0.9, "E")):
            if proxy < thr:
                return g
        return "F"

    def _network_analysis(self, hist: list[dict[str, Any]]) -> dict[str, Any]:
        """路網層交通評估（事件車＋背景車全部）：總量、LOS、瓶頸、事件邊際負載。"""
        vol_event = [h.get("event_on_network", 0) for h in hist]
        vol_ambient = [h.get("ambient_on_network", 0) for h in hist]
        congs = [h.get("average_congestion_proxy", 0.0) for h in hist]
        mean_c = round(sum(congs) / len(congs), 4) if congs else 0.0
        peak_c = round(max(congs), 4) if congs else 0.0
        # Top-N 瓶頸路段（整趟尖峰壅塞，含背景車負載）
        rows = sorted(self._road_peak.values(), key=lambda r: r["peak_proxy"], reverse=True)[:10]
        bottlenecks = [{
            "name": r["name"],
            "peak_proxy": r["peak_proxy"],
            "los": self._los_grade(r["peak_proxy"]),
            "peak_flow": r["peak_flow"],
            "capacity": r["capacity"],
            "vc": round(r["peak_flow"] / r["capacity"], 2) if r["capacity"] else 0.0,
        } for r in rows]
        tot_veh = self._event_vehsteps + self._ambient_vehsteps
        return {
            "ambient_count": self._ambient_count,
            "volume_event": vol_event,
            "volume_ambient": vol_ambient,
            "los": {"mean_congestion": mean_c, "peak_congestion": peak_c,
                    "mean_grade": self._los_grade(mean_c), "peak_grade": self._los_grade(peak_c)},
            "bottlenecks": bottlenecks,
            "event_load_share": round(100.0 * self._event_vehsteps / tot_veh, 1) if tot_veh else 0.0,
            "ambient_load_share": round(100.0 * self._ambient_vehsteps / tot_veh, 1) if tot_veh else 0.0,
        }

    def _llm_init_info(self) -> dict[str, Any]:
        """給前端模型選擇器的初始資料：目前後端/模型 + vLLM 候選登錄表。"""
        try:
            from llm_server import llm_config, model_registry
            return {
                "backend": llm_config.LLM_BACKEND,
                "current_model": llm_config.current_model(),
                "vllm_models": model_registry.VLLM_MODELS,
            }
        except ImportError:
            return {"backend": "ollama", "current_model": "", "vllm_models": []}

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
