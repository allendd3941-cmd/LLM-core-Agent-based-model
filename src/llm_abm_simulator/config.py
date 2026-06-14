"""config.py — 模擬器參數的型別化 schema 與載入器。

**可調整參數的唯一真實來源是 ``config/simulation.toml``**（人類可編輯、可寫註解）。
本檔負責：(1) 用 dataclass 定義型別化 schema 與「程式碼預設值」（TOML 缺值時的 fallback）；
(2) 用 Python 內建 ``tomllib`` 載入 TOML 並覆寫成 ``DEFAULT_CONFIG`` / ``UI_CONFIG`` /
``HIGHWAY_SPECS``。TOML 的 key 名稱與此處 dataclass 欄位名一一對應。

數值與語意沿襲原 GAML 交通模型的 ``global`` [可調整參數區]（GAMA 版已移除，此為其行為延續）。
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 專案路徑（src/llm_abm_simulator/config.py → 專案根目錄上溯三層）
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent

DATA_DIR = PROJECT_ROOT / "data"        # 所有資料集中於此（GIS 原始資料 + 衍生路網）
GIS_DIR = DATA_DIR / "gis"              # 原 "GIS data/"，整理後移至 data/gis/
OUTPUT_DIR = PROJECT_ROOT / "output"    # runtime artifact（CSV），已被 .gitignore 忽略
FRONTEND_DIR = PROJECT_ROOT / "simulation_web" / "frontend"
CONFIG_TOML = PROJECT_ROOT / "config" / "simulation.toml"   # 使用者可編輯的參數檔

# GIS 來源檔（對齊 GAML shape_file_* 設定；ROADLINK 在本專案不存在，改由 OSM/synthetic 取代）
TOWN_SHP = GIS_DIR / "TOWN_MOI_1140318_3826.shp"
STADIUM_SHP = GIS_DIR / "亞太棒球場_point.shp"
# 註：舊的「亞太棒球場_研究範圍.shp」已移除——OSM 下載邊界改用 TOWN_MOI 縣界 union（全台南市）。
TOWN_POPULATION_CSV = GIS_DIR / "town_population.csv"   # 各區人口（重力模型需求生成；近似值可替換）
ROAD_GRAPHML = DATA_DIR / "tainan_roads.graphml"   # bundle 的真實 OSM 路網（重現用）

# CRS：TOWN_MOI 與球場 point 皆為 EPSG:3826（TWD97 TM Taiwan，公尺）。
# 距離/空間運算一律使用公尺投影座標；前端地圖使用 WGS84。
CRS_METRIC = "EPSG:3826"
CRS_WGS84 = "EPSG:4326"

# 輸出 CSV（取代 GAMA 的 agent_memory.csv / road_flow.csv）
AGENT_MEMORY_CSV = OUTPUT_DIR / "agent_memory.csv"
ROAD_FLOW_CSV = OUTPUT_DIR / "road_flow.csv"

# 五種 active mode（對齊 prompts/decision_making_prompt.txt 與 LLM 實際輸出）
ACTIVE_MODES = ("fast", "tolerate_congestion", "avoid_congestion", "comfortable", "short_distance")
VEHICLE_TYPES = ("汽車", "機車")

# 台南市 37 個行政區（mock profile 生成用；以 TOWN_MOI 實際載入為準，此處為 fallback）
TAINAN_DISTRICTS = (
    "中西區", "東區", "南區", "北區", "安平區", "安南區",
    "永康區", "歸仁區", "新化區", "左鎮區", "玉井區", "楠西區",
    "南化區", "仁德區", "關廟區", "龍崎區", "官田區", "麻豆區",
    "佳里區", "西港區", "七股區", "將軍區", "學甲區", "北門區",
    "新營區", "後壁區", "白河區", "東山區", "六甲區", "下營區",
    "柳營區", "鹽水區", "善化區", "大內區", "山上區", "新市區", "安定區",
)


@dataclass(frozen=True)
class SimulationConfig:
    """單次模擬的完整參數集合（不可變）。對應 GAML global 變數。"""

    # === 模擬時間（GAML: max_steps / step_minutes）===
    max_steps: int = 36
    step_minutes: int = 5

    # === agent 規模與起訖（GAML: nb_agents / destination_town_name / default_origin_town）===
    nb_agents: int = 10
    destination_town_name: str = "安定區"   # 球場 point 載入失敗時的 fallback 終點
    default_origin_town: str = "東區"        # 解析失敗時的 fallback 生成行政區
    county_name: str = "臺南市"

    # === 感知 / 移動 / 抵達（GAML 同名參數）===
    perception_radius_m: float = 300.0
    arrival_distance_threshold_m: float = 50.0   # GAML 原為 0；0 在離散步進下幾乎無法判定抵達，改用一格容差
    crowded_speed_factor: float = 0.55
    missing_road_speed_cap_kmh: float = 40.0
    crowded_road_threshold: float = 0.5          # congestion_proxy ≥ 此值視為壅塞 / 觸發 recompute
    nearby_mode: str = "grid"                    # 鄰近車數估法："grid"（空間網格近似、O(n)）|"exact"（精確全比對、O(n²)）
    town_mode: str = "node"                      # current_town 估法："node"（所在節點所屬區、O(1)）|"exact"（精確位置、O(車數×區數)）

    # === 預設移動偏好（GAML default_* active_mode）===
    default_vehicle_type: str = "汽車"
    default_desired_speed_kmh: float = 40.0
    default_speed_car_kmh: float = 45.0
    default_speed_moto_kmh: float = 35.0
    default_route_randomness: float = 0.15
    default_comfort_weight: float = 0.20
    default_time_weight: float = 0.45
    default_distance_weight: float = 0.25
    default_capacity_weight: float = 0.10
    default_road_type_preference: tuple[str, ...] = ("primary", "secondary", "tertiary", "residential")

    # === 道路壅塞估計與權重（GAML 同名參數）===
    active_road_min_flow: int = 1
    capacity_fallback_vehicle_count: float = 10.0
    flow_weight_multiplier: float = 2.0
    road_flow_high_threshold: int = 8            # 視覺化：紅色門檻
    road_flow_medium_threshold: int = 3          # 視覺化：橘色門檻

    # === 決策模式 ===
    # LLM 決策一律在本進程直呼 llm_server pipeline（run_perception / run_decision_making）。
    use_llm: bool = False                        # 預設 mock；前端可切換

    # === 可重現性 ===
    seed: int = 42

    # === 路網來源（OSM bundle 不存在時 fallback synthetic）===
    allow_osm_download: bool = True              # bundle 不存在時是否嘗試即時下載
    synthetic_grid_size: int = 12                # synthetic fallback 網格邊長節點數


@dataclass(frozen=True)
class UIConfig:
    """前端控制範圍（對應 TOML ``[ui]``）。同時驅動後端 clamp 與前端 slider。"""

    speed_min: float = 0.5
    speed_max: float = 5.0
    speed_step: float = 0.5
    speed_default: float = 1.0
    agents_min: int = 5
    agents_max: int = 80        # 同時是後端 set_agents 的 clamp 上限
    agents_step: int = 5
    # 大規模渲染：車數 ≤ render_individual_max → 逐台送/畫（任何 zoom）；超過 → 依 zoom/可視範圍裁切，
    # zoom < agent_min_zoom 時只送道路壅塞、不送車（見 engine._visible_agents）。
    render_individual_max: int = 1500
    agent_min_zoom: int = 14
    # 時間控制（前端可調「跑幾個週期 / 每週期幾分鐘」；改了需重設，比照 set_agents）
    steps_min: int = 6
    steps_max: int = 240
    steps_step: int = 6
    step_minutes_options: tuple[int, ...] = (1, 2, 5, 10)

    def to_payload(self) -> dict[str, Any]:
        """給 engine.init_payload 下發前端（key 與前端 slider 屬性對應）。"""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class MemoryConfig:
    """agent 旅次記憶（單一 ``memory``）的可調門檻（對應 TOML ``[memory]``）。

    把每步的量化感知（congestion_proxy、移動距離、距離目的地）轉成「人類印象」的
    質性標籤時所用的門檻；以及壓縮記憶時記幾個壅塞點。詳見 docs/MEMORY_zh-TW.md。
    這裡只放「數值門檻」；質性標籤文字（順暢/普通/壅塞…）是設計常數，定義在
    domain/agent.py，與 prompt 語意綁定、不從 TOML 調整。
    """

    # --- traffic_feel：當步壅塞感（congestion_proxy 介於 0..1）---
    feel_congested_proxy: float = 0.6    # proxy ≥ 此值（或 is_crowded）→「壅塞」
    feel_normal_proxy: float = 0.3       # proxy ≥ 此值 →「普通」；否則「順暢」

    # --- moved：當步移動感（每步前進公尺）---
    moved_stalled_m: float = 50.0        # 每步移動 < 此值 →「停滯」
    moved_slow_m: float = 200.0          # < 此值 →「緩慢」；否則「前進中」

    # --- overall_smoothness：整趟順暢度（整趟平均 congestion_proxy）---
    smoothness_rough_proxy: float = 0.6  # 平均 ≥ 此值 →「不順」
    smoothness_mid_proxy: float = 0.3    # 平均 ≥ 此值 →「中等」；否則「順暢」

    # --- 記憶壓縮 ---
    congested_spots_max: int = 5         # 記憶中最多保留幾個塞過的地點
    distance_decimals: int = 1           # remaining 距離顯示的小數位數


@dataclass(frozen=True)
class PerceptionContextConfig:
    """送給 LLM 的「環境感知」可調參數（對應 TOML ``[perception_context]``）。

    控制全域壅塞熱點與每車前方路況的取樣範圍，兼顧 LLM 可理解度與 max context。
    質性門檻（順暢/普通/壅塞）沿用 MemoryConfig 的 feel_*，保持同一套詞彙。
    詳見 docs/ENVIRONMENT_zh-TW.md。
    """

    hotspots_top_k: int = 5              # 全域 congestion_hotspots 取前幾個行政區
    lookahead_distance_m: float = 2000.0 # 每車 road_ahead 沿路徑往前看的距離（公尺）
    speed_free_ratio: float = 0.8        # speed/速限 ≥ 此值 →「自由流」
    speed_slow_ratio: float = 0.5        # ≥ 此值 →「略慢」；否則「壅塞緩行」


@dataclass(frozen=True)
class SummaryConfig:
    """單一旅次記憶 ``summary`` 的 LLM 摘要設定（對應 TOML ``[summary]``）。

    記憶已合併為**單一 memory**（不再分長短期；1 step=1 分鐘，長短期區分無意義）。
    規則式核心：summary 用 domain/agent.py 的確定性模板每步重算。
    LLM 核心：summary **只在該 agent「重新決策」時**由 llm_server/memory_summary.py 重寫一次
    （見 engine `_summarize_memory`），其餘時間沿用上次摘要/模板；失敗 fallback 回模板。
    這裡只保留「取不到前端所選模型時的後備摘要模型」。詳見 docs/MEMORY_zh-TW.md。
    """

    summary_model: str = "gemma4:e2b"    # 後備摘要模型 tag（整套 LLM 一般共用前端所選模型）


@dataclass(frozen=True)
class ProfileConfig:
    """agent persona「原型池」設定（對應 TOML ``[profile]``）。

    ``pool_size`` 是**原型數上限**（人物範本數），與模擬車數（nb_agents）分離：生成一次、
    存成穩定池檔、分批生成；模擬車數超過池大小時由 ``pool[i % len]`` 循環抽樣重用，
    **不會為了車多而生更多 persona**。要整批換人用前端「重新生成人物」按鈕。
    詳見 docs/PYTHON_SIMULATOR_zh-TW.md。
    """

    pool_size: int = 30                  # persona 原型數上限（建議數百個；車多時循環重用）


@dataclass(frozen=True)
class ScalingConfig:
    """LLM 決策的規模化設定（對應 TOML ``[scaling]``）。詳見 docs/SCALING_zh-TW.md。

    純事件觸發：LLM 模式下只在「踩到壅塞/前方塞」時才呼叫 LLM 重決 active_mode；
    順暢的車整趟維持規則式初始 mode。觸發的 agent 分批、並行送 LLM（同步等齊再推前端）。
    """

    event_triggered_decisions: bool = True  # 關閉＝退回「每步對全部 agent 決策」的舊行為
    cooldown_steps: int = 5                  # 同車觸發後幾步內不重複觸發（去抖動）
    batch_size: int = 30                     # B：每批最多幾個 agent（吃 context 預算）
    concurrency: int = 4                     # C：同時並行幾批（搭配後端真並行上限）


@dataclass(frozen=True)
class LLMBudgetConfig:
    """LLM token 預算（對應 TOML ``[llm_budget]``）。用來「依 token 預算動態切批」，避免 prompt 溢位。

    每批 decision prompt ≈ prompt_overhead + batch×(每 agent 約 status+persona tokens) + 輸出。
    引擎依此反推「安全 batch size」（不超過 [scaling].batch_size 上限），讓 prompt 不超過 max_model_len。
    對齊 vLLM ``--max-model-len``。詳見 calibrate.py 與 docs/CHANGES_LLM_PIPELINE_zh-TW.md。
    """

    max_model_len: int = 8192          # 對齊 vLLM --max-model-len：單次請求 prompt+輸出 token 上限
    reserve_output_tokens: int = 1024  # 為輸出保留的 token（結構化輸出後可估得更準）
    prompt_overhead_tokens: int = 800  # 模板+全域等固定開銷估計（token）
    chars_per_token: float = 2.0       # 中英混 JSON 的「字元→token」粗估比（保守；校準可量更準）


@dataclass(frozen=True)
class DemandConfig:
    """事件需求生成（出生地分配）設定（對應 TOML ``[demand]``）。詳見 mobility/demand.py、docs/DEMAND_zh-TW.md。

    用「生產約束重力模型」把 agent 出生地依各區人口 + 對場館的距離衰減分配（取代由 persona
    residential_location 決定出生地）。``enabled=False`` 或無人口資料 → 回退到既有出生地指派。
    """

    enabled: bool = True
    beta: float = 0.08          # 距離衰減係數（越大越偏好近場館的區）；decay=exp 時用於 exp(−beta·d_km)
    decay: str = "exp"          # "exp"（指數）或 "power"（冪次 d_km^(−beta)）
    min_distance_km: float = 0.5  # 距離下限（避免場館同區 d→0 造成權重爆掉）


@dataclass(frozen=True)
class AmbientConfig:
    """背景常態交通流設定（對應 TOML ``[ambient]``）。詳見 mobility/demand.py、docs/AMBIENT_zh-TW.md。

    在「去事件地點（球場）的事件車流」之外，注入一批**不指定事件終點的常態背景車流**，
    讓路網有真實的基礎負載（事件車感知到的壅塞才有意義，digital twin 更可信）。
    背景車：起訖以**雙邊重力模型**（鄉鎮對；起點 ∝ 人口、終點 ∝ 人口×距離衰減）抽樣，
    一律走**規則式核心**（不吃 LLM、不存記憶），抵達後換新 OD 重生 → 維持穩態背景負載。
    最終交通分析會把背景＋事件車流一起納入「路網層」評估（像交通局做交評）。
    無人口資料（population 全 0）→ 自動停用背景車流（fallback）。
    """

    enabled: bool = True
    count: int = 40              # 穩態背景車數（前端可調）；初始化要為每台算一次路徑，數量越大開場越久
    respawn: bool = True         # 抵達後以新 OD 重生，維持穩態（關閉＝抵達即停）
    max_count: int = 600         # 前端/介入可設的上限（保護效能；數百台時開場/重設會較久）


@dataclass(frozen=True)
class DepartureConfig:
    """事件車分批出發（時空需求）設定（對應 TOML ``[departure]``）。詳見 docs/DEMO_FEATURES_zh-TW.md。

    真實事件是「陸續抵達」而非全部同時出發。每台事件車在 ``departure_cycle`` 之前「尚未進場」
    （不移動、不算路網流量/壅塞、不顯示），到該步才開始跑。出發時間在 [0, 視窗] 內依 ``profile``
    抽樣（seeded、可重現）。``window_minutes=0`` → 全部 cycle 0 出發（＝舊行為，向後相容）。
    背景車流（ambient）不分批（本即穩態連續流）。
    """

    window_minutes: int = 0          # 出發視窗（分鐘）；0＝全部同時出發
    profile: str = "uniform"         # uniform（均勻）/ front_loaded（早到多）/ peak（接近開賽尖峰多）


@dataclass(frozen=True)
class SignalConfig:
    """紅綠燈號誌系統設定（對應 TOML ``[signals]``）。詳見 spatial/signals.py。

    台南無真實時相秒數，故 ``cycle_s`` / ``yellow_s`` 為合成值（runtime 可調，改了不需重建 artifact）；
    哪些節點是號誌、相位軸、offset 由 ``build_signals.py`` 烤進 ``data/tainan_signals.json``。
    停用（enabled=False）或 artifact 不存在時，引擎行為等同無號誌（不影響既有車流模擬）。
    """

    enabled: bool = True
    cycle_s: float = 90.0        # 號誌週期秒（兩相位各佔約一半）
    yellow_s: float = 3.0        # 每相位末尾的黃燈/清道秒（此段兩組皆紅）
    snap_threshold_m: float = 40.0  # 僅記錄用途；實際 snap 門檻在 build_signals 決定


# road_network 的 highway 速限 fallback（TOML [highway_specs] 缺省時用這組）。
_DEFAULT_HIGHWAY_SPECS: dict[str, dict[str, float]] = {
    "motorway": {"speed_car": 90, "speed_moto": 70, "lanes": 3, "capacity": 60},
    "trunk": {"speed_car": 80, "speed_moto": 65, "lanes": 2, "capacity": 50},
    "primary": {"speed_car": 60, "speed_moto": 50, "lanes": 2, "capacity": 40},
    "secondary": {"speed_car": 50, "speed_moto": 40, "lanes": 2, "capacity": 30},
    "tertiary": {"speed_car": 40, "speed_moto": 35, "lanes": 1, "capacity": 20},
    "residential": {"speed_car": 30, "speed_moto": 30, "lanes": 1, "capacity": 12},
    "unclassified": {"speed_car": 30, "speed_moto": 30, "lanes": 1, "capacity": 10},
    "service": {"speed_car": 25, "speed_moto": 25, "lanes": 1, "capacity": 8},
}
_DEFAULT_HIGHWAY_SPEC = {"speed_car": 40, "speed_moto": 35, "lanes": 1, "capacity": 15}


@dataclass(frozen=True)
class ActiveModeProfile:
    """單一 active_mode 的「數值 + 路徑策略」（對應 TOML ``[active_modes.<name>]``）。

    前四個 weight 驅動 routing 基礎成本（time/distance/comfort/capacity 的相對大小決定取向）；
    後面的策略旗標讓每個 mode 走出「不同的路徑選擇方式」，預設值皆為「關閉」→
    不啟用任何旗標時行為等同原本的最短路徑。詳見 docs/ACTIVE_MODES_zh-TW.md。
    """

    desired_speed_kmh: float = 40.0
    time_weight: float = 0.45
    distance_weight: float = 0.25
    comfort_weight: float = 0.20
    capacity_weight: float = 0.10
    # --- 路徑策略旗標 ---
    congestion_penalty: float = 0.0      # 額外壅塞懲罰倍率：成本 ×(1 + penalty×congestion)；0=不額外懲罰
    avoid_threshold: float = 1.0         # congestion_proxy > 此值的邊近乎封路（重罰）；≥1=停用（proxy 上限 1）
    road_class_bias: float = 0.0         # >0：幹道打折、小路加罰（偏好大路）；0=不分路型
    recompute_on_crowded: bool = True    # 壅塞時是否重算路徑；False=路徑定了走到底（tolerate）
    route_randomness: float = 0.0        # 每邊成本隨機微擾 ±randomness（seeded，可重現）；用來分散車流


# 五種 active_mode 的內建預設（TOML [active_modes] 缺省/缺值時的 fallback）。
# 設計取向：fast/tolerate 拚時間、avoid 拚避塞、comfortable 拚路況好、short 拚距離。
_DEFAULT_ACTIVE_MODE_PROFILES: dict[str, ActiveModeProfile] = {
    # 最短時間：時間權重高、無視壅塞、塞了會重算找更快的
    "fast": ActiveModeProfile(
        desired_speed_kmh=55.0, time_weight=0.70, distance_weight=0.20,
        comfort_weight=0.05, capacity_weight=0.05, route_randomness=0.05),
    # 時間優先但「不繞路」：成本同 fast，但塞車不重算（走到底）
    "tolerate_congestion": ActiveModeProfile(
        desired_speed_kmh=45.0, time_weight=0.55, distance_weight=0.30,
        comfort_weight=0.10, capacity_weight=0.05,
        recompute_on_crowded=False, route_randomness=0.05),
    # 避塞：壅塞重罰 + 對高壅塞邊硬避開、積極重算、高隨機分散車流
    "avoid_congestion": ActiveModeProfile(
        desired_speed_kmh=38.0, time_weight=0.20, distance_weight=0.10,
        comfort_weight=0.25, capacity_weight=0.45,
        congestion_penalty=3.0, avoid_threshold=0.6, route_randomness=0.20),
    # 舒適：偏好大路（road_class_bias），中度避塞
    "comfortable": ActiveModeProfile(
        desired_speed_kmh=42.0, time_weight=0.20, distance_weight=0.10,
        comfort_weight=0.45, capacity_weight=0.25,
        congestion_penalty=1.0, road_class_bias=0.4, route_randomness=0.10),
    # 最短距離：純距離、無視速度與路型，願意鑽小路抄近
    "short_distance": ActiveModeProfile(
        desired_speed_kmh=35.0, time_weight=0.10, distance_weight=0.70,
        comfort_weight=0.10, capacity_weight=0.10, route_randomness=0.10),
}


# ---------------------------------------------------------------------------
# TOML 載入（唯一真實來源 = config/simulation.toml；缺值/缺檔皆回退到上面的預設）
# ---------------------------------------------------------------------------
def _load_toml(path: Path) -> dict[str, Any]:
    """讀取 TOML；檔案不存在回 {}（純用程式碼預設），解析錯誤則拋出含路徑的清楚訊息。"""
    if not path.exists():
        logger.info("找不到 %s，改用程式碼內建預設值。", path)
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"設定檔解析失敗 {path}：{e}") from e


def _overrides_for(cls: type, raw: dict[str, Any], skip_sections: set[str]) -> dict[str, Any]:
    """把所有區段（skip_sections 除外）的 key 攤平，過濾出 cls 的已知欄位。

    TOML key 名稱與 dataclass 欄位名一致；tuple 型欄位的 list 值會轉成 tuple。
    """
    field_types = {f.name: f.type for f in fields(cls)}
    defaults = {f.name: f.default for f in fields(cls)}
    overrides: dict[str, Any] = {}
    for section, body in raw.items():
        if section in skip_sections or not isinstance(body, dict):
            continue
        for key, value in body.items():
            if key not in field_types:
                logger.warning("設定檔出現未知參數 [%s].%s，已忽略。", section, key)
                continue
            # 預設值是 tuple 的欄位（如 default_road_type_preference）：list → tuple
            if isinstance(defaults.get(key), tuple) and isinstance(value, list):
                value = tuple(value)
            overrides[key] = value
    return overrides


def _build_simulation_config(raw: dict[str, Any]) -> SimulationConfig:
    overrides = _overrides_for(SimulationConfig, raw, skip_sections={"ui", "highway_specs", "active_modes", "memory", "perception_context", "summary", "profile", "scaling", "signals", "llm_budget", "demand", "ambient", "departure"})
    cfg = dataclasses.replace(SimulationConfig(), **overrides)
    if cfg.max_steps <= 0 or cfg.step_minutes <= 0:
        raise ValueError("設定檔 [time].max_steps / step_minutes 必須為正整數")
    if cfg.nearby_mode not in ("grid", "exact"):
        raise ValueError("設定檔 [perception].nearby_mode 必須為 'grid' 或 'exact'")
    if cfg.town_mode not in ("node", "exact"):
        raise ValueError("設定檔 [perception].town_mode 必須為 'node' 或 'exact'")
    return cfg


def _build_memory_config(raw: dict[str, Any]) -> MemoryConfig:
    overrides = {k: v for k, v in raw.get("memory", {}).items()
                 if k in {f.name for f in fields(MemoryConfig)}}
    mem = dataclasses.replace(MemoryConfig(), **overrides)
    if not (mem.feel_normal_proxy <= mem.feel_congested_proxy):
        raise ValueError("設定檔 [memory].feel_normal_proxy 不可大於 feel_congested_proxy")
    if not (mem.moved_stalled_m <= mem.moved_slow_m):
        raise ValueError("設定檔 [memory].moved_stalled_m 不可大於 moved_slow_m")
    if mem.congested_spots_max < 0 or mem.distance_decimals < 0:
        raise ValueError("設定檔 [memory].congested_spots_max / distance_decimals 不可為負")
    return mem


def _build_perception_context(raw: dict[str, Any]) -> PerceptionContextConfig:
    overrides = {k: v for k, v in raw.get("perception_context", {}).items()
                 if k in {f.name for f in fields(PerceptionContextConfig)}}
    pc = dataclasses.replace(PerceptionContextConfig(), **overrides)
    if pc.hotspots_top_k < 0 or pc.lookahead_distance_m < 0:
        raise ValueError("設定檔 [perception_context].hotspots_top_k / lookahead_distance_m 不可為負")
    if not (pc.speed_slow_ratio <= pc.speed_free_ratio):
        raise ValueError("設定檔 [perception_context].speed_slow_ratio 不可大於 speed_free_ratio")
    return pc


def _build_summary_config(raw: dict[str, Any]) -> SummaryConfig:
    overrides = {k: v for k, v in raw.get("summary", {}).items()
                 if k in {f.name for f in fields(SummaryConfig)}}
    return dataclasses.replace(SummaryConfig(), **overrides)


def _build_profile_config(raw: dict[str, Any]) -> ProfileConfig:
    overrides = {k: v for k, v in raw.get("profile", {}).items()
                 if k in {f.name for f in fields(ProfileConfig)}}
    pc = dataclasses.replace(ProfileConfig(), **overrides)
    if pc.pool_size < 1:
        raise ValueError("設定檔 [profile].pool_size 必須 ≥ 1")
    return pc


def _build_scaling_config(raw: dict[str, Any]) -> ScalingConfig:
    overrides = {k: v for k, v in raw.get("scaling", {}).items()
                 if k in {f.name for f in fields(ScalingConfig)}}
    sc = dataclasses.replace(ScalingConfig(), **overrides)
    if sc.cooldown_steps < 0 or sc.batch_size < 1 or sc.concurrency < 1:
        raise ValueError("設定檔 [scaling]：cooldown_steps≥0、batch_size≥1、concurrency≥1")
    return sc


def _build_demand_config(raw: dict[str, Any]) -> DemandConfig:
    overrides = {k: v for k, v in raw.get("demand", {}).items()
                 if k in {f.name for f in fields(DemandConfig)}}
    d = dataclasses.replace(DemandConfig(), **overrides)
    if d.decay not in ("exp", "power"):
        raise ValueError("設定檔 [demand].decay 必須為 'exp' 或 'power'")
    if d.beta < 0 or d.min_distance_km <= 0:
        raise ValueError("設定檔 [demand].beta 不可為負、min_distance_km 必須為正")
    return d


def _build_ambient_config(raw: dict[str, Any]) -> AmbientConfig:
    overrides = {k: v for k, v in raw.get("ambient", {}).items()
                 if k in {f.name for f in fields(AmbientConfig)}}
    a = dataclasses.replace(AmbientConfig(), **overrides)
    if a.count < 0 or a.max_count < 0:
        raise ValueError("設定檔 [ambient].count / max_count 不可為負")
    return a


def _build_departure_config(raw: dict[str, Any]) -> DepartureConfig:
    overrides = {k: v for k, v in raw.get("departure", {}).items()
                 if k in {f.name for f in fields(DepartureConfig)}}
    d = dataclasses.replace(DepartureConfig(), **overrides)
    if d.window_minutes < 0:
        raise ValueError("設定檔 [departure].window_minutes 不可為負")
    if d.profile not in ("uniform", "front_loaded", "peak"):
        raise ValueError("設定檔 [departure].profile 必須為 'uniform' / 'front_loaded' / 'peak'")
    return d


def _build_llm_budget_config(raw: dict[str, Any]) -> LLMBudgetConfig:
    overrides = {k: v for k, v in raw.get("llm_budget", {}).items()
                 if k in {f.name for f in fields(LLMBudgetConfig)}}
    b = dataclasses.replace(LLMBudgetConfig(), **overrides)
    if b.max_model_len <= 0 or b.chars_per_token <= 0:
        raise ValueError("設定檔 [llm_budget].max_model_len / chars_per_token 必須為正數")
    if b.reserve_output_tokens < 0 or b.prompt_overhead_tokens < 0:
        raise ValueError("設定檔 [llm_budget].reserve_output_tokens / prompt_overhead_tokens 不可為負")
    if b.reserve_output_tokens + b.prompt_overhead_tokens >= b.max_model_len:
        raise ValueError("設定檔 [llm_budget]：reserve_output_tokens + prompt_overhead_tokens 必須 < max_model_len")
    return b


def _build_signal_config(raw: dict[str, Any]) -> SignalConfig:
    overrides = {k: v for k, v in raw.get("signals", {}).items()
                 if k in {f.name for f in fields(SignalConfig)}}
    sc = dataclasses.replace(SignalConfig(), **overrides)
    if sc.cycle_s <= 0:
        raise ValueError("設定檔 [signals].cycle_s 必須為正數")
    if not (0 <= sc.yellow_s < sc.cycle_s / 2):
        raise ValueError("設定檔 [signals].yellow_s 必須 ≥0 且 < cycle_s/2")
    return sc


def _build_ui_config(raw: dict[str, Any]) -> UIConfig:
    overrides = {k: v for k, v in raw.get("ui", {}).items()
                 if k in {f.name for f in fields(UIConfig)}}
    ui = dataclasses.replace(UIConfig(), **overrides)
    if ui.agents_min > ui.agents_max:
        raise ValueError("設定檔 [ui].agents_min 不可大於 agents_max")
    if ui.speed_min > ui.speed_max:
        raise ValueError("設定檔 [ui].speed_min 不可大於 speed_max")
    if ui.render_individual_max < 0 or ui.agent_min_zoom < 0:
        raise ValueError("設定檔 [ui].render_individual_max / agent_min_zoom 不可為負")
    if ui.steps_min > ui.steps_max or ui.steps_min < 1:
        raise ValueError("設定檔 [ui].steps_min 須 ≥1 且不可大於 steps_max")
    if not ui.step_minutes_options:
        raise ValueError("設定檔 [ui].step_minutes_options 不可為空")
    return ui


def _build_highway_specs(raw: dict[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    specs = raw.get("highway_specs")
    if not isinstance(specs, dict) or not specs:
        return dict(_DEFAULT_HIGHWAY_SPECS), dict(_DEFAULT_HIGHWAY_SPEC)
    table = {k: dict(v) for k, v in specs.items() if isinstance(v, dict) and k != "default"}
    default_spec = dict(specs.get("default", _DEFAULT_HIGHWAY_SPEC))
    return (table or dict(_DEFAULT_HIGHWAY_SPECS)), default_spec


def _build_active_mode_profiles(raw: dict[str, Any]) -> dict[str, ActiveModeProfile]:
    """以內建預設為底，用 TOML [active_modes.<name>] 覆寫各 mode；缺的 mode 補回預設。"""
    section = raw.get("active_modes")
    profiles = dict(_DEFAULT_ACTIVE_MODE_PROFILES)
    if isinstance(section, dict):
        valid = {f.name for f in fields(ActiveModeProfile)}
        for name, body in section.items():
            if not isinstance(body, dict):
                continue
            overrides = {k: v for k, v in body.items() if k in valid}
            base = profiles.get(name, ActiveModeProfile())
            profiles[name] = dataclasses.replace(base, **overrides)
    return profiles


# ---------------------------------------------------------------------------
# 模組層匯出（載入一次；下游沿用 DEFAULT_CONFIG / UI_CONFIG / HIGHWAY_SPECS）
# ---------------------------------------------------------------------------
_RAW = _load_toml(CONFIG_TOML)

# 預設情境（web 層與測試可直接引用，再以 dataclasses.replace 覆寫）
DEFAULT_CONFIG = _build_simulation_config(_RAW)
UI_CONFIG = _build_ui_config(_RAW)
MEMORY_CONFIG = _build_memory_config(_RAW)
PERCEPTION_CONTEXT = _build_perception_context(_RAW)
SUMMARY_CONFIG = _build_summary_config(_RAW)
PROFILE_CONFIG = _build_profile_config(_RAW)
SCALING_CONFIG = _build_scaling_config(_RAW)
DEMAND_CONFIG = _build_demand_config(_RAW)
AMBIENT_CONFIG = _build_ambient_config(_RAW)
DEPARTURE_CONFIG = _build_departure_config(_RAW)
LLM_BUDGET = _build_llm_budget_config(_RAW)
SIGNAL_CONFIG = _build_signal_config(_RAW)
HIGHWAY_SPECS, DEFAULT_HIGHWAY_SPEC = _build_highway_specs(_RAW)
ACTIVE_MODE_PROFILES = _build_active_mode_profiles(_RAW)

# runtime 可覆寫的 max_model_len（前端選模型時依該模型 context 設定；None＝用 [llm_budget] 值）
_RUNTIME_MAX_MODEL_LEN: int | None = None


def set_runtime_max_model_len(n: int | None) -> None:
    global _RUNTIME_MAX_MODEL_LEN
    _RUNTIME_MAX_MODEL_LEN = n


def effective_max_model_len() -> int:
    """token 預算切批用的有效 max_model_len（runtime 覆寫優先，否則 [llm_budget]）。"""
    return _RUNTIME_MAX_MODEL_LEN or LLM_BUDGET.max_model_len


# runtime 可覆寫的背景車數（前端 slider / NL 介入調整；None＝用 [ambient].count）
_RUNTIME_AMBIENT_COUNT: int | None = None


def set_runtime_ambient_count(n: int | None) -> None:
    global _RUNTIME_AMBIENT_COUNT
    _RUNTIME_AMBIENT_COUNT = n


def effective_ambient_count() -> int:
    """有效背景車數：停用→0；runtime 覆寫優先，否則 [ambient].count，clamp 到 max_count。"""
    if not AMBIENT_CONFIG.enabled:
        return 0
    n = AMBIENT_CONFIG.count if _RUNTIME_AMBIENT_COUNT is None else _RUNTIME_AMBIENT_COUNT
    return max(0, min(int(n), AMBIENT_CONFIG.max_count))
