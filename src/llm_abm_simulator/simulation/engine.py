"""engine.py — 交通 ABM 模擬引擎（取代 GAMA 模擬主迴圈）。

擁有完整模擬狀態，對齊 GAML 主模型的 init 與每 cycle reflex 行為：

    每 step：套用決策 → 感知（速限/壅塞/鄰近）→ 移動（壅塞時重算路徑）→
             重算道路 flow/congestion/weight → 指標/分佈 → 記錄 memory → 快照。

決策來源透過 DecisionPolicy 抽象（mock 預設，可切 LLM）；LLM 不可用時自動 fallback
到 mock，不會 crash。同一個 seed 兩次執行產生相同軌跡。

說明（與 GAML 的差異）：GAML 以 `is_crowded = nearby>0` 觸發 recompute_path，
本實作改以「道路 congestion_proxy ≥ crowded_road_threshold」觸發，語意更貼近「避開壅塞」
且大幅減少不必要的最短路徑重算，讓互動式 demo 維持流暢。
"""

from __future__ import annotations

import logging
import math
import time
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
from .profiling import StepProfiler
from .random_seed import make_rng
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

# 跨連線/重設的「節點→行政區索引」快取（鍵＝graphml 路徑+mtime）。索引唯讀、確定性，
# 與車數/seed 無關 → 共用不改結果；只留最新場景一份。詳見 _build_town_node_index。
_SPATIAL_INDEX_CACHE: dict[tuple[str, float], tuple[dict[str, list[str]], dict[str, str]]] = {}

# 監測器放置：一律「吸附到最近的道路」（像街景丟人，總會落在最近街道上）。
# 只有當最近道路都超過此距離（等於丟到外海/深山、附近根本沒路）才拒絕。
_DETECTOR_SNAP_M = 1500.0


# ---------------------------------------------------------------------------
# init 路由並行（multiprocessing）：spawn-safe（worker 函式放模組頂層、圖以 initializer
# 在每個 worker 載一次）。Linux 用 fork（圖 copy-on-write、最快）；Windows 用 spawn（各載一次）。
# 不改結果：init 時各路段 congestion=0，find_path 為純函式（jitter 用 crc32，跨進程一致）。
# ---------------------------------------------------------------------------
_ROUTE_WORKER_NET = None     # 每個 worker 程序各自持有的 RoadNetwork（initializer 載一次）
_ROUTE_WORKER_SEED = 0


def _init_route_worker(graphml_path: str, seed: int) -> None:
    global _ROUTE_WORKER_NET, _ROUTE_WORKER_SEED
    from pathlib import Path
    from ..spatial import road_network
    _ROUTE_WORKER_NET = road_network._from_graphml(Path(graphml_path))
    _ROUTE_WORKER_SEED = seed


def _route_worker(task: tuple[str, str, dict]) -> list[str]:
    from ..spatial import routing
    origin, dest, strategy = task
    return routing.find_path(_ROUTE_WORKER_NET, origin, dest, strategy,
                             seed=_ROUTE_WORKER_SEED, avoid_circles=None)


