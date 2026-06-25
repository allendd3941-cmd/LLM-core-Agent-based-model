"""驗證 CSV 三流量（總/事件/背景）匯出測試。

需求：偵測器驗證 CSV 從「只輸出事件車（doc_count）」擴成「同一份 CSV 一次輸出
總流量(total_count)、事件車(doc_count)、背景車(background_count)」，且：
  - doc_count 語意/數值不變（co-author impact 讀此欄）；
  - 原 6 欄名稱與順序不變、新增兩欄追加末尾；
  - weekend/weekday 時間視窗、5 分鐘 binning、ext_id 編號對照不變；
  - 每列滿足 total_count == doc_count + background_count；background ≥ 0。

作法：SimulationEngine(cfg) 很輕量（只需 cfg+scheduler，無網路），直接注入已知的偵測器
時間序列再匯出、解 zip、驗 CSV（隔離測匯出數學，不跑完整模擬）。
"""

from __future__ import annotations

import csv
import dataclasses
import io
import zipfile

from llm_abm_simulator.config import DEFAULT_CONFIG
from llm_abm_simulator.simulation.engine import SimulationEngine

_EXPECTED_FIRST6 = ["camera_name", "device_group_id", "stream_id", "time_start", "doc_count", "avg_speed"]
_EXPECTED_ALL = _EXPECTED_FIRST6 + ["total_count", "background_count"]
_EXT_ID = "uuid-cam-1"
_EXT_NAME = "長和路相機A"


def _export(tmp_path, case, total, event, step_minutes=5):
    """注入 total/event 步序列 → 匯出 → 回 (fieldnames, gameday_rows, nogameday_rows)。"""
    cfg = dataclasses.replace(DEFAULT_CONFIG, step_minutes=step_minutes)
    eng = SimulationEngine(cfg)
    eng.scheduler.cycle = len(total)
    eng._detectors = [{
        "id": "D1", "ext_id": _EXT_ID, "ext_name": _EXT_NAME, "label": "road-A",
        "lat": 23.0, "lng": 120.2, "dir_a": "a_b", "dir_b": "b_a",
        "a": {"ce": 0, "ca": 0, "me": 0, "ma": 0}, "b": {"ce": 0, "ca": 0, "me": 0, "ma": 0},
    }]
    eng._detector_series = {"D1": list(total)}
    eng._detector_series_event = {"D1": list(event)}

    zip_path = eng.export_validation_csv(case, tmp_path)

    def read(name):
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(name) as f:
                rdr = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                return rdr.fieldnames, list(rdr)

    fields, gameday = read(f"{case}_gameday.csv")
    _, nogameday = read(f"{case}_nogameday.csv")
    return fields, gameday, nogameday


def test_columns_appended_first6_unchanged(tmp_path):
    """原 6 欄名稱與順序不變；新欄 total_count/background_count 追加末尾。"""
    fields, _, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    assert fields[:6] == _EXPECTED_FIRST6      # 前 6 欄位置與名稱不動（向後相容）
    assert fields == _EXPECTED_ALL


def test_doc_count_backward_compatible(tmp_path):
    """doc_count 逐格＝事件序列（語意/數值不變）。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    assert [int(r["doc_count"]) for r in gameday] == [2, 1, 3]


def test_total_count_is_total_series(tmp_path):
    """total_count 逐格＝總序列。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    assert [int(r["total_count"]) for r in gameday] == [5, 4, 7]


def test_background_is_total_minus_event(tmp_path):
    """background_count 逐格＝總−事件。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    assert [int(r["background_count"]) for r in gameday] == [3, 3, 4]


def test_row_invariant_total_eq_doc_plus_background(tmp_path):
    """每列不變式：total == doc + background。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    for r in gameday:
        assert int(r["total_count"]) == int(r["doc_count"]) + int(r["background_count"])


def test_nogameday_all_three_zero(tmp_path):
    """nogameday 三流量欄全 0（維持零基線慣例）。"""
    _, _, nogameday = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 1, 3])
    for r in nogameday:
        assert int(r["doc_count"]) == 0
        assert int(r["total_count"]) == 0
        assert int(r["background_count"]) == 0


def test_background_clamped_non_negative(tmp_path):
    """event > total（dedup 邊界）時 background 不為負 → clamp 到 0。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[5, 4, 7], event=[2, 9, 3])
    assert [int(r["background_count"]) for r in gameday] == [3, 0, 4]


def test_weekend_time_window_unchanged(tmp_path):
    """weekend 視窗起點 14:00、格間隔 5 分鐘（不變）。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[1, 1, 1], event=[0, 0, 0])
    ts = [r["time_start"] for r in gameday]
    assert ts[0] == "2026-03-29 14:00:00"
    assert ts[1] == "2026-03-29 14:05:00"
    assert ts[2] == "2026-03-29 14:10:00"


def test_weekday_time_window_unchanged(tmp_path):
    """weekday 視窗起點 16:30（不變）。"""
    _, gameday, _ = _export(tmp_path, "weekday", total=[1, 1], event=[0, 0])
    assert gameday[0]["time_start"] == "2026-04-22 16:30:00"


def test_ext_id_mapping_unchanged(tmp_path):
    """編號對照不變：device_group_id==ext_id、camera_name==ext_name。"""
    _, gameday, _ = _export(tmp_path, "weekend", total=[1], event=[0])
    assert gameday[0]["device_group_id"] == _EXT_ID
    assert gameday[0]["camera_name"] == _EXT_NAME


def test_binning_5min_unchanged(tmp_path):
    """step_minutes=1 → 每 5 步聚合一格（binning 不變）。"""
    # 10 步、step_minutes=1 → steps_per_bin=5 → 2 格
    total = [1, 1, 1, 1, 1, 2, 2, 2, 2, 2]   # 每格 5 步：sum=5, 10
    event = [0, 1, 0, 1, 0, 1, 1, 1, 1, 1]   # 每格：sum=2, 5
    _, gameday, _ = _export(tmp_path, "weekend", total=total, event=event, step_minutes=1)
    assert len(gameday) == 2
    assert [int(r["total_count"]) for r in gameday] == [5, 10]
    assert [int(r["doc_count"]) for r in gameday] == [2, 5]
    assert [int(r["background_count"]) for r in gameday] == [3, 5]
