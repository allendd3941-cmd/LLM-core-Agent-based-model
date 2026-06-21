"""detector ext_id/ext_name round-trip 修正測試。

bug：前端「套用設定」會把目前地圖上的偵測器整批回送，舊版 ``_sanitize_detectors`` 只保留
lat/lng，洗掉相機 UUID（ext_id）→ 重新初始化後偵測器無 ext_id → ``export_validation_csv``
的 ``cams = [d for d in self._detectors if d.get("ext_id")]`` 為空 → 匯出的驗證 CSV 全空。
修正：``_sanitize_detectors`` 在 ext_id/ext_name 存在（且非空）時保留之，使預設 55 台相機
即使經過 apply_config round-trip 也保住 UUID。
"""

from __future__ import annotations

from llm_abm_simulator.web.websocket import _sanitize_detectors


def test_sanitize_preserves_ext_id_and_name():
    raw = [{"lat": 23.0, "lng": 120.2, "ext_id": "uuid-123", "ext_name": "長和路相機"}]
    assert _sanitize_detectors(raw) == [
        {"lat": 23.0, "lng": 120.2, "ext_id": "uuid-123", "ext_name": "長和路相機"}
    ]


def test_sanitize_manual_point_has_no_ext_fields():
    """手動放置的點沒有 UUID → 不應憑空多出 ext_id 欄位。"""
    out = _sanitize_detectors([{"lat": 23.0, "lng": 120.2}])
    assert out == [{"lat": 23.0, "lng": 120.2}]
    assert "ext_id" not in out[0]


def test_sanitize_empty_ext_id_not_kept():
    """ext_id 為空字串 / ext_name 為 None 不保留（避免假 UUID 混入匯出）。"""
    out = _sanitize_detectors([{"lat": 1.0, "lng": 2.0, "ext_id": "", "ext_name": None}])
    assert out == [{"lat": 1.0, "lng": 2.0}]


def test_sanitize_drops_invalid_entries():
    raw = [{"lat": 1.0}, {"lng": 2.0}, "notadict", {"lat": 3.0, "lng": 4.0}]
    assert _sanitize_detectors(raw) == [{"lat": 3.0, "lng": 4.0}]


def test_sanitize_caps_at_100():
    """上限 100：容得下 55 台相機 + 手動加放數台，又防爆量。"""
    raw = [{"lat": i, "lng": i, "ext_id": f"u{i}"} for i in range(150)]
    out = _sanitize_detectors(raw)
    assert len(out) == 100
    assert out[0] == {"lat": 0, "lng": 0, "ext_id": "u0"}