def _parallel_init_routes(tasks: list[tuple[str, str, dict]], graphml_path: str,
                          seed: int, workers: int) -> list[list[str]]:
    import multiprocessing as mp
    ctx = mp.get_context()   # 預設：Linux=fork、Windows=spawn
    chunk = max(1, len(tasks) // (workers * 4))
    with ctx.Pool(processes=workers, initializer=_init_route_worker,
                  initargs=(graphml_path, seed)) as pool:
        # pool.map 保留輸入順序 → 結果可依 index 對回 agent（確定性）
        return pool.map(_route_worker, tasks, chunksize=chunk)


def congestion_color(proxy: float) -> str:
    if proxy < 0.3:
        return "#00C853"
    if proxy < 0.7:
        return "#FFD600"
    if proxy < 0.9:
        return "#FF6D00"
    return "#D50000"


def _dedupe_provenance(provenance: list[dict], cap: int = 6) -> list[dict]:
    """合併本步各批的 RAG provenance：依 (source, idx) 去重、保留 rrf 較高者，依 rrf 排序取前 cap。"""
    best: dict[tuple, dict] = {}
    for h in provenance or []:
        key = (h.get("source"), h.get("idx"))
        if key not in best or h.get("rrf", 0) > best[key].get("rrf", 0):
            best[key] = h
    return sorted(best.values(), key=lambda h: h.get("rrf", 0), reverse=True)[:cap]


class SimulationEngine:
    """單一模擬執行個體（每個 WebSocket 連線一個）。"""

    def __init__(self, cfg: SimulationConfig | None = None) -> None:
        self.cfg = cfg or config.DEFAULT_CONFIG
        self.rng = make_rng(self.cfg.seed)
        self.scheduler = Scheduler(self.cfg.max_steps, self.cfg.step_minutes)
        self.recorder = metrics.MetricsRecorder(self.cfg)
        self._profiler = StepProfiler(config.SCALING_CONFIG.profile_steps)   # 每步分段計時（opt-in）

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
        self._egress_declared_cycle: int | None = None   # 散場宣告的週期（None＝尚在進場/停留階段）
        self._persona_residence: dict[str, str] = {}     # persona name → 居住區（散場 destination=residence 用）

        # 背景常態車流（ambient）與「路網層」交通評估累積器
        self._ambient_count: int = 0
        self._road_peak: dict[str, dict[str, Any]] = {}   # road_id → {name, peak_proxy, peak_flow}
        self._event_vehsteps: int = 0                     # 事件車「車·步」累積（路網負載占比用）
        self._ambient_vehsteps: int = 0                   # 背景車「車·步」累積

        # 規模化：節點→行政區索引、鄰近空間網格、前端可視範圍
        self._town_nodes: dict[str, list[str]] = {}       # 區名 → 覆蓋它的節點清單（init 一次性建，放置 O(1)）
        self._node_town: dict[str, str] = {}              # 節點 → 所屬區（同一索引反向；current_town O(1)）
        self._town_rep_cache: dict[str, str | None] = {}  # 區名 → 代表節點（終點樹路由：背景車終點收斂到此）
        self._dest_pool: dict[str, list[str]] | None = None  # 稀疏終點池：區名→終點節點清單（lazy 建一次；見 _dest_node_in_town）
        self._nearby_grid: dict[tuple[int, int], int] | None = None  # 每步重建的鄰近計數網格
        self._nearby_cell: float = 1.0
        self._view: dict[str, float] | None = None        # 前端回報的可視範圍（公尺框 + zoom）；大規模裁切用
        self._to_metric = None                            # lazy pyproj transformer（set_view 用）
        self._to_wgs = None                               # lazy pyproj transformer（公尺→WGS84，排隊顯示用真實投影）

        # 車流監測器（detectors）：放在路上的被動計數器（不改物理、可重現）。GIS 流量圖層也用同一套計數。
        self._detector_specs: list[dict[str, Any]] = []   # 前端放置、套用設定時帶入的 {lat,lng}
        self._detectors: list[dict[str, Any]] = []        # 已吸附到路段、註冊好的監測器
        self._detector_series: dict[str, list[int]] = {}  # 監測器 id → 每步通過數（時間曲線；總量＝事件+背景）
        self._detector_series_event: dict[str, list[int]] = {}  # 監測器 id → 每步「事件車」通過數（驗證匯出 doc_count 用）
        self._road_volume: dict[str, dict[str, int]] = {}  # 全路網每條有向邊累積通過數（流量圖層用）
        self._agent_prev_road: dict[str, str] = {}        # agent_id → 上一個計過的邊 road_id（連續停同邊去重）
        self._step_entered_edges: dict[str, list[str]] = {}  # 本步每台車「實際走過的邊」road_id 序列（移動時收集，監測器計數用）→ 通過數與 step_minutes 無關
        self._edge_ids: list[str] = []                    # 監測器吸附用：邊 road_id 清單
        self._edge_uv: list[tuple[str, str]] = []         # 對應 (u, v)
        self._edge_xy = None                              # numpy (N,4)：每邊端點公尺座標 ax,ay,bx,by

        # Decision 即時日誌（走 WebSocket 取代讀 txt 檔）：本步重決的車 + 解析健康度
        self._decision_log: list[dict[str, Any]] = []
        self._decision_health: dict[str, Any] = {}
        self._rag_provenance: list[dict[str, Any]] = []   # 本步 RAG 注入來源（多批去重後；前端決策日誌）

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
        self._build_edge_index()        # 監測器吸附用：邊端點 numpy 索引

        self._road_peak = {}
        self._event_vehsteps = 0
        self._ambient_vehsteps = 0
        self._road_volume = {}
        self._agent_prev_road = {}
        self._step_entered_edges = {}
        self._register_detectors()      # 把暫存的監測器點位吸附到路段並註冊（計數歸零）

        self._build_agents()
        self._initial_decisions()
        # 事件車出生地解耦：用重力模型（人口+距離衰減）覆寫出生地；停用/無人口資料則保留既有指派。
        demand_mod.assign_origin_towns(self.agents, self.towns, self._stadium_xy,
                                       self.rng, config.DEMAND_CONFIG)
        # 背景常態車流：用雙邊重力 OD 生成不指定事件終點的常態車流（一律規則式、無記憶）。
        self._build_ambient_agents()
        self._egress_declared_cycle = None          # 散場尚未宣告
        self._build_persona_residence()             # 散場 destination=residence：name→居住區（一次建表）
        self._place_all_agents()    # 放置（依序、保 determinism）+ 路徑運算（可選並行，不改結果）
        self._assign_departures()   # 事件車分批出發（時空需求）；window=0 → 全部 cycle 0 出發

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
        name/車種（不呼叫 LLM 做初始決策、開場不爆量）；初始 action_mode 維持規則式。
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
            if a.action_mode:
                agent.apply_action_mode(a.action_mode)   # 套用 mode 的數值 + 路徑策略
            agent.api_status = "init_response_applied" if self.last_decision_source == "llm" else "rule"

        # LLM 模式（不分事件觸發）：persona 池為「出生地 / name / 車種」的單一真實來源，
        # 確定性覆寫之（初始 action_mode 維持上面的規則式或 LLM init 結果）。
        # 出生地只在此處設一次；之後每步決策不會、也不應再更動出生地（agent 只出生一次）。
        if self.cfg.use_llm:
            from ..decisions import profile_pool
            if profile_pool.assign_to_agents(self.agents, config.PROFILE_CONFIG.pool_size,
                                             self._available_towns, self.cfg.default_origin_town):
                self.last_decision_source = "llm"
                for agent in self.agents:
                    agent.api_status = "persona_assigned"
            else:
                logger.warning("persona 池不可用（vLLM 未啟動？），沿用既有人物")

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

        # 跨連線/重設快取：索引只取決於（圖節點、行政區幾何、球場），與車數/seed 無關 →
        # 同一份 graphml 可重用，省掉數萬節點的 covers 計算（不改結果）。
        cache_key = self._spatial_cache_key()
        if cache_key is not None:
            cached = _SPATIAL_INDEX_CACHE.get(cache_key)
            if cached is not None:
                self._town_nodes, self._node_town = cached
                logger.info("節點→行政區索引命中快取（跳過建表）")
                return

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
        self._dest_pool = None   # 節點索引重建 → 終點池失效，下次 _dest_node_in_town 重建
        if cache_key is not None:
            _SPATIAL_INDEX_CACHE.clear()   # 只留最新場景的索引
            _SPATIAL_INDEX_CACHE[cache_key] = (idx, node_town)
        logger.info("節點→行政區索引完成：%d 節點 / %d 區", len(nodes), len(idx))

    def _spatial_cache_key(self) -> tuple[str, float] | None:
        """節點→行政區索引的快取鍵：bundle graphml 路徑 + mtime（檔案重建即失效）。
        無 bundle 檔（synthetic fallback）或停用快取時回 None（不快取、每次重算）。"""
        if not config.SCALING_CONFIG.cache_network:
            return None
        from .. import scenarios
        path = scenarios.active().road_graphml
        try:
            return (str(path), path.stat().st_mtime)
        except OSError:
            return None

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

    def _dest_node_in_town(self, town_name: str) -> str:
        """**終點專用**（稀疏終點）：從該區的「終點池」抽一個節點，把全市不同終點節點數壓到數百~千個
        → 讓 UXsim DUO route_search 只需對少數終點算（見 simulation/uxsim_sparse_routing.py）。

        池 = 每區 ceil(人口 / [demand].dest_pool_per_capita) 個不重複隨機節點（**建一次、快取**，
        用獨立 rng 取樣以**不擾動主 rng 序列**）。停用（per_capita≤0）或池空 → 退回 `_node_in_town`（區內任一節點）。
        消耗主 rng 與 `_node_in_town` 一致（一次 `randrange`）→ 既有可重現性不變（同 seed→同結果）。
        只用於終點（背景/散場）；起點仍用 `_node_in_town`（保留起點多樣性，且起點不影響 route_search 成本）。"""
        pool = self._dest_pool_for(town_name)
        if pool:
            return pool[self.rng.randrange(len(pool))]
        return self._node_in_town(town_name)

    def _dest_pool_for(self, town_name: str) -> list[str]:
        if config.DEMAND_CONFIG.dest_pool_per_capita <= 0:
            return []                                  # 停用 → 舊行為
        if self._dest_pool is None:
            self._build_dest_pool(config.DEMAND_CONFIG.dest_pool_per_capita)
        return self._dest_pool.get(town_name, [])

    def _build_dest_pool(self, per_capita: int) -> None:
        """為每區建終點池：ceil(人口/per_capita) 個不重複隨機節點（人口缺/0 的區給 1 個）。"""
        import math
        import random
        prng = random.Random((self.cfg.seed or 0) ^ 0x5E5D)  # 獨立 rng，不動 self.rng
        pool: dict[str, list[str]] = {}
        for t in self.towns:
            nodes = self._town_nodes.get(t.town_name) or []
            if not nodes:
                continue
            k = math.ceil(t.population / per_capita) if t.population and t.population > 0 else 1
            pool[t.town_name] = prng.sample(nodes, min(max(1, k), len(nodes)))
        self._dest_pool = pool
        logger.info("稀疏終點池建立：%d 區、共 %d 個終點節點（每 %d 人一個）",
                    len(pool), sum(len(v) for v in pool.values()), per_capita)

    def _place_agent(self, agent: VehicleAgent) -> None:
        """把單一 agent 放到起點節點、算初始路徑（runtime 介入新增車用；init 走 _place_all_agents）。"""
        self._place_agent_setup(agent)
        path = self._route(agent.current_node, agent.destination_node, agent.routing_strategy())
        self._finalize_agent_route(agent, path)

    def _place_agent_setup(self, agent: VehicleAgent) -> None:
        """放置（會抽 rng）：定起點/終點節點、destination_town、phase、散場居住地。

        事件車（role=event）：目的地＝事件地點（球場）。
        背景車（role=ambient）：目的地＝其 destination_town 內的隨機節點（不指定事件終點）。
        把「抽 rng 的放置」與「無 rng 的路徑運算」分開，讓 init 路由可安全並行（不改結果）。
        """
        assert self.network is not None
        town = self._town_by_name(agent.origin_town)
        if town is not None:
            origin_node = self._node_in_town(agent.origin_town)
        else:
            origin_node = self.network.nearest_node(*self._stadium_xy)

        if agent.role == "ambient":
            dtown = self._town_by_name(agent.destination_town)
            dest_node = (self._dest_node_in_town(agent.destination_town)
                         if dtown is not None else self._dest_node)
        else:
            agent.destination_town = self._dest_town
            dest_node = self._dest_node
            agent.phase = "ingress"
            self._assign_home(agent, origin_node)   # 散場目的地（居住地 / 出生地）

        agent.current_node = origin_node
        if agent.role == "event":
            agent.visited_nodes = [origin_node]   # 軌跡起點（整趟路徑視覺化用）
        agent.destination_node = dest_node
        agent.x, agent.y = self.network.node_xy(origin_node)

    def _finalize_agent_route(self, agent: VehicleAgent, path: list[str]) -> None:
        """套用算好的路徑（無 rng）：設定 path/status，並刷新一次感知。"""
        agent.current_path = path
        agent.path_index = 0
        agent.edge_progress = 0.0
        if path and len(path) > 1:
            agent.route_status = RouteStatus.MOVING
        else:
            agent.route_status = RouteStatus.ARRIVED if path else RouteStatus.ERROR
        self._refresh_agent_perception(agent, pre_move=True)

    def _place_all_agents(self) -> None:
        """init 放置所有 agent。放置（抽 rng）一律主程序依序（保 determinism）；
        路徑運算（無 rng、純函式）可選擇用 multiprocessing 並行（[scaling].init_workers）。"""
        for agent in self.agents:
            self._place_agent_setup(agent)
        if self._use_route_trees():
            self._place_routes_via_trees()
            return
        tasks = [(a.current_node, a.destination_node, a.routing_strategy()) for a in self.agents]
        paths = self._compute_init_routes(tasks)
        for agent, path in zip(self.agents, paths):
            self._finalize_agent_route(agent, path)

    def _compute_init_routes(self, tasks: list[tuple[str, str, dict]]) -> list[list[str]]:
        """算 init 路徑清單。預設單程序；init_workers>1 且車數達門檻且有 bundle 路網時改並行。

        並行不改結果：路徑成本是 length/speed/權重/旗標 + crc32 jitter 的純函式（init 時各路段
        congestion 皆為 0），與程序無關；所有 rng 抽取已在主程序 _place_agent_setup 依序完成。
        """
        sc = config.SCALING_CONFIG
        seq = lambda: [self._route(o, d, s) for (o, d, s) in tasks]
        if sc.init_workers <= 1 or len(tasks) < sc.parallel_init_min_agents or self._avoid_circles:
            return seq()
        from .. import scenarios
        graphml = scenarios.active().road_graphml
        if not graphml.exists():        # synthetic fallback 無檔可在 worker 重建 → 單程序
            return seq()
        try:
            return _parallel_init_routes(tasks, str(graphml), self.cfg.seed, sc.init_workers)
        except Exception as e:  # noqa: BLE001  並行失敗一律安全退回單程序
            logger.warning("並行 init 路由失敗（%s），改用單程序", e)
            return seq()

    # ------------------------------------------------------------------
    # 終點樹 init 路由（城市尺度；達門檻才啟用，小規模/測試走原本逐車 find_path）
    # ------------------------------------------------------------------
    def _use_route_trees(self) -> bool:
        """是否用反向終點樹做路由（init + 中途重算）。route_trees=false → 退回逐車 find_path（對照/除錯）。"""
        return config.SCALING_CONFIG.route_trees and self.network is not None

    def _town_rep_node(self, town_name: str) -> str | None:
        """行政區代表節點（形心最近節點；確定性、不抽 rng）。終點樹把背景車終點收斂到此 → 終點數＝區數。"""
        rep = self._town_rep_cache.get(town_name, "__miss__")
        if rep != "__miss__":
            return rep
        t = self._town_by_name(town_name)
        node: str | None = None
        if t is not None and t.centroid_metric is not None and self.network is not None:
            node = self.network.nearest_node(t.centroid_metric.x, t.centroid_metric.y)
        self._town_rep_cache[town_name] = node
        return node

    def _tree_routes(self, agents: list[VehicleAgent], avoid_circles=None) -> dict[str, list[str]]:
        """對一批車用反向終點樹算「current_node→destination_node」路徑（含當前壅塞 + avoid_circles）。

        同 action_mode 共用一張 CSR、每個終點一棵反向樹；惰性只建這批用到的 (mode, 終點)。回 {agent_id: path}。
        樹找得到路 ⟺ 路存在（avoid_circles 是加權非刪邊），故不需 per-car 退回。
        """
        from ..spatial import routing
        groups: dict[tuple, tuple[dict[str, Any], list[VehicleAgent]]] = {}
        for a in agents:
            strategy = a.routing_strategy()
            groups.setdefault(routing.strategy_signature(strategy), (strategy, []))[1].append(a)
        out: dict[str, list[str]] = {}
        for _sig, (strategy, members) in groups.items():
            trees = routing.DestinationTrees(self.network, strategy, self.cfg.seed,
                                             avoid_circles=avoid_circles)
            by_dest: dict[str, list[VehicleAgent]] = {}
            for a in members:
                by_dest.setdefault(a.destination_node, []).append(a)
            for dest, das in by_dest.items():
                paths = trees.paths_to(dest, [a.current_node for a in das])
                for a in das:
                    out[a.agent_id] = paths.get(a.current_node) or []
        return out

    def _place_routes_via_trees(self) -> None:
        """終點樹 init 路由：背景車終點收斂到區代表節點；每車沿樹讀路徑。init 時 congestion=0、無 avoid_circles。"""
        for a in self.agents:
            if a.role == "ambient":
                rep = self._town_rep_node(a.destination_town)
                if rep is not None:
                    a.destination_node = rep
        paths = self._tree_routes(self.agents, avoid_circles=None)
        for a in self.agents:
            self._finalize_agent_route(a, paths.get(a.agent_id) or [])
        logger.info("終點樹 init 路由：%d 台車", len(self.agents))

    def _reroute_via_trees(self, cycle: int) -> None:
        """中途壅塞重算（批次、走樹）：挑「crowded + recompute_on_crowded + 過 reroute cooldown」的車，
        用「當前壅塞 + avoid_circles」權重的反向樹讀新路徑，取代逐車 networkx。事件+背景一視同仁。"""
        sc = config.SCALING_CONFIG
        cool = max(1, round(sc.reroute_cooldown_minutes / max(1, self.cfg.step_minutes)))
        cands = [
            a for a in self.agents
            if not a.waiting_for_origin
            and a.route_status == RouteStatus.MOVING
            and a.is_crowded and a.recompute_on_crowded
            and a.current_node and a.destination_node
            and (cycle - a.last_reroute_cycle) >= cool
        ]
        if not cands:
            return
        with self._profiler.phase("reroute"):
            paths = self._tree_routes(cands, avoid_circles=self._avoid_circles or None)
        for a in cands:
            p = paths.get(a.agent_id) or []
            if len(p) > 1:
                a.current_path, a.path_index, a.edge_progress = p, 0, 0.0
                a.selected_action = "goto_destination_recompute_path"
            a.last_reroute_cycle = cycle
        self._profiler.count("reroute_n", len(cands))

    # ==================================================================
    # 散場（egress）：居住地指派 / 階段推進
    # ==================================================================
    def _build_persona_residence(self) -> None:
        """建 persona name → 居住區 對照（散場 destination="residence" 用）。
        讀 persona 池的 ``identity.residential_location``，正規化到實際行政區；對不到的略過
        （之後改用人口加權後備）。destination="origin" 時不需要、直接略過。"""
        self._persona_residence = {}
        if config.effective_egress().destination != "residence":
            return
        from ..decisions import profile_pool, response_parser
        towns = self._available_towns
        for p in profile_pool.load_pool():
            ident = p.get("identity") or {}
            name = str(ident.get("name") or "").strip()
            res = ident.get("residential_location")
            if not name or res is None:
                continue
            t = response_parser.normalize_town_name(res, towns, "")
            if t:
                self._persona_residence[name] = t

    def _assign_home(self, agent: VehicleAgent, origin_node: str) -> None:
        """設定事件車的散場目的地（home_town / home_node）。
        - destination="origin"：回出生地（來回程，home_node＝出生節點）。
        - destination="residence"：persona 居住地 → 對不到/規則式車則人口加權抽一個居住區。"""
        if config.effective_egress().destination == "origin":
            agent.home_town = agent.origin_town
            agent.home_node = origin_node
            return
        town = self._persona_residence.get(agent.profile_name)
        if not town:
            town = demand_mod.sample_residence(self.towns, self.rng) or agent.origin_town
        agent.home_town = town
        agent.home_node = (self._dest_node_in_town(town)
                           if self._town_by_name(town) is not None else origin_node)

    def declare_egress(self) -> str:
        """宣告散場開始（操作者驅動）：已抵達/停留的事件車將在視窗內依 profile 陸續離場回家。"""
        if self._egress_declared_cycle is not None:
            return "散場已宣告，車輛陸續離場中。"
        self._egress_declared_cycle = self.scheduler.cycle
        dwell = sum(1 for a in self._event_agents() if a.phase == "dwell")
        logger.info("宣告散場 @ cycle %d：%d 台停留中車輛將陸續離場", self.scheduler.cycle, dwell)
        return f"已宣告散場（第 {self.scheduler.cycle} 步）：{dwell} 台已抵達車將陸續離場返家。"

    def _handle_egress(self, cycle: int) -> None:
        """散場排程 + 啟動：宣告後，停留中的事件車在視窗內依 profile 錯開離場、改往家、重算路徑。"""
        if self._egress_declared_cycle is None:
            return
        eg = config.effective_egress()
        window = max(0, round(eg.window_minutes / max(1, self.cfg.step_minutes)))
        declared = self._egress_declared_cycle
        for a in self._event_agents():
            if a.phase != "dwell":
                continue
            if a.egress_cycle is None:                       # 首次：分派錯開的離場週期
                base = max(declared, a.arrival_cycle if a.arrival_cycle is not None else declared)
                if window <= 0:
                    a.egress_cycle = base
                else:
                    u = self.rng.random()
                    if eg.profile == "peak":
                        u = u * u                            # 一窩蜂：集中在最前面
                    elif eg.profile == "gradual":
                        u = 1.0 - (1.0 - u) * (1.0 - u)      # 拖長：偏後段
                    a.egress_cycle = base + int(round(u * window))
            if cycle >= a.egress_cycle and a.home_node:      # 到點 → 開始散場
                path = self._route(a.current_node, a.home_node, a.routing_strategy())
                if len(path) > 1:
                    a.begin_egress_leg(cycle, carry_memory=eg.carry_ingress_memory)
                    a.phase = "egress"
                    a.destination_town = a.home_town
                    a.destination_node = a.home_node
                    a.current_path, a.path_index, a.edge_progress = path, 0, 0.0
                    a.route_status = RouteStatus.MOVING
                else:                                        # 已在家節點/無路 → 視為返家
                    a.phase = "home"
                    a.egress_arrival_cycle = cycle

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
            a.apply_action_mode(self.rng.choice(config.ACTION_MODES))
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
            dnode = (self._dest_node_in_town(dest)
                     if dtown is not None else self._dest_node)
            _t0 = time.perf_counter() if self._profiler.enabled else 0.0
            path = self._route(a.current_node, dnode, a.routing_strategy())
            if self._profiler.enabled:
                self._profiler.add("respawn", time.perf_counter() - _t0)
                self._profiler.count("respawn_n")
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
        dc = config.effective_departure()
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
        self._handle_egress(cycle)             # 散場：宣告後停留車陸續離場回家（改往 home_node）
        prof = self._profiler

        # 1. 感知快照（用上一步遺留的道路壅塞；尚未進場的車跳過）
        with prof.phase("perceive"):
            if self.cfg.nearby_mode == "grid":
                self._build_nearby_grid()
            for agent in self.agents:
                if not agent.waiting_for_origin:
                    self._refresh_agent_perception(agent, pre_move=True)

        # 2. 決策（LLM 或 mock；LLM 失敗 fallback）
        env = self._environment_summary(cycle)
        _t_decide = time.perf_counter()
        with prof.phase("decide"):
            self._apply_step_decisions(env, cycle)
        llm_s = time.perf_counter() - _t_decide

        # 3. 感知速度 + 移動（壅塞時重算路徑）；尚未進場的車不動；背景車抵達即以新 OD 重生
        with prof.phase("move"):
            self._step_entered_edges = {}   # 本步從頭收集每台車走過的邊（_advance_along_path 填）
            # 先算全部車的速度 + is_crowded（reroute 批次需要先知道誰塞）
            for agent in self.agents:
                if not agent.waiting_for_origin:
                    self._perceive_speed(agent)
            # 批次重算（走樹）：route_trees 開時在這裡一次處理所有 crowded 車；_move_agent 不再逐車重算
            if self._use_route_trees():
                self._reroute_via_trees(cycle)
            for agent in self.agents:
                if agent.waiting_for_origin:
                    continue
                self._move_agent(agent)
            self._respawn_arrived_ambient()

        # 4. 重算道路 flow / congestion / weight（含背景車 → 路網層負載 + 瓶頸累積）
        with prof.phase("flow"):
            self._recompute_flows()

        # 5. 移動後感知快照（供 memory / 輸出；尚未進場的車跳過）
        with prof.phase("perceive"):
            if self.cfg.nearby_mode == "grid":
                self._build_nearby_grid()
            for agent in self.agents:
                if not agent.waiting_for_origin:
                    self._refresh_agent_perception(agent, pre_move=False)

        # 5.5 監測器 / 全路網流量累積（被動量測、進入新邊計一次；不改物理）
        with prof.phase("detect"):
            self._update_detectors(cycle)

        # 6. 指標 + 分佈（事件 KPI 只算事件車；路網層壅塞/流量含背景車）
        event_agents = self._event_agents()
        ambient_agents = self._ambient_agents()
        env = self._environment_summary(cycle)
        # 階段推進（只給事件車）：進場抵達→停留(dwell)；散場抵達→返家(home)。週期各記一次。
        for a in event_agents:
            if a.route_status != RouteStatus.ARRIVED:
                continue
            if a.phase == "ingress":
                a.phase = "dwell"
                if a.arrival_cycle is None:
                    a.arrival_cycle = cycle
            elif a.phase == "egress":
                a.phase = "home"
                if a.egress_arrival_cycle is None:
                    a.egress_arrival_cycle = cycle
        env["signal_waiting"] = sum(1 for a in event_agents if a.waiting_at_signal)
        env["event_on_network"] = sum(1 for a in event_agents if a.route_status == RouteStatus.MOVING)
        env["ambient_on_network"] = sum(1 for a in ambient_agents if a.route_status == RouteStatus.MOVING)
        self._event_vehsteps += env["event_on_network"]
        self._ambient_vehsteps += env["ambient_on_network"]
        mode_dist, status_dist = metrics.distributions(event_agents)
        self.recorder.record_cycle(cycle, env, mode_dist, status_dist)
        # 記下本步全市平均壅塞，供「下一步」算 congestion_trend
        self._prev_avg_congestion = env["average_congestion_proxy"]

        # 每步結構化摘要（always-on, INFO）：規模 + LLM 決策健康 + 壅塞，一行可解析、可 grep 做 paper 圖表
        dh = self._decision_health
        logger.info(
            "step=%d t=%dmin on_net=%d(evt=%d,amb=%d) llm=%s/trig%d/dec%d/fb%d llm_s=%.0f congest=%.2f sig_wait=%d",
            cycle, cycle * self.cfg.step_minutes,
            env["event_on_network"] + env["ambient_on_network"],
            env["event_on_network"], env["ambient_on_network"],
            dh.get("source", "?"), dh.get("triggered", 0), dh.get("decided", 0), dh.get("fallback", 0),
            llm_s, env["average_congestion_proxy"], env.get("signal_waiting", 0),
        )

        # 7. memory（只給已進場的事件車；LLM 核心的摘要已於決策時重寫）
        for agent in event_agents:
            if not agent.waiting_for_origin:
                agent.update_memory(cycle, self.cfg.step_minutes, config.MEMORY_CONFIG)

        with prof.phase("snap"):
            result = self._snapshot(cycle, env, mode_dist, status_dist)
        prof.flush(cycle)
        return result

    def _llm_environment(self, env: dict[str, Any]) -> dict[str, Any]:
        """給 LLM 決策用的精簡質性全域環境。

        只留決策相關欄位；展示用統計（agent_count / active_road_count / crowded_road_count /
        average_congestion_proxy 等裸值）繼續給 recorder/前端，不進 LLM。
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
        - 事件車 + LLM 核心 + 事件觸發（預設）：只對「踩到壅塞/前方塞」的事件車重決，分批並行。
        - 事件車 + LLM 核心 + 關閉事件觸發：退回「每步對全部事件車決策」的舊行為。
        記憶 summary 一律用確定性模板（已移除 LLM 重寫；見 docs/MEMORY_zh-TW.md）。
        """
        sc = config.SCALING_CONFIG
        self._decision_log = []     # 本步決策日誌（走 WS 給前端；每步重置）
        self._decision_health = {"triggered": 0, "decided": 0, "fallback": 0, "source": "rule"}
        self._rag_provenance = []   # 本步 RAG 注入來源（每步重置；僅 LLM+多重查詢時有值）
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
            decisions, provenance = self._llm.decide_step_traced(event_agents, self._llm_environment(env), cycle)
            if decisions and self._llm.last_call_ok:
                self.last_decision_source = "llm"
                self._rag_provenance = _dedupe_provenance(provenance)
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
        bsize = self._budget_batch_size(triggered, sc.batch_size)  # 依 token 預算動態壓低批量
        n_batches = math.ceil(len(triggered) / bsize)
        logger.info("step %d · LLM 重決 %d 台（壅塞觸發）→ %d 批 ×%d 並行（batch≤%d）",
                    cycle, len(triggered), n_batches, min(sc.concurrency, n_batches), bsize)
        decisions, provenance = self._llm_decide_batched(triggered, self._llm_environment(env), cycle, bsize)
        self._rag_provenance = _dedupe_provenance(provenance)   # 多批去重（並行安全：在主執行緒合併後處理）
        self._apply_decisions(decisions)
        self._record_decision_log(triggered, decisions, cycle)

    def _apply_decisions(self, decisions: dict[str, Any]) -> None:
        """依 agent_id 順序套用決策（確定性，與批次回來的順序無關）。"""
        for agent in self.agents:
            d = decisions.get(agent.agent_id)
            if d is None:
                continue
            if d.action_mode:
                agent.apply_action_mode(d.action_mode)
            if d.vehicle_type:
                agent.apply_vehicle_type(d.vehicle_type)
            if d.reason:
                agent.decision_reason = d.reason

    def _record_decision_log(self, targeted: list[VehicleAgent],
                             decisions: dict[str, Any], cycle: int) -> None:
        """記錄本步決策日誌（走 WS 取代讀 txt 檔）+ 解析健康度（fallback 數＝解析出問題的訊號）。

        `targeted` 是本步被決策的車；`decisions` 是回傳的 {agent_id: StepDecision}。
        有拿到 action_mode 的算「成功」、其餘算 fallback（維持現 mode）。日誌上限 50 筆控前端 payload。
        """
        log: list[dict[str, Any]] = []
        decided = 0
        for a in targeted:
            d = decisions.get(a.agent_id)
            if d is not None and getattr(d, "action_mode", ""):
                decided += 1
                a.last_decision_cycle = cycle
                if len(log) < 50:
                    log.append({"name": a.profile_name or a.agent_id,
                                "mode": a.action_mode, "reason": a.decision_reason})
        self._decision_log = log
        self._decision_health = {
            "triggered": len(targeted), "decided": decided,
            "fallback": len(targeted) - decided, "source": self.last_decision_source,
        }

    def _triggered_agents(self, cycle: int, agents: list[VehicleAgent]) -> list[VehicleAgent]:
        """回傳本步「需要重決」的 agent：壅塞訊號上升緣 + 過了 cooldown（只在傳入的事件車中找）。"""
        sc = config.SCALING_CONFIG
        cool = max(1, round(sc.cooldown_minutes / max(1, self.cfg.step_minutes)))  # 分鐘→週期
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
                a._decision_cooldown_until = cycle + cool
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
                            cycle: int, batch_size: int | None = None
                            ) -> tuple[dict[str, Any], list[dict]]:
        """把觸發的 agent 分批、並行送 LLM（同步等齊再回傳合併決策 + 各批 RAG provenance）。

        batch_size 預設由 [llm_budget] token 預算決定（呼叫端算好傳入）；未給則退回 [scaling].batch_size。
        provenance 隨各批回傳值收集、在主執行緒合併（並行安全）。
        """
        sc = config.SCALING_CONFIG
        bsize = batch_size or self._budget_batch_size(agents, sc.batch_size)
        batches = [agents[i:i + bsize] for i in range(0, len(agents), bsize)]
        merged: dict[str, Any] = {}
        prov_all: list[dict] = []
        if len(batches) <= 1:
            if batches:
                d, p = self._llm.decide_step_traced(batches[0], env, cycle)
                merged.update(d)
                prov_all.extend(p)
            return merged, prov_all
        # 單一 step 級進度 watchdog：每 interval 秒印「一行」聚合進度（取代每呼叫各自洗版的 heartbeat）。
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = len(batches)
        done = 0
        t_start = time.perf_counter()
        stop = threading.Event()

        def _watch(interval: float = 30.0) -> None:
            while not stop.wait(interval):
                logger.info("step %d · LLM 進行中… 完成 %d/%d 批、已 %.0fs",
                            cycle, done, total, time.perf_counter() - t_start)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            with ThreadPoolExecutor(max_workers=min(sc.concurrency, total)) as ex:
                futures = [ex.submit(self._llm.decide_step_traced, b, env, cycle) for b in batches]
                for f in as_completed(futures):
                    try:
                        d, p = f.result()
                        merged.update(d)
                        prov_all.extend(p)
                    except Exception as e:  # noqa: BLE001  單批失敗 → 該批維持現有 mode
                        logger.warning("批次決策失敗：%s", e)
                    done += 1
        finally:
            stop.set()
            watcher.join(timeout=1)
        return merged, prov_all

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

        # 壅塞 → 重算路徑（避開壅塞）。route_trees 開時已由 _reroute_via_trees 批次處理 → 這裡跳過逐車；
        # 只有 route_trees=false（對照/除錯）才走逐車 networkx 重算。tolerate_congestion 的 recompute_on_crowded=False 不重算。
        if (not config.SCALING_CONFIG.route_trees and agent.is_crowded and agent.recompute_on_crowded
                and agent.current_node and agent.destination_node):
            _prof = self._profiler
            _t0 = time.perf_counter() if _prof.enabled else 0.0
            new_path = self._route(agent.current_node, agent.destination_node, agent.routing_strategy())
            if _prof.enabled:
                _prof.add("reroute", time.perf_counter() - _t0)
                _prof.count("reroute_n")
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
            if road is not None:
                # 記下本步走上的每條邊（含中途穿越的邊）；通過計數據此回放，故大 step 也不漏算。
                self._step_entered_edges.setdefault(agent.agent_id, []).append(road.road_id)
            edge_len = max(road.length if road else 1.0, 1.0)
            left_in_edge = edge_len - agent.edge_progress  # 這條邊還剩多少（支援跨步推進長邊）
            if left_in_edge <= remaining:
                # 走完這條邊，進到下一個節點
                remaining -= left_in_edge
                agent.path_index += 1
                agent.edge_progress = 0.0
                agent.current_node = v
                if agent.role == "event":
                    agent.visited_nodes.append(v)   # 累積整趟實際走過的節點（含重算後路線、進場+散場連續）
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
                road.update_flow(flow, 10.0, 2.0)   # legacy-only fallback/multiplier 常數（UXsim 後端不走此路）
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
        queue_disp = self._queue_layout_positions()   # A：等紅燈車的顯示用排隊座標（只改畫面）
        agents_snap = []
        for a in self._visible_agents():
            qd = queue_disp.get(a.agent_id)
            lat, lng = qd if qd else self._xy_to_latlng(a.x, a.y)
            agents_snap.append(AgentSnapshot(
                agent_id=a.agent_id, profile_name=a.profile_name, lat=lat, lng=lng,
                route_status=str(a.route_status), action_mode=a.action_mode,
                vehicle_type=a.vehicle_type, speed_kmh=round(a.speed_kmh, 2),
                congestion_proxy=round(a.congestion_proxy, 4),
                distance_to_destination=round(a.distance_to_destination, 1),
                nearby_agent_count=a.nearby_agent_count,
                origin_town=a.origin_town, destination_town=a.destination_town,
                current_town=a.current_town, current_road_id=a.current_road_id,
                waiting_at_signal=a.waiting_at_signal,
                selected_action=a.selected_action,
                trip_summary=a.memory.get("summary", ""),
                decision_reason=a.decision_reason,
                role=a.role,
                phase=a.phase,
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
                "arrived_event": sum(1 for a in self.agents if a.role == "event" and a.arrival_cycle is not None),
                "returned_home": sum(1 for a in self.agents if a.role == "event" and a.phase == "home"),
                "egress_declared": self._egress_declared_cycle is not None,
                "history": self.recorder.history,
            },
            mode_distribution=mode_dist, status_distribution=status_dist,
            decisions=self._decision_log, decision_health=self._decision_health,
            rag_provenance=self._rag_provenance,
        )

    def _xy_to_latlng(self, x: float, y: float) -> tuple[float, float]:
        # 用最近節點的 lat/lng 近似（避免每點都做投影轉換）；節點密集，誤差極小。
        node = self.network.nearest_node(x, y) if self.network else None
        if node is None:
            return (0.0, 0.0)
        return self.network.node_latlng(node)

    def _metric_to_latlng(self, x: float, y: float) -> tuple[float, float]:
        """公尺(EPSG:3826) → (lat,lng) 真實投影。排隊顯示需要精確位移，不能用 _xy_to_latlng 的最近節點近似。"""
        from pyproj import Transformer
        if self._to_wgs is None:
            self._to_wgs = Transformer.from_crs(config.CRS_METRIC, config.CRS_WGS84, always_xy=True)
        lng, lat = self._to_wgs.transform(x, y)
        return (lat, lng)

    def _queue_layout_positions(self) -> dict[str, tuple[float, float]]:
        """排隊顯示的「顯示座標」——把停在節點上會疊成一點的車，沿其進場道往上游依車距(~7m)錯開。

        涵蓋所有「停在節點」的車：等紅燈、已抵達、塞在路口都算（edge_progress==0＝人在節點上）。
        移動中的車在邊上各有位置（edge_progress>0）、本就不會疊，故不動。
        只改快照顯示經緯度、不動 agent.x/y 與任何物理量（距離/鄰近/抵達/流量/熱點/偵測器計數皆不受影響）。
        隊首在停止線(節點)、其後每台往上游錯開；超過路段長則封頂在上游端。依 agent_id 排序→確定性。
        底層仍是 point-queue 中觀模型，此處隊長為視覺示意(非物理 spillback)。可由 [ui].queue_render 關閉。
        """
        if not config.UI_CONFIG.queue_render or self.network is None:
            return {}
        SPACING = 7.0   # 公尺：塞車時車距(jam spacing)
        groups: dict[tuple[str, str], list[VehicleAgent]] = {}
        for a in self.agents:
            # 尚未進場 / 還在邊上移動（edge_progress>0，各有位置不疊點）→ 不排
            if a.waiting_for_origin or a.edge_progress > 0.0:
                continue
            path = a.current_path
            i = a.path_index
            if not path or i <= 0 or i >= len(path):   # 需有上游節點(進場方向)
                continue
            groups.setdefault((path[i - 1], path[i]), []).append(a)
        out: dict[str, tuple[float, float]] = {}
        for (u, v), members in groups.items():
            members.sort(key=lambda ag: ag.agent_id)   # 確定性排序
            vx, vy = self.network.node_xy(v)
            ux, uy = self.network.node_xy(u)
            dx, dy = ux - vx, uy - vy          # 由停止線(v)往上游(u)的方向
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                continue
            nx, ny = dx / length, dy / length
            for k, ag in enumerate(members):
                back = min(k * SPACING, max(0.0, length - 1.0))   # 不越過上游節點
                out[ag.agent_id] = self._metric_to_latlng(vx + nx * back, vy + ny * back)
        return out

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
    # ==================================================================
    # 車流監測器（detectors）：放在路上的被動計數器（不改物理、可重現）
    # ==================================================================
    def set_detectors(self, specs: list[dict[str, Any]] | None) -> None:
        """設定待註冊的監測器點位（[{lat,lng}, ...]）；下次 initialize 時吸附到路段。"""
        self._detector_specs = list(specs or [])

    def _build_edge_index(self) -> None:
        """建監測器吸附用的邊端點 numpy 索引（每邊 ax,ay,bx,by，公尺座標）。"""
        import numpy as np
        assert self.network is not None
        ids: list[str] = []
        uv: list[tuple[str, str]] = []
        rows: list[tuple[float, float, float, float]] = []
        for (u, v), road in self.network.roads.items():
            ax, ay = self.network.node_xy(u)
            bx, by = self.network.node_xy(v)
            ids.append(road.road_id)
            uv.append((u, v))
            rows.append((ax, ay, bx, by))
        self._edge_ids = ids
        self._edge_uv = uv
        self._edge_xy = np.array(rows, dtype=float) if rows else np.zeros((0, 4))

    def _snap_to_road(self, x: float, y: float):
        """把點 (x,y 公尺) 吸附到最近的邊；回 (road_id,(u,v),dist_m,(projx,projy))；無邊→None。"""
        import numpy as np
        if self._edge_xy is None or len(self._edge_xy) == 0:
            return None
        ax, ay = self._edge_xy[:, 0], self._edge_xy[:, 1]
        bx, by = self._edge_xy[:, 2], self._edge_xy[:, 3]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        seg2_safe = np.where(seg2 > 0, seg2, 1.0)
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / seg2_safe, 0.0, 1.0)
        t = np.where(seg2 > 0, t, 0.0)   # 退化邊（零長）→ 投影到端點 a
        px, py = ax + t * dx, ay + t * dy
        d = np.hypot(x - px, y - py)
        i = int(np.argmin(d))
        return self._edge_ids[i], self._edge_uv[i], float(d[i]), (float(px[i]), float(py[i]))

    def _register_detectors(self) -> None:
        """把暫存的監測器點位吸附到路段並註冊（計數歸零）；離所有道路 > 門檻則略過。"""
        from pyproj import Transformer
        self._detectors = []
        self._detector_series = {}
        self._detector_series_event = {}
        if not self._detector_specs or self.network is None:
            return
        to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
        to_wgs = Transformer.from_crs(config.CRS_METRIC, config.CRS_WGS84, always_xy=True)
        for i, spec in enumerate(self._detector_specs):
            try:
                lat, lng = float(spec["lat"]), float(spec["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            x, y = to_m.transform(lng, lat)
            snapped = self._snap_to_road(x, y)
            if snapped is None:
                continue
            rid, (u, v), dist, (px, py) = snapped
            if dist > _DETECTOR_SNAP_M:
                logger.info("監測器 #%d 附近無道路（最近 %.0fm），略過", i + 1, dist)
                continue
            road = self.network.road_between(u, v)
            plng, plat = to_wgs.transform(px, py)
            self._detectors.append({
                "id": f"D{len(self._detectors) + 1}",
                "ext_id": spec.get("ext_id"),       # 對應真實相機 UUID（= device_group_id），做對比配對用
                "ext_name": spec.get("ext_name"),   # 真實相機名稱
                "label": (road.road_name if road and road.road_name else rid),
                "lat": round(plat, 6), "lng": round(plng, 6),
                "dir_a": rid, "dir_b": f"{v}_{u}",
                "a": {"ce": 0, "ca": 0, "me": 0, "ma": 0},
                "b": {"ce": 0, "ca": 0, "me": 0, "ma": 0},
            })
        for d in self._detectors:
            self._detector_series[d["id"]] = []
            self._detector_series_event[d["id"]] = []
        if self._detectors:
            logger.info("已註冊 %d 個車流監測器", len(self._detectors))

    @staticmethod
    def _vehicle_leaf(agent: VehicleAgent) -> str:
        """車輛分類葉節點鍵：c/m（汽車/機車）× e/a（事件/背景）。"""
        return ("m" if agent.vehicle_type == "機車" else "c") + ("a" if agent.role == "ambient" else "e")

    def _update_detectors(self, cycle: int) -> None:
        """每步：回放每台車本步「實際走過的邊」累積全路網流量 + 監測器計數。

        被動量測、不改物理、確定性。計的是「通過次數」(throughput)：有通過就計，不是停留才計。
        逐一回放 ``_step_entered_edges`` 內每台車走過的每條邊（含一步跨多條邊時的中途邊），
        每條邊以 ``_agent_prev_road`` 去重（連續停在同一邊只計一次）→ 與 step_minutes 無關，
        大步進跨越路口也不漏算。方向由有向邊 road_id 決定（u→v=dir_a、v→u=dir_b）；
        同一條邊上多台監測器各自計數（不互相覆蓋）。背景車重生再經過再計＝真實流量。
        """
        step_counts = {d["id"]: 0 for d in self._detectors}        # 每步總通過數（事件+背景）
        step_event = {d["id"]: 0 for d in self._detectors}         # 每步「事件車」通過數
        det_a: dict[str, list[dict[str, Any]]] = {}
        det_b: dict[str, list[dict[str, Any]]] = {}
        for d in self._detectors:
            det_a.setdefault(d["dir_a"], []).append(d)
            det_b.setdefault(d["dir_b"], []).append(d)
        for agent in self.agents:
            if agent.waiting_for_origin:
                continue
            edges = self._step_entered_edges.get(agent.agent_id)
            if not edges:
                continue
            leaf = self._vehicle_leaf(agent)
            is_event = leaf.endswith("e")   # leaf 結尾 e＝事件車、a＝背景車
            prev = self._agent_prev_road.get(agent.agent_id)
            for rid in edges:
                if rid == prev:        # 連續停在同一邊（含承接上一步的邊）→ 不重複計
                    continue
                prev = rid
                rv = self._road_volume.get(rid)
                if rv is None:
                    rv = {"ce": 0, "ca": 0, "me": 0, "ma": 0}
                    self._road_volume[rid] = rv
                rv[leaf] += 1
                for d in det_a.get(rid, ()):
                    d["a"][leaf] += 1
                    step_counts[d["id"]] += 1
                    if is_event:
                        step_event[d["id"]] += 1
                for d in det_b.get(rid, ()):
                    d["b"][leaf] += 1
                    step_counts[d["id"]] += 1
                    if is_event:
                        step_event[d["id"]] += 1
            self._agent_prev_road[agent.agent_id] = prev
        for d in self._detectors:
            self._detector_series[d["id"]].append(step_counts[d["id"]])
            self._detector_series_event[d["id"]].append(step_event[d["id"]])

    @staticmethod
    def _expand_counts(c: dict[str, int]) -> dict[str, int]:
        """4 葉計數 → 可選視角（汽車/機車、事件/背景、各小計與總計）。"""
        ce, ca, me, ma = c["ce"], c["ca"], c["me"], c["ma"]
        return {"car_event": ce, "car_ambient": ca, "moto_event": me, "moto_ambient": ma,
                "car": ce + ca, "moto": me + ma, "event": ce + me, "ambient": ca + ma,
                "total": ce + ca + me + ma}

    def _detector_payload(self, d: dict[str, Any]) -> dict[str, Any]:
        both = {k: d["a"][k] + d["b"][k] for k in ("ce", "ca", "me", "ma")}
        return {"id": d["id"], "label": d["label"], "lat": d["lat"], "lng": d["lng"],
                "dir_a": self._expand_counts(d["a"]),
                "dir_b": self._expand_counts(d["b"]),
                "both": self._expand_counts(both),
                "series": list(self._detector_series.get(d["id"], []))}

    def detectors_payload(self) -> list[dict[str, Any]]:
        """給前端：已註冊監測器的位置（畫標記用）。"""
        return [{"id": d["id"], "ext_id": d.get("ext_id"), "ext_name": d.get("ext_name"),
                 "label": d["label"], "lat": d["lat"], "lng": d["lng"]}
                for d in self._detectors]

    def snap_point(self, lat: float, lng: float) -> dict[str, Any]:
        """放置監測器時的即時吸附驗證：把點吸到最近路段；離道路過遠回 ok=False。"""
        from pyproj import Transformer
        if self.network is None:
            return {"ok": False}
        to_m = Transformer.from_crs(config.CRS_WGS84, config.CRS_METRIC, always_xy=True)
        to_wgs = Transformer.from_crs(config.CRS_METRIC, config.CRS_WGS84, always_xy=True)
        x, y = to_m.transform(float(lng), float(lat))
        snapped = self._snap_to_road(x, y)
        if snapped is None:
            return {"ok": False}
        rid, (u, v), dist, (px, py) = snapped
        if dist > _DETECTOR_SNAP_M:
            return {"ok": False, "dist": round(dist, 1)}
        road = self.network.road_between(u, v)
        plng, plat = to_wgs.transform(px, py)
        return {"ok": True, "lat": round(plat, 6), "lng": round(plng, 6),
                "label": (road.road_name if road and road.road_name else rid), "dist": round(dist, 1)}

    # ==================================================================
    # GIS 主題圖層匯出（給交通局 QGIS/ArcGIS 分析）
    # ==================================================================
    def gis_road_records(self) -> list[dict[str, Any]]:
        """每「無向路段」一筆：幾何(WGS84) + LOS/流量/壅塞屬性（欄名 ≤10 字元對齊 DBF）。

        整趟尖峰來自 `_road_peak`（含背景車），累積通過量來自 `_road_volume`（雙向合計）。
        涵蓋全路網（無流量者 peak=0/LOS=A/vol=0），供交通局出完整主題圖。
        """
        from shapely.geometry import LineString
        assert self.network is not None
        recs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for (u, v), road in self.network.roads.items():
            key = tuple(sorted((u, v)))
            if key in seen:
                continue
            seen.add(key)
            rid_a, rid_b = f"{u}_{v}", f"{v}_{u}"
            pa, pb = self._road_peak.get(rid_a, {}), self._road_peak.get(rid_b, {})
            peak = max(pa.get("peak_proxy", 0.0), pb.get("peak_proxy", 0.0))
            pflow = max(pa.get("peak_flow", 0), pb.get("peak_flow", 0))
            va, vb = self._road_volume.get(rid_a, {}), self._road_volume.get(rid_b, {})
            ce = va.get("ce", 0) + vb.get("ce", 0)
            ca = va.get("ca", 0) + vb.get("ca", 0)
            me = va.get("me", 0) + vb.get("me", 0)
            ma = va.get("ma", 0) + vb.get("ma", 0)
            cap = road.capacity
            geom = road.geometry_wgs84
            if geom is None:
                latu, lngu = self.network.node_latlng(u)
                latv, lngv = self.network.node_latlng(v)
                geom = LineString([(lngu, latu), (lngv, latv)])
            recs.append({
                "geometry": geom,
                "road_id": road.road_id, "name": road.road_name, "highway": road.highway,
                "lanes": float(road.lanes), "capacity": round(cap, 1),
                "peak_prox": round(peak, 3),
                "peak_vc": round(pflow / cap, 3) if cap else 0.0,
                "peak_flow": int(pflow), "peak_los": self._los_grade(peak),
                "tot_vol": ce + ca + me + ma, "car_vol": ce + ca, "moto_vol": me + ma,
                "evt_vol": ce + me, "amb_vol": ca + ma,
            })
        return recs

    def gis_detector_records(self) -> list[dict[str, Any]]:
        """每監測器一筆點位 + 各類通過量（欄名 ≤10 字元）。"""
        from shapely.geometry import Point
        recs: list[dict[str, Any]] = []
        for d in self._detectors:
            both = self._expand_counts({k: d["a"][k] + d["b"][k] for k in ("ce", "ca", "me", "ma")})
            recs.append({
                "geometry": Point(d["lng"], d["lat"]),
                "det_id": d["id"], "name": d["label"],
                "tot_vol": both["total"], "car_vol": both["car"], "moto_vol": both["moto"],
                "evt_vol": both["event"], "amb_vol": both["ambient"],
                "dir_a_vol": self._expand_counts(d["a"])["total"],
                "dir_b_vol": self._expand_counts(d["b"])["total"],
            })
        return recs

    def export_gis_layer(self, layer: str, out_dir) -> "Any":
        """建指定主題圖層的 Shapefile 並打包成 zip，回傳 zip 路徑。lazy 匯入 gis_export。"""
        from ..spatial import gis_export
        return gis_export.export_layer_zip(self, layer, out_dir)

    # ==================================================================
    # 驗證 CSV 匯出（給組員 validation 腳本 main.py 直接吃）
    # ==================================================================
    def _validation_run_params(self, case: str, n_bins: int, camera_count: int,
                               steps_per_bin: int) -> list[tuple[str, object]]:
        """蒐集本次模擬的所有關鍵參數（供 paper 標註；寫成 <case>_run_params.csv）。"""
        from datetime import datetime
        dep = config.effective_departure()
        params: list[tuple[str, object]] = [
            ("export_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("case", case),
            ("scenario", scenarios.active().name),
            ("event_cars_nb_agents", self.cfg.nb_agents),
            ("ambient_count", config.effective_ambient_count()),
            ("seed", self.cfg.seed),
            ("step_minutes", self.cfg.step_minutes),
            ("max_steps", self.cfg.max_steps),
            ("cycles_run", self.scheduler.cycle),
            ("steps_per_5min_bin", steps_per_bin),
            ("time_bins_5min", n_bins),
            ("departure_profile", dep.profile),
            ("departure_window_minutes", dep.window_minutes),
            ("use_llm", self.cfg.use_llm),
            ("demand_beta", config.DEMAND_CONFIG.beta),
            ("demand_decay", config.DEMAND_CONFIG.decay),
            ("crowded_road_threshold", self.cfg.crowded_road_threshold),
            ("validation_cameras_exported", camera_count),
        ]
        try:  # LLM 後端/模型（若可取得；取不到不影響匯出）
            from llm_server import llm_config
            params.append(("llm_backend", "vllm"))
            getter = getattr(llm_config, "current_model", None)
            params.append(("llm_model", getter() if callable(getter) else ""))
        except Exception:  # noqa: BLE001
            pass
        return params

    def export_validation_csv(self, case: str, out_dir) -> "Any":
        """把目前這次模擬的偵測器計數，輸出成組員 validation 腳本可直接吃的 CSV（打包成 zip）。

        ``case`` ∈ {"weekend","weekday"}：決定時間視窗起點（weekend 14:00 / weekday 16:30）與檔名。
        zip 內含三檔：
          - ``<case>_gameday.csv``    每相機每 5 分鐘「事件車-only」通過數（= doc_count；對應 observed impact）。
          - ``<case>_nogameday.csv``  同結構、doc_count 全 0（模型在非球賽日無事件車流；供 main.py game−nogame）。
          - ``<case>_run_params.csv`` 本次模擬所有參數（供 paper 標註）。
        欄位對齊 observed report：``camera_name,device_group_id,stream_id,time_start,doc_count,avg_speed``。
        只匯出帶 ext_id（相機 UUID）的偵測器，以 ``device_group_id=UUID`` 與真實相機配對。
        """
        import csv as _csv
        import uuid as _uuid
        import zipfile
        from datetime import datetime, timedelta
        from pathlib import Path
        from tempfile import TemporaryDirectory

        if case not in ("weekend", "weekday"):
            raise ValueError("case 必須為 'weekend' 或 'weekday'")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 視窗起點時鐘時間（對齊 main.py 寫死的時段；日期沿用 observed 球賽日，main.py 只看小時）
        window_start = (datetime(2026, 3, 29, 14, 0, 0) if case == "weekend"
                        else datetime(2026, 4, 22, 16, 30, 0))

        # 相對步 → 5 分鐘格：每格累加它涵蓋的步（step_minutes=5→1步1格；=1→5步1格）。最多 24 格（2 小時）。
        step_min = max(1, int(self.cfg.step_minutes))
        steps_per_bin = max(1, round(5 / step_min))
        n_steps = int(self.scheduler.cycle)
        n_bins = min(24, -(-n_steps // steps_per_bin)) if n_steps else 0

        def binned(series: list[int]) -> list[int]:
            return [int(sum(series[k * steps_per_bin:(k + 1) * steps_per_bin])) for k in range(n_bins)]

        cams = [d for d in self._detectors if d.get("ext_id")]
        fields = ["camera_name", "device_group_id", "stream_id", "time_start", "doc_count", "avg_speed"]

        def build_rows(zero: bool) -> list[dict[str, object]]:
            rows: list[dict[str, object]] = []
            for d in cams:
                ev = binned(self._detector_series_event.get(d["id"], []))
                for k in range(n_bins):
                    ts = window_start + timedelta(minutes=5 * k)
                    rows.append({
                        "camera_name": d.get("ext_name") or d.get("label", ""),
                        "device_group_id": d["ext_id"],
                        "stream_id": "",
                        "time_start": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "doc_count": 0 if zero else ev[k],
                        "avg_speed": "",
                    })
            return rows

        params = self._validation_run_params(case, n_bins, len(cams), steps_per_bin)
        token = _uuid.uuid4().hex[:8]
        zip_path = out_dir / f"validation_{case}_{token}.zip"

        def _write(tdp: Path, name: str, rows: list[dict], fnames: list[str]) -> None:
            with (tdp / name).open("w", encoding="utf-8-sig", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=fnames)
                w.writeheader()
                w.writerows(rows)

        with TemporaryDirectory() as td:
            tdp = Path(td)
            _write(tdp, f"{case}_gameday.csv", build_rows(zero=False), fields)
            _write(tdp, f"{case}_nogameday.csv", build_rows(zero=True), fields)
            _write(tdp, f"{case}_run_params.csv",
                   [{"param": k, "value": v} for k, v in params], ["param", "value"])
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(tdp.glob("*.csv")):
                    zf.write(p, p.name)
        return zip_path

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
            "detectors": self.detectors_payload(),
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
                "departure": {
                    "profile": config.effective_departure().profile,
                    "window_minutes": config.effective_departure().window_minutes,
                },
                "egress": {
                    "profile": config.effective_egress().profile,
                    "window_minutes": config.effective_egress().window_minutes,
                    "destination": config.effective_egress().destination,
                    "carry_ingress_memory": config.effective_egress().carry_ingress_memory,
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

    def get_agent_path(self, agent_id: str) -> dict[str, Any]:
        """回傳某事件車整趟走過的節點軌跡（lat/lng），分進場/散場兩段供前端上色。

        ingress＝進場段；egress＝散場段（與進場共用銜接節點）；無資料回空清單。
        點擊 agent 時才呼叫（不進每步快照），用於檢驗散場路徑是否受進場記憶影響。
        """
        empty = {"agent_id": agent_id, "ingress": [], "egress": []}
        if self.network is None:
            return empty
        agent = next((a for a in self.agents if a.agent_id == agent_id), None)
        if agent is None or not agent.visited_nodes:
            return empty
        latlng: list[list[float]] = []
        for n in agent.visited_nodes:
            try:
                lat, lng = self.network.node_latlng(n)
            except KeyError:
                continue
            latlng.append([lat, lng])
        split = agent.egress_path_split
        if split <= 0 or split >= len(latlng):
            return {"agent_id": agent_id, "ingress": latlng, "egress": []}
        return {"agent_id": agent_id, "ingress": latlng[:split], "egress": latlng[split - 1:]}

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
            "egress": self._egress_analysis(cycles),
            "detectors": [self._detector_payload(d) for d in self._detectors],
        }

    def _egress_analysis(self, cycles: list[int]) -> dict[str, Any]:
        """散場層分析（只在已宣告散場時有資料）：疏散曲線 / 每步離場 / 散場旅時 / 散場 OD / 清場時間。"""
        from collections import Counter
        event = self._event_agents()
        sm = self.cfg.step_minutes
        declared = self._egress_declared_cycle
        if declared is None or not cycles:
            return {"enabled": False}
        home_cycles = [a.egress_arrival_cycle for a in event if a.egress_arrival_cycle is not None]
        cum_home = [sum(1 for c in home_cycles if c <= cyc) for cyc in cycles]
        start_counter = Counter(a.egress_start_cycle for a in event if a.egress_start_cycle is not None)
        departures = [start_counter.get(c, 0) for c in cycles]
        travel = [(a.egress_arrival_cycle - a.egress_start_cycle) * sm for a in event
                  if a.egress_arrival_cycle is not None and a.egress_start_cycle is not None]
        od = Counter(a.home_town for a in event if a.phase in ("egress", "home") and a.home_town).most_common(12)
        reached = sum(1 for a in event if a.arrival_cycle is not None)
        returned = len(home_cycles)
        clearance_min = None
        if reached:
            target = 0.9 * reached
            for cyc, c in zip(cycles, cum_home):
                if c >= target:
                    clearance_min = (cyc - declared) * sm
                    break
        return {
            "enabled": True,
            "declared_cycle": declared,
            "cumulative_home": cum_home,
            "departures": departures,
            "travel_time_minutes": travel,
            "od": od,
            "summary": {
                "reached_stadium": reached,
                "returned_home": returned,
                "return_pct": round(100.0 * returned / reached, 1) if reached else 0.0,
                "avg_egress_travel_min": round(sum(travel) / len(travel), 1) if travel else 0.0,
                "clearance_min": clearance_min,
            },
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
                "backend": "vllm",
                "current_model": llm_config.current_model(),
                "vllm_models": model_registry.VLLM_MODELS,
            }
        except ImportError:
            return {"backend": "vllm", "current_model": "", "vllm_models": []}

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
