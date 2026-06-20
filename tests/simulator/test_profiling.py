"""StepProfiler 與 session-id logging filter 的單元測試。"""

from __future__ import annotations

import logging


def test_profiler_disabled_is_noop(caplog):
    from llm_abm_simulator.simulation.profiling import StepProfiler
    p = StepProfiler(enabled=False)
    with p.phase("move"):
        pass
    p.add("reroute", 1.0)
    p.count("reroute_n", 5)
    with caplog.at_level(logging.INFO):
        p.flush(1)
    assert "prof:" not in caplog.text            # 關閉時不印
    assert p._times == {} and p._counts == {}    # 也不累積


def test_profiler_enabled_accumulates_and_logs(caplog):
    from llm_abm_simulator.simulation.profiling import StepProfiler
    p = StepProfiler(enabled=True)
    with p.phase("decide"):
        pass
    p.add("move", 2.0)
    p.add("reroute", 1.5)
    p.count("reroute_n", 3)
    with caplog.at_level(logging.INFO):
        p.flush(7)
    assert "step 7 prof:" in caplog.text
    assert "decide=" in caplog.text and "move=" in caplog.text
    assert "reroute=" in caplog.text and "n=3" in caplog.text
    assert p._times == {} and p._counts == {}    # flush 後清空


def test_session_id_filter_injects_sid():
    from llm_abm_simulator.web.logctx import session_id_var, SessionIdFilter
    session_id_var.set("ab12")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    assert SessionIdFilter().filter(rec) is True
    assert rec.sid == "ab12"
