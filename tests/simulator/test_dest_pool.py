"""稀疏終點池測試：每區 ceil(人口/per_capita) 個不重複節點，全市終點數遠小於節點數。

驗 engine._build_dest_pool / _dest_node_in_town（[demand].dest_pool_per_capita）。用真實 GIS（small_config）。
"""

from __future__ import annotations

import math

from llm_abm_simulator import config
from llm_abm_simulator.simulation.engine import SimulationEngine


def test_dest_pool_bounded_and_subset(small_config):
    per_cap = config.DEMAND_CONFIG.dest_pool_per_capita
    assert per_cap > 0, "預設應啟用稀疏終點"

    eng = SimulationEngine(small_config)
    eng.initialize()

    towns = [t for t in eng.towns
             if t.population and t.population > 0 and eng._town_nodes.get(t.town_name)]
    assert towns, "需有有人口且有節點的區"
    t = towns[0]
    town_nodes = eng._town_nodes[t.town_name]
    pool = eng._dest_pool_for(t.town_name)

    expect = min(math.ceil(t.population / per_cap), len(town_nodes))
    assert len(pool) == expect                      # 數量 = ceil(人口/per_cap)（受該區節點數上限）
    assert len(set(pool)) == len(pool)              # 不重複
    assert set(pool) <= set(town_nodes)             # 都在該區節點內
    assert eng._dest_node_in_town(t.town_name) in set(pool)   # 終點解析回池節點


def test_dest_pool_total_smaller_than_nodes(small_config):
    eng = SimulationEngine(small_config)
    eng.initialize()
    total_pool = sum(len(eng._dest_pool_for(t.town_name)) for t in eng.towns)
    assert 0 < total_pool < len(eng.network._node_ids)   # 終點數遠少於全節點（這正是稀疏的目的）


def test_dest_pool_deterministic_and_disable(small_config):
    import dataclasses
    # 同 seed → 同池
    e1 = SimulationEngine(small_config); e1.initialize()
    e2 = SimulationEngine(small_config); e2.initialize()
    name = next(t.town_name for t in e1.towns
                if t.population and t.population > 0 and e1._town_nodes.get(t.town_name))
    assert e1._dest_pool_for(name) == e2._dest_pool_for(name)

    # per_capita ≤ 0 → 停用（回空池 → _dest_node_in_town 退回 _node_in_town，仍是該區節點）
    orig = config.DEMAND_CONFIG
    try:
        config.DEMAND_CONFIG = dataclasses.replace(orig, dest_pool_per_capita=0)
        e3 = SimulationEngine(small_config); e3.initialize()
        assert e3._dest_pool_for(name) == []
        assert e3._dest_node_in_town(name) in set(e3._town_nodes[name])
    finally:
        config.DEMAND_CONFIG = orig
