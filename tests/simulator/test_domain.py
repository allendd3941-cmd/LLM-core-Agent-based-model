"""domain 模型測試：action_mode 套用、車種正規化、Road 動態更新。"""

from __future__ import annotations

from llm_abm_simulator.config import ACTION_MODE_PROFILES, DEFAULT_CONFIG
from llm_abm_simulator.domain.agent import VehicleAgent
from llm_abm_simulator.domain.road import Road
from llm_abm_simulator.domain.events import RouteStatus


def test_apply_action_mode_dict_partial_update():
    a = VehicleAgent.from_config("v1", DEFAULT_CONFIG)
    a.apply_action_mode({"mode_name": "avoid_congestion", "time_weight": 0.6})
    assert a.action_mode == "avoid_congestion"
    # 套 mode 會先載入整組 action_mode profile（見 docs/ACTION_MODES_zh-TW.md），
    # 再讓 dict 內明確給的欄位覆寫：time_weight 被覆寫成 0.6，未指定的欄位＝該 profile 的值。
    assert a.time_weight == 0.6
    assert a.comfort_weight == ACTION_MODE_PROFILES["avoid_congestion"].comfort_weight


def test_apply_action_mode_string():
    a = VehicleAgent.from_config("v1", DEFAULT_CONFIG)
    a.apply_action_mode("fast")
    assert a.action_mode == "fast"


def test_apply_vehicle_type():
    a = VehicleAgent.from_config("v1", DEFAULT_CONFIG)
    a.apply_vehicle_type("一輛機車")          # 中文輸入也正規化為英文 canonical
    assert a.vehicle_type == "motorcycle"
    a.apply_vehicle_type("")                   # 空字串不變更
    assert a.vehicle_type == "motorcycle"
    a.apply_vehicle_type("a family car")
    assert a.vehicle_type == "car"


def test_road_update_flow_congestion_and_weight():
    r = Road(road_id="r1", node_a="a", node_b="b", length=200, speed_car=50,
             speed_moto=40, capacity=10)
    r.update_flow(10, capacity_fallback=10.0, flow_multiplier=2.0)
    assert r.congestion_proxy == 1.0                  # 10/10 截斷於 1
    assert r.weight == 200 * (1 + 10 * 2.0)           # 鏡像 GAML
    assert r.speed_limit_for("motorcycle") == 40


def test_route_status_serializes_to_value():
    assert str(RouteStatus.ARRIVED) == "arrived"
