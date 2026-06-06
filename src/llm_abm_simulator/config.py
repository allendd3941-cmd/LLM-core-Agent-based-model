"""config.py — 集中管理模擬器所有可調整參數。

這裡的數值與語意刻意對齊 ``gama_moudle/Traffic_ABM_LLM_complete_v2.gaml`` 的
``global`` [可調整參數區]，讓 Python 模擬器與原 GAMA 模型行為一致、便於對照。
之後要調整模擬行為，原則上只改這一個檔。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 專案路徑（src/llm_abm_simulator/config.py → 專案根目錄上溯三層）
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent

DATA_DIR = PROJECT_ROOT / "data"        # 所有資料集中於此（GIS 原始資料 + 衍生路網）
GIS_DIR = DATA_DIR / "gis"              # 原 "GIS data/"，整理後移至 data/gis/
OUTPUT_DIR = PROJECT_ROOT / "output"    # runtime artifact（CSV），已被 .gitignore 忽略
FRONTEND_DIR = PROJECT_ROOT / "simulation_web" / "frontend"

# GIS 來源檔（對齊 GAML shape_file_* 設定；ROADLINK 在本專案不存在，改由 OSM/synthetic 取代）
TOWN_SHP = GIS_DIR / "TOWN_MOI_1140318_3826.shp"
STADIUM_SHP = GIS_DIR / "亞太棒球場_point.shp"
STUDY_AREA_SHP = GIS_DIR / "亞太棒球場_研究範圍.shp"
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

    # === LLM API（既有 server.py /from-gama）===
    # 註：GAML 內 api_port 為 8001，但 server.py / README 實際在 8000。預設對齊 server.py。
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_endpoint: str = "/from-gama"
    api_timeout_s: float = 120.0

    # === 決策模式 ===
    use_llm: bool = False                        # 預設 mock；前端可切換

    # === 可重現性 ===
    seed: int = 42

    # === 路網來源（OSM bundle 不存在時 fallback synthetic）===
    allow_osm_download: bool = True              # bundle 不存在時是否嘗試即時下載
    synthetic_grid_size: int = 12                # synthetic fallback 網格邊長節點數

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}{self.api_endpoint}"


# 預設情境（web 層與測試可直接引用，再以 dataclasses.replace 覆寫）
DEFAULT_CONFIG = SimulationConfig()
