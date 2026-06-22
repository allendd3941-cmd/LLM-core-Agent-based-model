"""uxsim_engine.py — 以 UXsim 為物理/路由後端的引擎（facade 子類別）。

設計（見 docs/UXSIM_MIGRATION_zh-TW.md）：``UXsimEngine`` 繼承 ``SimulationEngine``，
**只覆寫「物理 + 路由」**，其餘（towns / agents / 需求 / 決策 / 感知標籤 / 偵測器 / snapshot /
分析 / 匯出 / 介入）全部繼承重用 → 對前端與決策系統的公開介面完全不變（facade 保留）。

覆寫點：
- ``_place_all_agents``：只設起訖節點/座標，**不算 networkx 路徑**（UXsim 負責路由）。
- ``initialize``：在父類完成 setup + 分批出發後，建 UXsim ``World`` 並為每台 agent 建車。
- ``step``：用 ``World.exec_simulation(until_t)`` 推進物理，再把車況讀回 agent/Road 欄位，
  接著重用父類的感知 / 偵測器 / 指標 / snapshot。
- ``_current_road`` / ``_recompute_flows``：改從 UXsim link 讀（取代 current_path 推導）。

**本版範圍**：ingress（事件車→球場）+ 背景車 + rule/DUO 決策，已可本機小網路驗證。
**待 server/LLM 驗證**：全市規模（~9GiB）、LLM 決策（Design 2 路徑注入鉤子已留 ``_inject_routes``）、
散場（``_handle_egress`` 在 UXsim 下需重設車輛目的地，本版先 no-op 並記 warning）。
"""

from __future__ import annotations

import logging
import math
import os

import numpy as np

