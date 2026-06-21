"""mock_policy 測試：確定性、欄位合法。"""

from __future__ import annotations

from llm_abm_simulator.config import ACTION_MODES, VEHICLE_TYPES, DEFAULT_CONFIG
from llm_abm_simulator.decisions.mock_policy import MockDecisionPolicy
from llm_abm_simulator.domain.agent import VehicleAgent
from llm_abm_simulator.simulation.random_seed import make_rng

TOWNS = ["東區", "安南區", "永康區"]


def _agents(n=5):
    return [VehicleAgent.from_config(f"v{i}", DEFAULT_CONFIG) for i in range(n)]


def test_initialize_deterministic_same_seed():
    p1 = MockDecisionPolicy(DEFAULT_CONFIG, make_rng(42))
    p2 = MockDecisionPolicy(DEFAULT_CONFIG, make_rng(42))
    a1 = p1.initialize_agents(_agents(), TOWNS)
    a2 = p2.initialize_agents(_agents(), TOWNS)
    assert {k: (v.origin_town, v.vehicle_type, v.action_mode) for k, v in a1.items()} == \
           {k: (v.origin_town, v.vehicle_type, v.action_mode) for k, v in a2.items()}


def test_initialize_fields_valid():
    p = MockDecisionPolicy(DEFAULT_CONFIG, make_rng(1))
    for asg in p.initialize_agents(_agents(), TOWNS).values():
        assert asg.origin_town in TOWNS
        assert asg.vehicle_type in VEHICLE_TYPES
        assert asg.action_mode in ACTION_MODES


def test_decide_step_rules():
    p = MockDecisionPolicy(DEFAULT_CONFIG, make_rng(1))
    agents = _agents(1)
    a = agents[0]
    a.congestion_proxy = 0.8
    d = p.decide_step(agents, {}, 1)
    assert d[a.agent_id].action_mode == "avoid_congestion"
    a.congestion_proxy = 0.0
    a.distance_to_destination = 1000
    d = p.decide_step(agents, {}, 1)
    assert d[a.agent_id].action_mode == "short_distance"
