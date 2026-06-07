"""agent.py — 車輛 agent 資料模型。

欄位與行為對齊 GAML ``species vehicle skills:[moving]``：
identity / active_mode 偏好 / 旅程狀態 / 感知狀態 / API & memory。

設計取捨：本類別只持有「狀態」與「狀態轉換」（套用 active_mode、記錄 memory、
組 payload），不直接依賴 networkx。實際的路徑規劃與移動由 ``simulation.engine``
搭配 ``spatial`` 驅動，使 domain 層維持可純單元測試。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import MemoryConfig, PerceptionContextConfig, SimulationConfig
from .events import RouteStatus

# ---------------------------------------------------------------------------
# 旅次記憶的「質性標籤」設計常數（與 prompt 語意綁定，不從 TOML 調整；
# 調整的是 MemoryConfig 的數值門檻）。詳見 docs/MEMORY_zh-TW.md。
# ---------------------------------------------------------------------------
FEEL_SMOOTH = "順暢"
FEEL_NORMAL = "普通"
FEEL_CONGESTED = "壅塞"

MOVED_FORWARD = "前進中"
MOVED_SLOW = "緩慢"
MOVED_STALLED = "停滯"

SMOOTH_GOOD = "順暢"
SMOOTH_MID = "中等"
SMOOTH_ROUGH = "不順"

# trip_summary 句型：整趟順暢度 → 一句敘述
_SMOOTHNESS_PHRASE = {
    SMOOTH_GOOD: "一路大致順暢",
    SMOOTH_MID: "整趟走走停停",
    SMOOTH_ROUGH: "整趟偏壅塞",
}

# 環境感知標籤（送 LLM 的當下環境；與 prompt 語意綁定，門檻在 PerceptionContextConfig）
SPEED_FREE = "自由流"
SPEED_SLOW = "略慢"
SPEED_JAM = "壅塞緩行"
SPEED_ARRIVED = "已抵達"
AHEAD_CLEAR = "前方順暢"


def _traffic_feel(proxy: float, is_crowded: bool, cfg: MemoryConfig) -> str:
    """當步壅塞感：congestion_proxy（或 is_crowded 旗標）→ 順暢/普通/壅塞。"""
    if proxy >= cfg.feel_congested_proxy or is_crowded:
        return FEEL_CONGESTED
    if proxy >= cfg.feel_normal_proxy:
        return FEEL_NORMAL
    return FEEL_SMOOTH


def _moved_label(dist_moved_m: float, cfg: MemoryConfig) -> str:
    """當步移動感：每步前進公尺 → 停滯/緩慢/前進中。"""
    if dist_moved_m < cfg.moved_stalled_m:
        return MOVED_STALLED
    if dist_moved_m < cfg.moved_slow_m:
        return MOVED_SLOW
    return MOVED_FORWARD


def _smoothness_label(avg_proxy: float, cfg: MemoryConfig) -> str:
    """整趟順暢度：整趟平均 congestion_proxy → 順暢/中等/不順。"""
    if avg_proxy >= cfg.smoothness_rough_proxy:
        return SMOOTH_ROUGH
    if avg_proxy >= cfg.smoothness_mid_proxy:
        return SMOOTH_MID
    return SMOOTH_GOOD


def _where_label(town: str, road_name: str, road_id: str) -> str:
    """地點到「行政區・路名」粒度；路名為空逐階退到行政區 / road_id。"""
    if town and road_name:
        return f"{town}・{road_name}"
    if town:
        return town
    return road_name or road_id or "未知位置"


def _km_label(distance_m: float, decimals: int) -> str:
    return f"約 {distance_m / 1000:.{decimals}f} 公里"


def speed_status_label(speed_kmh: float, limit_kmh: float, cfg: PerceptionContextConfig) -> str:
    """速度感：speed/速限比 → 自由流/略慢/壅塞緩行（給 LLM 判斷是否在爬）。"""
    if limit_kmh <= 0:
        return SPEED_FREE
    ratio = speed_kmh / limit_kmh
    if ratio >= cfg.speed_free_ratio:
        return SPEED_FREE
    if ratio >= cfg.speed_slow_ratio:
        return SPEED_SLOW
    return SPEED_JAM


def road_ahead_label(distance_m: float, road_name: str) -> str:
    """前方壅塞點描述（distance_m＝距離該壅塞段的公尺）。"""
    where = road_name or "前方路段"
    return f"前方約 {distance_m / 1000:.1f} 公里後壅塞（{where}）"


def clean_highway(highway: str) -> str:
    """OSM highway tag 可能是 list 字串表示，取第一個關鍵字。"""
    return highway.split(",")[0].strip().strip("[]'\" ") if highway else ""


@dataclass
class VehicleAgent:
    """單一車輛 agent。"""

    # === identity（GAML: agent_id / profile_agent_name）===
    agent_id: str
    profile_name: str = ""

    # === 旅程起訖（GAML: origin_town / destination_town / vehicle_type）===
    origin_town: str = ""
    destination_town: str = ""
    vehicle_type: str = "汽車"

    # === active_mode（GAML: mode_name + 一組移動偏好權重）===
    active_mode: str = "fast"
    desired_speed: float = 40.0          # km/h
    speed_car_preference: float = 45.0
    speed_moto_preference: float = 35.0
    road_type_preference: list[str] = field(default_factory=list)
    route_randomness: float = 0.15
    comfort_weight: float = 0.20
    time_weight: float = 0.45
    distance_weight: float = 0.25
    capacity_weight: float = 0.10
    # active_mode 路徑策略旗標（由 ACTIVE_MODE_PROFILES 套用；預設關閉＝原最短路徑行為）
    congestion_penalty: float = 0.0
    avoid_threshold: float = 1.0
    road_class_bias: float = 0.0
    recompute_on_crowded: bool = True
    custom_params: dict[str, Any] = field(default_factory=dict)

    # === 旅程狀態（GAML: route_status / waiting_for_origin / next_road_id）===
    route_status: RouteStatus = RouteStatus.CREATED
    waiting_for_origin: bool = False
    next_road_id: str = "calculating"

    # === 路網位置（公尺座標 EPSG:3826）===
    x: float = 0.0
    y: float = 0.0
    current_node: str | None = None
    destination_node: str | None = None
    current_path: list[str] = field(default_factory=list)
    path_index: int = 0
    edge_progress: float = 0.0          # 已在「目前這條邊」上前進的公尺（支援跨步在長邊上推進）
    current_road_id: str = ""
    current_road_name: str = ""          # 由 engine 從 Road.road_name 帶入（OSM NAME，可能為空）
    current_road_class: str = ""         # 由 engine 從 Road.highway 帶入並清理（primary/secondary/...）
    current_town: str = ""

    # === 感知與移動狀態（GAML: speed / perception_radius / is_crowded / ...）===
    speed_kmh: float = 40.0
    perception_radius: float = 300.0
    is_crowded: bool = False
    distance_moved_last_step: float = 0.0
    distance_to_destination: float = 0.0
    nearby_agent_count: int = 0
    congestion_proxy: float = 0.0
    selected_action: str = "none"
    decision_reason: str = ""            # LLM/mock 選擇此 active_mode 的原因（前端顯示）

    # === 送 LLM 的環境感知質性標籤（由 engine 每步算好填入；詳見 docs/ENVIRONMENT_zh-TW.md）===
    traffic_here: str = ""               # 腳下壅塞感（順暢/普通/壅塞）
    speed_status: str = ""               # 速度感（自由流/略慢/壅塞緩行）
    road_ahead: str = ""                 # 前方路況（沿路徑往前看固定距離）

    # === API & memory（旅次記憶：STM 上一步 / LTM 壓縮印象）===
    # short_term_memory：只保留「上一步」的人類化印象（每步覆蓋）。
    # long_term_memory ：整趟壓縮成一段印象 + 少量聚合量（每步由累積器確定性重算）。
    # 兩者都固定大小，取代舊的無上限成長 travel_memory list。詳見 docs/MEMORY_zh-TW.md。
    short_term_memory: dict[str, Any] = field(default_factory=dict)
    long_term_memory: dict[str, Any] = field(default_factory=dict)
    summary_source: str = "template"     # trip_summary 來源："template"（模板）或 "llm"（gemma 摘要）
    api_status: str = "not_sent"
    warning_message: str = ""

    # --- LTM 滾動累積器（內部狀態，不外送 LLM）---
    _start_cycle: int | None = field(default=None, repr=False)
    _prev_mode: str = field(default="", repr=False)
    _prev_distance: float | None = field(default=None, repr=False)
    _mode_switch_count: int = field(default=0, repr=False)
    _congested_spots: list[str] = field(default_factory=list, repr=False)
    _smoothness_sum: float = field(default=0.0, repr=False)
    _smoothness_n: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    # 工廠：以 config 預設值建立
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, agent_id: str, cfg: SimulationConfig) -> "VehicleAgent":
        return cls(
            agent_id=agent_id,
            vehicle_type=cfg.default_vehicle_type,
            destination_town=cfg.destination_town_name,
            desired_speed=cfg.default_desired_speed_kmh,
            speed_car_preference=cfg.default_speed_car_kmh,
            speed_moto_preference=cfg.default_speed_moto_kmh,
            road_type_preference=list(cfg.default_road_type_preference),
            route_randomness=cfg.default_route_randomness,
            comfort_weight=cfg.default_comfort_weight,
            time_weight=cfg.default_time_weight,
            distance_weight=cfg.default_distance_weight,
            capacity_weight=cfg.default_capacity_weight,
            perception_radius=cfg.perception_radius_m,
            speed_kmh=cfg.default_desired_speed_kmh,
        )

    # ------------------------------------------------------------------
    # active_mode 套用（鏡像 GAML apply_active_mode）
    # ------------------------------------------------------------------
    def apply_active_mode(self, payload: dict[str, Any] | str | None) -> None:
        """從決策回應套用 active_mode；缺少的欄位保留原值。

        接受兩種形式：
        - 字串：視為 mode 名稱（例如 "fast"）→ 套用 ACTIVE_MODE_PROFILES 對應的數值與路徑策略。
        - dict：含 mode_name / move_speed / 各權重等（GAML active_mode map）。先依名稱套用 profile
          當基準，再讓 dict 內明確給的數值欄位覆寫（LLM 可細調）。
        """
        if payload is None:
            return
        if isinstance(payload, str):
            self.active_mode = payload or self.active_mode
            self._apply_named_profile(self.active_mode)
            return

        if "mode_name" in payload:
            self.active_mode = str(payload["mode_name"])
            self._apply_named_profile(self.active_mode)
        elif "mode" in payload:
            self.active_mode = str(payload["mode"])
            self._apply_named_profile(self.active_mode)

        _set = self._maybe_set_float
        _set(payload, "move_speed", "desired_speed")
        _set(payload, "speed_car", "speed_car_preference")
        _set(payload, "speed_moto", "speed_moto_preference")
        _set(payload, "route_randomness", "route_randomness")
        _set(payload, "comfort_weight", "comfort_weight")
        _set(payload, "time_weight", "time_weight")
        _set(payload, "distance_weight", "distance_weight")
        _set(payload, "capacity_weight", "capacity_weight")
        if "custom_params" in payload and isinstance(payload["custom_params"], dict):
            self.custom_params = dict(payload["custom_params"])

    def _maybe_set_float(self, payload: dict[str, Any], key: str, attr: str) -> None:
        if key in payload:
            try:
                setattr(self, attr, float(payload[key]))
            except (TypeError, ValueError):
                pass

    def _apply_named_profile(self, mode_name: str) -> None:
        """依 mode 名稱套用 ACTIVE_MODE_PROFILES 的數值與路徑策略；查無此名則不動。"""
        from ..config import ACTIVE_MODE_PROFILES

        prof = ACTIVE_MODE_PROFILES.get(mode_name)
        if prof is None:
            return
        self.desired_speed = prof.desired_speed_kmh
        self.time_weight = prof.time_weight
        self.distance_weight = prof.distance_weight
        self.comfort_weight = prof.comfort_weight
        self.capacity_weight = prof.capacity_weight
        self.congestion_penalty = prof.congestion_penalty
        self.avoid_threshold = prof.avoid_threshold
        self.road_class_bias = prof.road_class_bias
        self.recompute_on_crowded = prof.recompute_on_crowded
        self.route_randomness = prof.route_randomness

    def apply_vehicle_type(self, requested: str) -> None:
        """套用車種；只允許「汽車」/「機車」（鏡像 GAML normalize_vehicle_type）。"""
        if not requested:
            return
        if "機車" in requested:
            self.vehicle_type = "機車"
        elif "汽車" in requested:
            self.vehicle_type = "汽車"

    # ------------------------------------------------------------------
    # 權重輸出（給 routing 用）
    # ------------------------------------------------------------------
    def routing_weights(self) -> dict[str, float]:
        """回傳給路徑規劃使用的權重（time/distance/comfort/capacity）。"""
        return {
            "time": self.time_weight,
            "distance": self.distance_weight,
            "comfort": self.comfort_weight,
            "capacity": self.capacity_weight,
        }

    def routing_strategy(self) -> dict[str, Any]:
        """回傳 find_path 用的完整策略：四個權重 + active_mode 路徑策略旗標 + 分散用 salt。"""
        return {
            **self.routing_weights(),
            "congestion_penalty": self.congestion_penalty,
            "avoid_threshold": self.avoid_threshold,
            "road_class_bias": self.road_class_bias,
            "randomness": self.route_randomness,
            "salt": self.agent_id,
        }

    # ------------------------------------------------------------------
    # payload（對齊 GAML build_api_agent_payload / build_active_mode_payload）
    # ------------------------------------------------------------------
    def build_active_mode_payload(self) -> dict[str, Any]:
        return {
            "mode_name": self.active_mode,
            "move_speed": self.desired_speed,
            "speed_car": self.speed_car_preference,
            "speed_moto": self.speed_moto_preference,
            "road_type_preference": list(self.road_type_preference),
            "route_randomness": self.route_randomness,
            "comfort_weight": self.comfort_weight,
            "time_weight": self.time_weight,
            "distance_weight": self.distance_weight,
            "capacity_weight": self.capacity_weight,
            "custom_params": dict(self.custom_params),
        }

    def build_environment_payload(self) -> dict[str, Any]:
        """agent 當下局部環境（質性、給 LLM 判斷用）。

        欄位由 engine 每步算好填入（traffic_here / speed_status / road_ahead）；
        此處只組裝、不含裸 congestion_proxy。詳見 docs/ENVIRONMENT_zh-TW.md。
        """
        return {
            "current_town": self.current_town,
            "current_road": self._current_road_label(),
            "route_status": str(self.route_status),
            "traffic_here": self.traffic_here,
            "speed_status": self.speed_status,
            "road_ahead": self.road_ahead,
            "nearby_agent_count": self.nearby_agent_count,
            "distance_to_destination_m": round(self.distance_to_destination),
        }

    def _current_road_label(self) -> str:
        """目前道路 → 「路名（等級）」；路名空逐階退到等級 / road_id。"""
        name, cls = self.current_road_name, self.current_road_class
        if name and cls:
            return f"{name}（{cls}）"
        if name:
            return name
        if cls:
            return f"（{cls}）"
        return self.current_road_id or "—"

    def build_api_payload(self) -> dict[str, Any]:
        """對齊 GAML build_api_agent_payload（每 step 送給 /from-gama）。"""
        return {
            "agent_id": self.agent_id,
            "agent_name": self.profile_name,
            "origin_town": self.origin_town,
            "destination_town": self.destination_town,
            "active_mode": self.active_mode,
            "vehicle_type": self.vehicle_type,
            "environment": self.build_environment_payload(),
            "short_term_memory": self.short_term_memory,
            "long_term_memory": self.long_term_memory,
        }

    # ------------------------------------------------------------------
    # 旅次記憶更新（取代 GAML build_memory_entry + append）
    # ------------------------------------------------------------------
    def update_memory(self, cycle: int, step_minutes: int, cfg: MemoryConfig) -> None:
        """以「上一步」覆蓋 STM、以累積器確定性重算 LTM（每步呼叫一次）。

        模擬人類旅次記憶：上一刻記得清楚（STM），整趟只留一段模糊印象（LTM）。
        全程確定性（不呼叫 LLM、不含隨機），維持同 seed 同軌跡。
        """
        feel = _traffic_feel(self.congestion_proxy, self.is_crowded, cfg)
        where = _where_label(self.current_town, self.current_road_name, self.current_road_id)
        closer = (
            self._prev_distance is not None
            and self.distance_to_destination < self._prev_distance - 1e-6
        )
        arrived = self.route_status == RouteStatus.ARRIVED

        # --- STM：上一步的人類化印象（每步覆蓋）---
        self.short_term_memory = {
            "step": cycle,
            "where": where,
            "traffic_feel": feel,
            "mode_used": self.active_mode,
            "moved": _moved_label(self.distance_moved_last_step, cfg),
            "getting_closer": bool(closer),
            "remaining": _km_label(self.distance_to_destination, cfg.distance_decimals),
        }

        # --- 滾動更新累積器 ---
        if self._start_cycle is None:
            self._start_cycle = cycle
        if self._prev_mode and self.active_mode != self._prev_mode:
            self._mode_switch_count += 1
        if feel == FEEL_CONGESTED and where not in self._congested_spots \
                and len(self._congested_spots) < cfg.congested_spots_max:
            self._congested_spots.append(where)
        self._smoothness_sum += self.congestion_proxy
        self._smoothness_n += 1
        self._prev_mode = self.active_mode
        self._prev_distance = self.distance_to_destination

        # --- LTM：把整趟壓縮成一段印象 + 少量聚合量（固定大小）---
        elapsed_steps = cycle - self._start_cycle + 1
        avg_proxy = self._smoothness_sum / max(self._smoothness_n, 1)
        self.long_term_memory = {
            "trip_summary": self._compose_summary(elapsed_steps, step_minutes, avg_proxy, closer, arrived, cfg),
            "elapsed": f"約 {elapsed_steps * step_minutes} 分鐘（{elapsed_steps} 步）",
            "congested_spots": list(self._congested_spots),
            "mode_switches": self._mode_switch_count,
            "overall_smoothness": _smoothness_label(avg_proxy, cfg),
        }
        self.summary_source = "template"   # 預設模板；engine 開啟 LLM 摘要時會覆寫此句與來源

    def memory_facts(self) -> dict[str, Any]:
        """給 LLM 摘要器的結構化事實（只給事實，不含模板那句）。"""
        ltm = self.long_term_memory
        return {
            "agent_id": self.agent_id,
            "origin_town": self.origin_town,
            "destination_town": self.destination_town,
            "active_mode": self.active_mode,
            "route_status": str(self.route_status),
            "overall_smoothness": ltm.get("overall_smoothness", ""),
            "congested_spots": ltm.get("congested_spots", []),
            "mode_switches": ltm.get("mode_switches", 0),
            "elapsed": ltm.get("elapsed", ""),
            "traffic_here": self.traffic_here,
            "road_ahead": self.road_ahead,
        }

    def _compose_summary(
        self, elapsed_steps: int, step_minutes: int, avg_proxy: float,
        closer: bool, arrived: bool, cfg: MemoryConfig,
    ) -> str:
        """以累積器確定性拼出一段繁體中文旅次摘要（方式 A：模板生成）。"""
        parts = [f"從{self.origin_town or '出發地'}出發前往{self.destination_town or '目的地'}"]
        parts.append(_SMOOTHNESS_PHRASE[_smoothness_label(avg_proxy, cfg)])
        if self._congested_spots:
            parts.append("曾在" + "、".join(self._congested_spots) + "一帶遇到壅塞")
        if self._mode_switch_count > 0:
            parts.append(f"中途換了 {self._mode_switch_count} 次策略，目前採「{self.active_mode}」")
        if arrived:
            parts.append("目前已抵達目的地")
        else:
            parts.append("正在接近目的地" if closer else "目前進度較緩")
        parts.append(f"已行進約 {elapsed_steps * step_minutes} 分鐘")
        return "；".join(parts) + "。"