from .. import config
from ..domain.agent import VehicleAgent
from ..domain.events import RouteStatus
from ..mobility import demand as demand_mod
from ..spatial import uxsim_builder
from .engine import SimulationEngine

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class UXsimEngine(SimulationEngine):
    """UXsim 後端引擎。公開介面與 ``SimulationEngine`` 相同。"""

    def __init__(self, cfg=None) -> None:
        super().__init__(cfg)
        self._world = None
        self._veh: dict[str, object] = {}          # agent_id → uxsim Vehicle
        self._link_by_road: dict[str, object] = {}  # road_id → uxsim Link（壅塞讀回）
        self._roads_by_id: dict[str, object] = {}   # road_id → Road
        self._veh_prog: dict[str, int] = {}          # agent_id → 已走過 link 數（偵測器每步差分）
        self._injected: dict[str, str] = {}          # agent_id → 已注入的偏好類型（避免重複套用）
        self._reroute_cycle: dict[str, int] = {}     # agent_id → 最後一次「中途改道」的 cycle（前端顯示「改道中」）
        self._respawn_count: dict[str, int] = {}     # ambient agent_id → 已重生次數（車名唯一）
        # 全域設定來自 config [uxsim]；deltan / dev_crop 另允許環境變數覆寫（本機開發方便）。
        uc = config.UXSIM_CONFIG
        self._deltan = int(_env_float("UXSIM_DELTAN", uc.deltan))
        self._dev_crop_km = _env_float("UXSIM_DEV_CROP_KM", uc.dev_crop_km)

    # ------------------------------------------------------------------
    # initialize：父類做 setup（含被覆寫的 _place_all_agents）→ 建 World + 車
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        super().initialize()
        assert self.network is not None
        # 本機開發裁切（正式全市時 UXSIM_DEV_CROP_KM=0 → 不裁切）。
        if self._dev_crop_km > 0:
            self.network = uxsim_builder.crop_to_region(
                self.network, self._stadium_latlng, self._dev_crop_km)
            # 裁切後：重算球場節點 + 重建索引/偵測器 + 重新放置（起點/終點都落在裁切後網內）
            self._dest_node = self.network.nearest_node(*self._stadium_xy)
            self._build_town_node_index()
            self._build_edge_index()
            self._register_detectors()
            self._place_all_agents()
        self._roads_by_id = {r.road_id: r for r in self.network.all_roads()}
        self._build_world_and_vehicles()

    def _use_route_trees(self) -> bool:
        return False   # UXsim 負責路由，停用 legacy 終點樹

    def _spatial_cache_key(self):
        # 裁切模式下圖會變但 graphml 路徑/mtime 不變 → 必須停用節點→區索引快取，
        # 否則裁切後會讀到舊的全網索引、起點落在裁切外。全市（不裁切）維持快取。
        if self._dev_crop_km > 0:
            return None
        return super()._spatial_cache_key()

    def _place_all_agents(self) -> None:
        """只放置（抽 rng 設起訖節點/座標），不算路徑。UXsim 建車後負責路由。"""
        for a in self.agents:
            self._place_agent_setup(a)
            a.current_path = [a.current_node]   # 佔位，避免繼承碼存取空 path
            a.path_index = 0
            a.route_status = RouteStatus.MOVING

    def _build_world_and_vehicles(self) -> None:
        assert self.network is not None
        sm = self.cfg.step_minutes
        tmax = int((self.cfg.max_steps + 2) * sm * 60)
        uc = config.UXSIM_CONFIG
        W = uxsim_builder.build_world(
            self.network, tmax=tmax, deltan=self._deltan, seed=self.cfg.seed,
            signals=self.signals, jam_density=uc.jam_density, reaction_time=uc.reaction_time,
            duo_update_time=uc.duo_update_time, duo_update_weight=uc.duo_update_weight,
            duo_noise=uc.duo_noise, route_choice_principle=uc.route_choice_principle,
            route_choice_update_gradual=uc.route_choice_update_gradual,
            instantaneous_TT_timestep_interval=uc.instantaneous_TT_timestep_interval,
            no_cyclic_routing=uc.no_cyclic_routing,
            hard_deterministic_mode=uc.hard_deterministic_mode,
            vehicle_logging_timestep_interval=uc.vehicle_logging_interval,
            reduce_memory_delete_vehicle_route_pref=uc.reduce_memory_route_pref)
        self._link_by_road = {l.name: l for l in W.LINKS}
        # Road.capacity 設為 UXsim 的 jam 儲容（kappa × 長度）= 一條 link 塞死時最多容納車數。
        # 讓 build_analysis / snapshot / GIS 匯出的容量與 congestion_proxy、與物理**同源**，
        # 不再用 [highway_specs].capacity_per_lane（那會讓 LOS 報表跟即時地圖壅塞對不上）。
        for rid, link in self._link_by_road.items():
            road = self._roads_by_id.get(rid)
            if road is not None:
                road.capacity = float(link.kappa) * float(link.length)
        in_net = set(self.network.graph.nodes())
        added = 0
        for a in self.agents:
            o, d = a.current_node, a.destination_node
            if o not in in_net or d not in in_net or o == d:
                continue
            dt = int(a.departure_cycle * sm * 60)
            try:
                self._veh[a.agent_id] = W.addVehicle(str(o), str(d), departure_time=dt,
                                                     name=a.agent_id)
                added += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("addVehicle 失敗 %s: %s", a.agent_id, e)
        self._world = W
        from . import uxsim_sparse_routing
        if config.UXSIM_CONFIG.sparse_route_search:
            uxsim_sparse_routing.enable_sparse_routing()   # DUO route_search 只對實際終點算（搭配稀疏終點池）
        else:
            uxsim_sparse_routing.disable_sparse_routing()  # 還原原生 all-pairs（對拍/baseline）
        logger.info("UXsim 後端就緒：%d/%d 台車已建（deltan=%d）",
                    added, len(self.agents), self._deltan)

    # ------------------------------------------------------------------
    # 物理讀回：UXsim → agent / Road 欄位（讓繼承的感知/snapshot 直接可用）
    # ------------------------------------------------------------------
    @staticmethod
    def _status_from_state(state: str) -> RouteStatus:
        return {
            "home": RouteStatus.CREATED, "wait": RouteStatus.CREATED,
            "run": RouteStatus.MOVING, "end": RouteStatus.ARRIVED,
            "abort": RouteStatus.ERROR,
        }.get(state, RouteStatus.MOVING)

    def _readback(self, cycle: int) -> None:
        """把每台 UXsim 車的狀態讀回對應 agent；並從 link 車數更新 Road flow/congestion。"""
        assert self.network is not None
        # 1) link → Road flow / congestion（供 snapshot 道路上色 + 感知）
        self.network.reset_flows()
        for road_id, link in self._link_by_road.items():
            n = int(getattr(link, "num_vehicles", 0) or 0)
            if n <= 0:
                continue
            road = self._roads_by_id.get(road_id)
            if road is None:
                continue
            # congestion_proxy = 占有率 = 車數 / jam 儲容。儲容 = road.capacity（建構時已設為 kappa×length，
            # 單一來源，與 build_analysis/snapshot/GIS 完全同源；不再用 [highway_specs].capacity_per_lane）。
            storage = road.capacity
            road.current_flow = n
            road.congestion_proxy = round(min(1.0, n / storage), 4) if storage > 0 else 0.0
            rec = self._road_peak.get(road.road_id)   # 累積尖峰（供 build_analysis 瓶頸分析；V/C 用同一儲容）
            if rec is None or road.congestion_proxy > rec["peak_proxy"]:
                self._road_peak[road.road_id] = {
                    "road_id": road.road_id, "name": road.road_name or road.road_id,
                    "peak_proxy": road.congestion_proxy, "peak_flow": n,
                    "capacity": round(storage, 1)}
        # 2) 每台車 → agent 欄位（並用 traveled_route 差分填本步走過的邊，供繼承的 _update_detectors 計數）
        self._step_entered_edges = {}
        for a in self.agents:
            veh = self._veh.get(a.agent_id)
            if veh is None:
                a.waiting_for_origin = True
                continue
            try:
                # 直接讀 UXsim 的 link log（log_t_link 中非字串者即 Link 物件），取名字差分本步新進入的邊。
                # 取代 veh.traveled_route()[0].links——後者每車重建 Route 物件 + 對 W.LINKS 線性搜 get_link
                # （readback 的 93% 成本）。差分語意與舊版完全一致：prog 記未過濾的 Link 序列長度、
                # 輸出才依 name 過濾（與 traveled_route().links 逐元素等價，見 tests/test_readback_edges.py）。
                seq = [e[1] for e in veh.log_t_link if not isinstance(e[1], str)]
                prog = self._veh_prog.get(a.agent_id, 0)
                if len(seq) > prog:
                    self._step_entered_edges[a.agent_id] = [
                        l.name for l in seq[prog:] if getattr(l, "name", None)]
                    self._veh_prog[a.agent_id] = len(seq)
            except Exception:  # noqa: BLE001
                pass
            state = getattr(veh, "state", "wait")
            a.waiting_for_origin = state in ("home", "wait")
            a.route_status = self._status_from_state(state)
            a.speed_kmh = round(float(getattr(veh, "v", 0.0)) * 3.6, 2)
            a.waiting_at_signal = False
            a.distance_moved_last_step = 0.0   # 未在路上→0；在路上時下方用每步位移覆寫（供 memory 的 moved 標籤）
            link = getattr(veh, "link", None)
            if link is not None:
                a.current_road_id = link.name
                a.current_node = getattr(getattr(link, "end_node", None), "name", a.current_node)
                a.current_path = [getattr(getattr(link, "start_node", None), "name", a.current_node),
                                  a.current_node]
                a.path_index = 0
                # 累積整趟走過節點（事件車路徑視覺化 get_agent_path 用；散場段由 begin_egress_leg 標 split）
                if a.role == "event" and a.current_node and (
                        not a.visited_nodes or a.visited_nodes[-1] != a.current_node):
                    a.visited_nodes.append(a.current_node)
                # 啟發式等紅燈：幾乎靜止且正前往一個號誌節點（供前端顯示 / LLM 感知）
                if a.speed_kmh < 1.0 and self.signals.is_signalized(a.current_node):
                    a.waiting_at_signal = True
                try:
                    x, y = veh.get_xy_coords()
                    prev_x, prev_y = a.x, a.y
                    a.x, a.y = float(x), float(y)
                    # 每步歐氏位移（與 legacy _move_agent 同算法）→ memory 的 moved（停滯/緩慢/前進）
                    a.distance_moved_last_step = math.hypot(a.x - prev_x, a.y - prev_y)
                except Exception:  # noqa: BLE001
                    pass
            # selected_action（前端「狀態」列用；與 legacy 同詞彙，前端轉中文）
            if a.route_status == RouteStatus.ARRIVED:
                a.selected_action = "arrived"
            elif a.route_status == RouteStatus.ERROR:
                a.selected_action = "error"
            elif a.waiting_at_signal:
                a.selected_action = "wait_at_signal"
            elif self._reroute_cycle.get(a.agent_id) == cycle:
                a.selected_action = "goto_destination_recompute_path"
            elif link is not None:
                a.selected_action = "goto_destination"
            else:
                a.selected_action = "none"
            if state == "end" and a.arrival_cycle is None and a.role == "event":
                a.arrival_cycle = cycle

    def _current_road(self, agent):
        """改用 agent 目前的 UXsim link 對應的 Road（取代 current_path 推導）。"""
        return self._roads_by_id.get(agent.current_road_id) if agent.current_road_id else None

    def _xy_to_latlng(self, x: float, y: float) -> tuple[float, float]:
        """UXsim 的 a.x/a.y 是 UXsim 由 get_xy_coords 給的『真實連續公尺座標』(CRS_METRIC)
        → 用真實投影顯示，**不吸最近節點**。

        legacy 引擎的車是節點到節點離散跳、x/y 即節點座標，故父類用「吸最近節點」近似又省投影成本；
        但 UXsim 的車連續在路段上跑，吸節點會讓整段路的車全塌到同一個路口節點 → 前端攤成一坨格子
        （不是物理塞爆，是顯示假象）。改真實投影後車沿路散開、忠實反映物理。
        渲染已由 _visible_agents 做視窗裁切（只送可視範圍的車），故每車投影成本可接受。"""
        return self._metric_to_latlng(x, y)

    def _recompute_flows(self) -> None:
        """flow 已在 _readback 從 UXsim link 更新；此處不再用 agent path 統計。"""
        return

    def _refresh_agent_perception(self, agent, pre_move: bool) -> None:
        """繼承感知 + 補設 ``is_crowded``（legacy 在 _perceive_speed 設；UXsim 不跑那段，故在此設，
        否則 _triggered_agents / avoid_congestion 的壅塞觸發會永遠 False）。"""
        super()._refresh_agent_perception(agent, pre_move)
        agent.is_crowded = agent.congestion_proxy >= self.cfg.crowded_road_threshold

    def _road_ahead(self, agent, congested_proxy: float, lookahead_m: float = 0.0) -> str:
        """UXsim：只看「朝終點的下一條 OSM link」的壅塞 → 質性文字（取代 base 的固定距離掃描）。

        下一條 link＝在當前 link 末端節點、UXsim 自己 route_pref（內建最短時間樹）最大的那條出口邊
        （即 route_next_link_choice 的同一邏輯）。無下一段（快到終點/不在路上）→ 順暢。
        比固定距離掃描更貼合 UXsim、也更像真人「看到下一條路塞就改道」。``lookahead_m`` 忽略（簽章相容）。
        **不自算任何路徑**——方向完全取自 UXsim 的 route_pref。
        """
        from ..domain import agent as agent_mod
        if agent.route_status in (RouteStatus.ARRIVED, RouteStatus.ERROR):
            return ""
        if agent.role != "event":          # 背景車不觸發 LLM、不需前方感知
            return agent_mod.AHEAD_CLEAR
        # 直接讀 UXsim 自己選的「下一條」：在當前 link 末端節點，route_pref 最大的出口邊
        # （= UXsim route_next_link_choice 的同一邏輯；route_pref 來自 ROUTECHOICE 的最短時間樹）。
        # 不再用我們的終點樹/Dijkstra——選路一律交給 UXsim。
        veh = self._veh.get(agent.agent_id)
        link = getattr(veh, "link", None) if veh is not None else None
        if link is None:
            return agent_mod.AHEAD_CLEAR
        try:
            outlinks = list(link.end_node.outlinks.values())
            rp = veh.route_pref
            nxt = max(outlinks, key=lambda l: rp[l.id]) if (outlinks and rp is not None) else None
        except Exception:  # noqa: BLE001
            nxt = None
        road = self._roads_by_id.get(getattr(nxt, "name", None)) if nxt is not None else None
        if road is None:
            return agent_mod.AHEAD_CLEAR
        if road.congestion_proxy >= congested_proxy:
            return agent_mod.road_ahead_next_label(road.road_name)
        return agent_mod.AHEAD_CLEAR

    # ------------------------------------------------------------------
    # action_mode → UXsim 路徑選擇參數（只用 UXsim 既有旋鈕，不自寫選路演算法）
    # ------------------------------------------------------------------
    def _inject_routes(self, cycle: int) -> None:
        """把每台事件車的 action_mode 翻成 UXsim 既有的 route-choice 參數（**純 UXsim、不自算路徑**）。

        方向一律由 UXsim 自己的 route_pref（內建最短時間樹）提供；我們只用 UXsim 參數加篩選：
          fast                → homogeneous_DUO，清 prefer/avoid（純最短時間）
          avoid_congestion    → DUO + set_links_avoid(壅塞 link；每節點保底不全避 → 不崩)
          tolerate_congestion → 凍結：route_pref.copy() + principle="fixed"（不再改道、忍受壅塞）
        重算時機：decision 改變（mode 變）即套用；avoid_congestion 另在「壅塞 + 過 cooldown」時重算避開集合。
        背景車不經此（純 DUO）。
        """
        if self._world is None:
            return
        sc = config.SCALING_CONFIG
        sm = max(1, self.cfg.step_minutes)
        avoid_set = None   # 本步共用、用到才算（壅塞 link 名集合，含每節點保底）
        for a in self._event_agents():
            veh = self._veh.get(a.agent_id)
            if veh is None or a.waiting_for_origin:
                continue
            mode = a.action_mode or "fast"
            prev = self._injected.get(a.agent_id)
            first = prev != mode
            recompute = (mode == "avoid_congestion" and a.is_crowded
                         and (cycle - a.last_reroute_cycle) * sm >= sc.reroute_cooldown_minutes)
            if not (first or recompute):
                continue
            if mode == "avoid_congestion" and avoid_set is None:
                avoid_set = self._safe_congested_avoid_set()
            if not self._apply_action_mode(a, veh, mode, avoid_set):
                continue   # tolerate 終點樹未就緒 → 不標記、下步重試（避免凍結到全 0 route_pref）
            if recompute or (first and prev is not None):   # 中途改道（非初始路由）→ 前端「改道中」
                self._reroute_cycle[a.agent_id] = cycle
            self._injected[a.agent_id] = mode
            a.last_reroute_cycle = cycle

    def _apply_action_mode(self, a, veh, mode: str, avoid_set=None) -> bool:
        """把 mode 翻成 UXsim 參數：方向用 UXsim 自己的 route_pref（時間樹），我們只加路型/壅塞篩選或凍結。
        **完全不自算路徑、無 Dijkstra**。回傳是否成功（tolerate 在終點時間樹尚未就緒時回 False → 下步重試）。"""
        try:
            if mode == "tolerate_congestion":
                # 凍結「UXsim 已算好的終點時間樹」+ 設非 DUO principle → 之後不再被 DUO 更新 = 不改道、忍受。
                # 注意：要凍結 ROUTECHOICE 的終點樹（已算好的），不能凍結車自身 route_pref——剛出發時它是全 0，
                # 凍全 0 會讓車每路口亂選、到不了。樹未就緒（如出發前）→ 暫用 DUO、回 False 由 _inject_routes 重試。
                veh.set_links_prefer([])
                veh.set_links_avoid([])
                try:
                    tree = veh.W.ROUTECHOICE.route_pref[veh.dest.id]
                except Exception:  # noqa: BLE001
                    tree = None
                if tree is not None and float(np.sum(tree)) > 0:
                    veh.route_pref = tree.copy() if hasattr(tree, "copy") else dict(tree)
                    veh.route_choice_principle = "fixed"
                    return True
                veh.route_choice_principle = "homogeneous_DUO"
                return False
            # 其餘模式：方向交給 UXsim 時間樹 → principle 設回 DUO，並還原 route_pref 為 UXsim 當前時間樹
            # （從 tolerate 切回來也會復原；讀的是 UXsim 自己算好的樹,我們不算）。
            veh.route_choice_principle = "homogeneous_DUO"
            try:
                veh.route_pref = veh.W.ROUTECHOICE.route_pref[veh.dest.id]
            except Exception:  # noqa: BLE001
                pass
            veh.set_links_avoid([])
            veh.set_links_prefer([])
            if mode == "avoid_congestion":
                veh.set_links_avoid(avoid_set or [])           # 避開壅塞 link（每節點保底→不崩）
            # else：fast / 未知 → 純 DUO（prefer/avoid 已清空）
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("inject %s 失敗 %s: %s", mode, a.agent_id, e)
            return True

    def _safe_avoid_set(self, avoid_rids) -> list:
        """要避開的 link 名，但**保證每個節點至少留一個非避開出口**（出度檢查，非算路徑、無 Dijkstra）
        → UXsim route_next_link_choice 不會「出口全被避→空集合→max() 崩潰」。
        供 avoid_congestion（避壅塞，動態）使用。"""
        if not avoid_rids or self.network is None:
            return []
        G = self.network.graph
        avoid = set(avoid_rids)
        for u in G.nodes():
            out_rids = []
            for v in G.successors(u):
                road = self.network.road_between(u, v)
                if road is not None:
                    out_rids.append(road.road_id)
            if out_rids and all(r in avoid for r in out_rids):   # 此節點出口全在避開集 → 留一個（任一）
                avoid.discard(out_rids[0])
        return list(avoid)

    def _safe_congested_avoid_set(self) -> list:
        """目前壅塞（congestion_proxy ≥ 門檻）的 link → 套每節點保底（見 _safe_avoid_set）。本步共用一次算好。"""
        thr = self.cfg.crowded_road_threshold
        congested = {rid for rid, r in self._roads_by_id.items() if r.congestion_proxy >= thr}
        return self._safe_avoid_set(congested)

    # ------------------------------------------------------------------
    # step：UXsim 推進 + 讀回，重用父類的決策/感知/偵測器/指標/snapshot
    # ------------------------------------------------------------------
    def step(self):
        from . import metrics
        if not self.is_initialized or self._world is None:
            raise RuntimeError("UXsim 引擎尚未初始化")

        cycle = self.scheduler.advance()
        self._elapsed_seconds = cycle * self.cfg.step_minutes * 60.0
        prof = self._profiler   # 每步分段計時（[scaling].profile_steps 開啟才印；關閉近乎零成本）
        self._handle_egress(cycle)

        # 決策（rule：每步對事件車決策；LLM 之後接）。先讀回一次讓決策有最新感知。
        env = self._environment_summary(cycle)
        with prof.phase("decide"):
            self._apply_step_decisions(env, cycle)
            self._inject_routes(cycle)

        # 物理推進到本週期末（UXsim exec_simulation；城市尺度的全節點對 route_search 成本落在 move）
        t_target = int(cycle * self.cfg.step_minutes * 60)
        logger.info("step %d：UXsim 物理推進中…（含 DUO 路徑計算；首次與每 %ds 重算步在城市尺度需數分鐘，屬正常）",
                    cycle, int(config.UXSIM_CONFIG.duo_update_time))
        with prof.phase("move"):
            self._world.exec_simulation(until_t=t_target)
        with prof.phase("flow"):
            self._readback(cycle)
        self._respawn_arrived_ambient_ux(cycle)   # 背景車抵達 → 以重力抽新目的地重生（維持穩態負載）

        # 移動後感知（繼承）
        with prof.phase("perceive"):
            if self.cfg.nearby_mode == "grid":
                self._build_nearby_grid()
            for a in self.agents:
                if not a.waiting_for_origin:
                    self._refresh_agent_perception(a, pre_move=False)

        with prof.phase("detect"):
            self._update_detectors(cycle)

        # 指標 + 階段推進 + 記錄 + snapshot（繼承）
        event_agents = self._event_agents()
        ambient_agents = self._ambient_agents()
        env = self._environment_summary(cycle)
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
        self._prev_avg_congestion = env["average_congestion_proxy"]

        for a in event_agents:
            if not a.waiting_for_origin:
                a.update_memory(cycle, self.cfg.step_minutes, config.MEMORY_CONFIG)

        with prof.phase("snap"):
            snap = self._snapshot(cycle, env, mode_dist, status_dist)
        prof.flush(cycle)   # 印「step N prof: decide=… move=… flow=… perceive=… detect=… snap=… total=…」
        return snap

    def _respawn_arrived_ambient_ux(self, cycle: int) -> None:
        """背景車抵達 → 從目前所在以重力抽新目的地、在 UXsim 重生一台車（維持穩態背景負載）。

        對齊 legacy `_respawn_arrived_ambient` 語意（不 teleport、以重力抽新終點），但啟動改為
        UXsim `addVehicle`（運行中注入）。每次重生用唯一車名，並把該 agent 的車 handle 換成新車。
        """
        if not config.AMBIENT_CONFIG.respawn or self._world is None or self.network is None:
            return
        now = int(cycle * self.cfg.step_minutes * 60)   # readback 後 world 已在此時刻
        in_net = set(self.network.graph.nodes())
        for a in self._ambient_agents():
            veh = self._veh.get(a.agent_id)
            if veh is None or getattr(veh, "state", "") != "end":
                continue
            origin = a.current_node
            dest = demand_mod.sample_dest_town(self.towns, (a.x, a.y), self.rng, config.DEMAND_CONFIG)
            dtown = self._town_by_name(dest) if dest else None
            dnode = self._dest_node_in_town(dest) if dtown is not None else self._dest_node
            if origin not in in_net or dnode not in in_net or origin == dnode:
                continue
            k = self._respawn_count.get(a.agent_id, 0) + 1
            try:
                nv = self._world.addVehicle(str(origin), str(dnode), departure_time=now,
                                            name=f"{a.agent_id}_r{k}")
            except Exception:  # noqa: BLE001
                continue
            self._veh[a.agent_id] = nv
            self._veh_prog[a.agent_id] = 0
            self._respawn_count[a.agent_id] = k
            a.origin_town = a.current_town or a.origin_town
            a.destination_town = dest or a.destination_town
            a.destination_node = dnode
            a.route_status = RouteStatus.MOVING
            a.arrival_cycle = None

    def _handle_egress(self, cycle: int) -> None:
        """散場：宣告後，停留中的事件車依 profile 錯開離場 → 在 UXsim **重生一台 球場→home 的車**。

        排程邏輯（egress_cycle 分派 + profile）與 legacy 相同；差別在「啟動」改成
        `addVehicle`（運行中注入，spike 已驗）並把該 agent 的車 handle 換成新車（偵測器差分歸零、
        路徑偏好重套）。home/origin 不在網內或建車失敗 → 視為已返家。
        """
        if self._egress_declared_cycle is None or self._world is None or self.network is None:
            return
        eg = config.effective_egress()
        sm = self.cfg.step_minutes
        window = max(0, round(eg.window_minutes / max(1, sm)))
        declared = self._egress_declared_cycle
        in_net = set(self.network.graph.nodes())
        now = max(0, int((cycle - 1) * sm * 60))   # 目前 world 時間（上一步 exec 到此）
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
                        u = u * u
                    elif eg.profile == "gradual":
                        u = 1.0 - (1.0 - u) * (1.0 - u)
                    a.egress_cycle = base + int(round(u * window))
            if cycle < a.egress_cycle or not a.home_node:
                continue
            origin = a.destination_node or a.current_node    # 抵達點（球場）
            if a.home_node in in_net and origin in in_net and origin != a.home_node:
                try:
                    veh = self._world.addVehicle(str(origin), str(a.home_node),
                                                 departure_time=now, name=f"{a.agent_id}_eg")
                    self._veh[a.agent_id] = veh
                    self._veh_prog[a.agent_id] = 0
                    self._injected.pop(a.agent_id, None)
                    a.begin_egress_leg(cycle, carry_memory=eg.carry_ingress_memory)
                    a.phase = "egress"
                    a.destination_town = a.home_town
                    a.destination_node = a.home_node
                    a.route_status = RouteStatus.MOVING
                    a.waiting_for_origin = False
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.debug("egress addVehicle 失敗 %s: %s", a.agent_id, e)
            a.phase = "home"                                 # 無法重生 → 視為已返家
            a.egress_arrival_cycle = cycle

    # ------------------------------------------------------------------
    # NL 介入（UXsim 版）：demand_surge 用 addVehicle 注入；avoid_area 記錄但路由生效待 stranding-safe
    # ------------------------------------------------------------------
    def apply_intervention(self, action: str, town: str = "", count: int = 0) -> str:
        if action == "demand_surge":
            if self._world is None or self.network is None:
                return "尚未初始化。"
            n = max(1, min(int(count or 0), 1000))
            base = len(self.agents)
            now = int(self.scheduler.cycle * self.cfg.step_minutes * 60)
            in_net = set(self.network.graph.nodes())
            towns = self._available_towns
            added = 0
            for i in range(n):
                ag = VehicleAgent.from_config(f"surge_{base + i + 1:04d}", self.cfg)
                ag.origin_town = town or (towns[self.rng.randrange(len(towns))]
                                          if towns else self.cfg.default_origin_town)
                self._place_agent_setup(ag)           # 起點/終點(球場)/居住地
                o, d = ag.current_node, ag.destination_node
                if o in in_net and d in in_net and o != d:
                    try:
                        self._veh[ag.agent_id] = self._world.addVehicle(
                            str(o), str(d), departure_time=now, name=ag.agent_id)
                        self.agents.append(ag)
                        added += 1
                    except Exception:  # noqa: BLE001
                        pass
            logger.info("介入：UXsim 新增 %d 台車（來自 %s）", added, town or "各區")
            return f"已新增 {added} 台車{('（來自' + town + '）') if town else ''}，加入模擬。"
        if action == "avoid_area":
            t = self._town_by_name(town)
            if t is None or t.centroid_metric is None:
                return f"找不到區域「{town}」，未套用。"
            self._avoid_circles.append((t.centroid_metric.x, t.centroid_metric.y, 2500.0))
            return f"已記錄避開「{town}」一帶（UXsim 後端避讓路由生效待 stranding-safe 機制）。"
        return f"未識別的介入動作：{action}"

    def clear_interventions(self) -> str:
        if not self._avoid_circles:
            return "目前沒有作用中的避讓區。"
        self._avoid_circles = []
        return "已清除避讓區記錄。"

    def reset(self) -> None:
        super().reset()
        self._world = None
        self._veh = {}
        self._link_by_road = {}
        self._roads_by_id = {}
        self._veh_prog = {}
        self._injected = {}
        self._reroute_cycle = {}
        self._respawn_count = {}
