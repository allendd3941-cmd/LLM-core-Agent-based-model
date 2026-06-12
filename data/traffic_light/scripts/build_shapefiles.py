from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry import Point

from source_config import SOURCE_SPECS, TAIWAN_CITIES, SourceSpec


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
RAW_DIR = ROOT / "raw"
PROCESSED_DIR = ROOT / "processed"
SHP_DIR = ROOT / "shapefile"
META_DIR = ROOT / "metadata"
LOG_DIR = ROOT / "logs"
TOWN_SHP = WORKSPACE / "data" / "gis" / "TOWN_MOI_1140318_3826.shp"
TODAY = dt.date.today().isoformat()


POINT_COLUMNS = [
    "signal_id",
    "intersection_name",
    "city",
    "district",
    "road_1",
    "road_2",
    "control_type",
    "longitude",
    "latitude",
    "source_id",
    "source_url",
    "source_dataset_name",
    "source_update_date",
    "download_date",
    "source_type",
    "source_reliability_note",
    "coordinate_system",
    "coordinate_transformed",
    "deduplicated",
    "note",
]

PHASE_COLUMNS = [
    "signal_id",
    "intersection_name",
    "city",
    "district",
    "plan_type",
    "time_from",
    "time_to",
    "phase_no",
    "phase_sequence",
    "green_seconds",
    "yellow_seconds",
    "all_red_seconds",
    "cycle_seconds",
    "actuated",
    "logic",
    "source_id",
    "source_url",
    "source_dataset_name",
    "source_update_date",
    "download_date",
    "missing_or_uncertain_note",
    "longitude",
    "latitude",
]

POINT_SHP_MAP = {
    "SIG_ID": "signal_id",
    "INT_NAME": "intersection_name",
    "CITY": "city",
    "DIST": "district",
    "ROAD_1": "road_1",
    "ROAD_2": "road_2",
    "CTRL_TYPE": "control_type",
    "LON": "longitude",
    "LAT": "latitude",
    "SRC_ID": "source_id",
    "SRC_URL": "source_url",
    "UPD_DATE": "source_update_date",
    "DL_DATE": "download_date",
    "OFFICIAL": "source_type",
    "NOTE": "note",
}

PHASE_SHP_MAP = {
    "SIG_ID": "signal_id",
    "INT_NAME": "intersection_name",
    "CITY": "city",
    "PLAN_TYPE": "plan_type",
    "TIME_FROM": "time_from",
    "TIME_TO": "time_to",
    "PHASE_NO": "phase_no",
    "PHASE_SEQ": "phase_sequence",
    "GREEN_S": "green_seconds",
    "YELLOW_S": "yellow_seconds",
    "ALLRED_S": "all_red_seconds",
    "CYCLE_S": "cycle_seconds",
    "ACTUATED": "actuated",
    "LOGIC": "logic",
    "SRC_ID": "source_id",
    "UPD_DATE": "source_update_date",
    "DL_DATE": "download_date",
    "NOTE": "missing_or_uncertain_note",
}

DATA_GOV_DATASET_IDS = {
    "TPE_SIGNAL_POINTS": "121890",
    "TPE_PED_PHASE": "121233",
    "TPE_TIMING_TABLE": "145720",
    "TPE_TIMING_PLAN": "145720",
    "TPE_TIMING_CSV": "145720",
    "TNN_SIGNAL_SHP_A": "137239",
    "TNN_SIGNAL_SHP_B": "137239",
    "TNN_WIFI_SIGNAL": "79978",
    "TNN_PED_PHASE": "102305",
    "CHA_SIGNAL_POINTS": "37695",
    "PENGHU_SIGNAL_POINTS_PHASE": "113153",
    "TC_SIGNAL_BOX": "82304",
    "TC_SIGNAL_GIS_INDEX": "103743",
    "CY_COUNTY_PHASE_INDEX": "156838",
}


def ensure_dirs() -> None:
    for path in [PROCESSED_DIR, SHP_DIR, META_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def raw_path(spec: SourceSpec) -> Path:
    return RAW_DIR / spec.source_id / spec.file_name


def source_by_id(source_id: str) -> SourceSpec:
    return next(s for s in SOURCE_SPECS if s.source_id == source_id)


def read_csv_loose(path: Path) -> pd.DataFrame:
    for encoding in ["utf-8-sig", "utf-8", "cp950", "big5", "latin1"]:
        for kwargs in [{"sep": None, "engine": "python"}, {}]:
            try:
                df = pd.read_csv(path, dtype=str, encoding=encoding, **kwargs).fillna("")
                df.columns = [str(col).strip() for col in df.columns]
                return df
            except Exception:
                continue
    return pd.DataFrame()


def repair_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text or "Ã" in text:
        return text
    if any(marker in text for marker in ["ä", "å", "è", "é", "æ", "ç"]):
        try:
            return text.encode("latin1").decode("utf-8")
        except Exception:
            return text
    return text


def to_num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def in_taiwan(lon: float | None, lat: float | None) -> bool:
    return lon is not None and lat is not None and 118.0 <= lon <= 123.8 and 21.5 <= lat <= 26.8


def normalize_city(value: str) -> str:
    return str(value).replace("台", "臺").strip()


def make_point_row(
    spec: SourceSpec,
    signal_id: str,
    intersection_name: str,
    city: str,
    district: str = "",
    road_1: str = "",
    road_2: str = "",
    control_type: str = "",
    lon: float | None = None,
    lat: float | None = None,
    update_date: str = "",
    coord_system: str = "WGS84 / EPSG:4326",
    transformed: str = "否",
    note: str = "",
) -> dict[str, Any] | None:
    if not in_taiwan(lon, lat):
        return None
    return {
        "signal_id": signal_id,
        "intersection_name": intersection_name,
        "city": normalize_city(city),
        "district": district,
        "road_1": road_1,
        "road_2": road_2,
        "control_type": control_type,
        "longitude": lon,
        "latitude": lat,
        "source_id": spec.source_id,
        "source_url": spec.landing_url,
        "source_dataset_name": spec.source_name,
        "source_update_date": update_date,
        "download_date": TODAY,
        "source_type": "官方" if spec.official else "非官方補充",
        "source_reliability_note": "官方開放資料" if spec.official else "OpenStreetMap 社群資料，需人工抽查。",
        "coordinate_system": coord_system,
        "coordinate_transformed": transformed,
        "deduplicated": "否",
        "note": note or spec.note,
    }


def make_phase_row(
    spec: SourceSpec,
    signal_id: str,
    intersection_name: str,
    city: str,
    district: str = "",
    plan_type: str = "",
    time_from: str = "",
    time_to: str = "",
    phase_no: str = "",
    phase_sequence: str = "",
    green_seconds: str = "",
    yellow_seconds: str = "",
    all_red_seconds: str = "",
    cycle_seconds: str = "",
    actuated: str = "",
    logic: str = "",
    update_date: str = "",
    note: str = "",
    lon: float | None = None,
    lat: float | None = None,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "intersection_name": intersection_name,
        "city": normalize_city(city),
        "district": district,
        "plan_type": plan_type,
        "time_from": time_from,
        "time_to": time_to,
        "phase_no": phase_no,
        "phase_sequence": phase_sequence,
        "green_seconds": green_seconds,
        "yellow_seconds": yellow_seconds,
        "all_red_seconds": all_red_seconds,
        "cycle_seconds": cycle_seconds,
        "actuated": actuated,
        "logic": logic,
        "source_id": spec.source_id,
        "source_url": spec.landing_url,
        "source_dataset_name": spec.source_name,
        "source_update_date": update_date,
        "download_date": TODAY,
        "missing_or_uncertain_note": note or spec.note,
        "longitude": lon,
        "latitude": lat,
    }


def split_intersection(name: str) -> tuple[str, str]:
    text = str(name).strip()
    for sep in ["與", "及", "交叉口", "/", "、", "-"]:
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
    return text, ""


def parse_time_range(text: str) -> tuple[str, str]:
    text = str(text).strip()
    match = re.search(r"(\d{1,2}:?\d{2})\s*[~-]\s*(\d{1,2}:?\d{2})", text)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def parse_taipei_points(spec: SourceSpec) -> list[dict[str, Any]]:
    df = read_csv_loose(raw_path(spec))
    rows = []
    for _, row in df.iterrows():
        lon = to_num(row.get("WGS經度座標") or row.get("WGSX") or row.get("經度"))
        lat = to_num(row.get("WGS緯度座標") or row.get("WGSY") or row.get("緯度"))
        seq = str(row.get("流水號", "")).strip()
        name = str(row.get("地點", "") or row.get("路口名稱", "")).strip()
        road_1, road_2 = split_intersection(name)
        item = make_point_row(
            spec,
            signal_id=f"TPE_{seq}" if seq else f"TPE_{len(rows) + 1}",
            intersection_name=name,
            city="臺北市",
            district=str(row.get("行政區", "")).strip(),
            road_1=road_1,
            road_2=road_2,
            lon=lon,
            lat=lat,
        )
        if item:
            rows.append(item)
    return rows


def parse_taipei_ped_phase(spec: SourceSpec) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = raw_path(spec)
    points: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    try:
        df = pd.read_csv(path, dtype=str, encoding="cp950", header=None).fillna("")
    except Exception:
        df = read_csv_loose(path)
    for _, row in df.iterrows():
        first = str(row.iloc[0]).strip() if len(row) else ""
        if not first.isdigit():
            continue
        sig = first
        name = str(row.iloc[1]).strip() if len(row) > 1 else ""
        district = str(row.iloc[2]).strip() if len(row) > 2 else ""
        week_type = str(row.iloc[3]).strip() if len(row) > 3 else ""
        time_text = str(row.iloc[4]).strip() if len(row) > 4 else ""
        start, end = parse_time_range(time_text)
        phases.append(
            make_phase_row(
                spec,
                signal_id=f"TPE_PED_{sig}",
                intersection_name=name,
                city="臺北市",
                district=district,
                plan_type="行人專用時相",
                time_from=start,
                time_to=end,
                logic=week_type,
                note=f"實施時段原文：{time_text}；來源未提供座標與綠黃全紅秒數。",
            )
        )
    return points, phases


def parse_taipei_timing_csv(spec: SourceSpec) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    df = read_csv_loose(raw_path(spec))
    points = []
    phases = []
    for _, row in df.iterrows():
        sig = str(row.get("設備編號", "") or row.get("deviceid", "") or row.get("icid", "")).strip()
        name = str(row.get("路口名稱", "") or row.get("icname", "")).strip()
        lon = to_num(row.get("經度"))
        lat = to_num(row.get("緯度"))
        road_1, road_2 = split_intersection(name)
        point = make_point_row(
            spec,
            signal_id=f"TPE_TIMING_{sig}" if sig else f"TPE_TIMING_{len(points) + 1}",
            intersection_name=name,
            city="臺北市",
            road_1=road_1,
            road_2=road_2,
            control_type="路口時制號誌",
            lon=lon,
            lat=lat,
            note=f"三合一報表連結：{row.get('三合一報表連結（網址）', '')}",
        )
        if point:
            points.append(point)
        phases.append(
            make_phase_row(
                spec,
                signal_id=point["signal_id"] if point else f"TPE_TIMING_{sig}",
                intersection_name=name,
                city="臺北市",
                plan_type=str(row.get("群組", "") or row.get("segmenttype", "")).strip(),
                logic=f"三合一報表連結：{row.get('三合一報表連結（網址）', '')}",
                update_date=str(row.get("InfoTime", "")).strip(),
                note="公開 CSV 提供路口時制號誌點位與報表連結，未直接提供綠黃全紅秒數。",
                lon=lon,
                lat=lat,
            )
        )
    return points, phases


def flatten_json_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ["data", "records", "result", "features"]:
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def parse_taipei_timing_json(spec: SourceSpec) -> list[dict[str, Any]]:
    if spec.source_id == "TPE_TIMING_TABLE":
        return []
    path = raw_path(spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    records = flatten_json_records(data)
    phases = []
    for rec in records:
        attrs = rec.get("attributes", rec)
        name = str(attrs.get("icname") or attrs.get("路口名稱") or attrs.get("name") or "").strip()
        sig = str(attrs.get("deviceid") or attrs.get("icid") or attrs.get("號誌編號") or "").strip()
        phase_summary = "; ".join(f"{k}={v}" for k, v in list(attrs.items())[:12])
        phases.append(
            make_phase_row(
                spec,
                signal_id=f"TPE_TIMING_{sig}" if sig else f"TPE_TIMING_JSON_{len(phases) + 1}",
                intersection_name=name,
                city="臺北市",
                plan_type=str(attrs.get("segmenttype", "") or attrs.get("時制", "")).strip(),
                phase_no=str(attrs.get("phaseorder", "")).strip(),
                phase_sequence=str(attrs.get("direction", "") or attrs.get("subsegment", "") or attrs.get("時相", "")).strip(),
                cycle_seconds=str(attrs.get("cycletime", "")).strip(),
                update_date=str(attrs.get("InfoTime", "") or attrs.get("更新時間", "")).strip(),
                note=f"JSON 欄位摘要：{phase_summary[:400]}；未可靠解析出綠黃全紅秒數。",
            )
        )
    return phases


def extract_zip_if_needed(path: Path) -> Path:
    if not zipfile.is_zipfile(path):
        return path
    out_dir = path.with_suffix("")
    if not out_dir.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(out_dir)
    return out_dir


def parse_tainan_signal_shp(spec: SourceSpec) -> list[dict[str, Any]]:
    path = raw_path(spec)
    target = extract_zip_if_needed(path)
    candidates = list(target.rglob("*.shp")) if target.is_dir() else [target]
    rows: list[dict[str, Any]] = []
    for shp in candidates:
        try:
            gdf = gpd.read_file(shp)
        except Exception:
            continue
        if gdf.empty:
            continue
        if gdf.crs is None:
            minx, miny, maxx, maxy = gdf.total_bounds
            if 118 <= minx <= 123.8 and 21.5 <= miny <= 26.8 and 118 <= maxx <= 123.8 and 21.5 <= maxy <= 26.8:
                guessed_epsg = 4326
            elif minx > 10000000 and maxx > 10000000:
                guessed_epsg = 3857
            else:
                guessed_epsg = 3826
            gdf = gdf.set_crs(guessed_epsg, allow_override=True)
        original_crs = str(gdf.crs)
        if CRS.from_user_input(gdf.crs).to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
            transformed = "是"
        else:
            transformed = "否"
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            point = geom if geom.geom_type == "Point" else geom.representative_point()
            sig = str(row.get("SignalID", "") or row.get("ID", "") or row.get("識別碼", "")).strip()
            name = repair_text(row.get("Notes", "") or row.get("備註", "") or sig).strip()
            item = make_point_row(
                spec,
                signal_id=f"TNN_{sig}" if sig else f"TNN_{len(rows) + 1}",
                intersection_name=name,
                city="臺南市",
                control_type=repair_text(row.get("SigType", "") or row.get("號誌種類", "")).strip(),
                lon=float(point.x),
                lat=float(point.y),
                update_date=str(row.get("TimePos", "") or row.get("資料時間", "")).strip(),
                coord_system=original_crs,
                transformed=transformed,
                note=f"公共管線號誌圖資；Install={row.get('Install', '')}; UseStat={row.get('UseStat', '')}",
            )
            if item:
                rows.append(item)
    return rows


def parse_tainan_wifi_points(spec: SourceSpec) -> list[dict[str, Any]]:
    df = read_csv_loose(raw_path(spec))
    rows = []
    for _, row in df.iterrows():
        lon = to_num(row.get("熱點經度") or row.get("經度"))
        lat = to_num(row.get("熱點緯度") or row.get("緯度"))
        seq = str(row.get("Seq", "")).strip()
        name = str(row.get("熱點名稱", "") or row.get("地址", "")).strip()
        item = make_point_row(
            spec,
            signal_id=f"TNN_WIFI_{seq}" if seq else f"TNN_WIFI_{len(rows) + 1}",
            intersection_name=name,
            city="臺南市",
            district=str(row.get("行政區", "")).strip(),
            control_type=str(row.get("熱點類別", "") or row.get("熱點分類", "")).strip(),
            lon=lon,
            lat=lat,
            note="交通號誌 4G/WiFi 點位，非完整號誌清冊。",
        )
        if item:
            rows.append(item)
    return rows


def parse_tainan_ped_phase(spec: SourceSpec) -> list[dict[str, Any]]:
    df = read_csv_loose(raw_path(spec))
    phases = []
    for _, row in df.iterrows():
        name = str(row.get("路口", "")).strip()
        phases.append(
            make_phase_row(
                spec,
                signal_id=f"TNN_PED_{row.get('Seq', len(phases) + 1)}",
                intersection_name=name,
                city="臺南市",
                district=str(row.get("區域", "")).strip(),
                plan_type="行人專用時相",
                logic=str(row.get("期間", "")).strip(),
                note=f"時段原文：{row.get('時段', '')}；未提供經緯度與綠黃全紅秒數。",
            )
        )
    return phases


def parse_changhua_points(spec: SourceSpec) -> list[dict[str, Any]]:
    df = read_csv_loose(raw_path(spec))
    rows = []
    for _, row in df.iterrows():
        lon = to_num(row.get("經度"))
        lat = to_num(row.get("緯度"))
        seq = str(row.get("編號", "")).strip()
        name = str(row.get("路口名稱", "")).strip()
        road_1, road_2 = split_intersection(name)
        item = make_point_row(
            spec,
            signal_id=f"CHA_{seq}" if seq else f"CHA_{len(rows) + 1}",
            intersection_name=name,
            city="彰化縣",
            district=str(row.get("地區", "")).strip(),
            road_1=road_1,
            road_2=road_2,
            control_type=str(row.get("號誌種類", "")).strip(),
            lon=lon,
            lat=lat,
            note=f"權責單位：{row.get('權責單位', '')}",
        )
        if item:
            rows.append(item)
    return rows


def parse_penghu_points_phase(spec: SourceSpec) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    df = read_csv_loose(raw_path(spec))
    points: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        lon = to_num(row.get("經度"))
        lat = to_num(row.get("緯度"))
        seq = str(row.get("編號", "")).strip()
        name = str(row.get("位置路口名稱", "") or row.get("GPS", "")).strip()
        road_1 = str(row.get("位置（東西向）", "")).strip()
        road_2 = str(row.get("位置（南北向）", "")).strip()
        point = make_point_row(
            spec,
            signal_id=f"PENGHU_{seq}" if seq else f"PENGHU_{len(points) + 1}",
            intersection_name=name,
            city="澎湖縣",
            road_1=road_1,
            road_2=road_2,
            control_type=str(row.get("號誌別", "")).strip(),
            lon=lon,
            lat=lat,
            note=f"運作情形：{row.get('運作情形', '')}；連鎖情形：{row.get('連鎖情形', '')}",
        )
        if point:
            points.append(point)
        for field in ["運作時段1", "運作時段2", "觀光季運作時段3"]:
            text = str(row.get(field, "")).strip()
            if not text:
                continue
            start, end = parse_time_range(text)
            phases.append(
                make_phase_row(
                    spec,
                    signal_id=point["signal_id"] if point else f"PENGHU_{seq}",
                    intersection_name=name,
                    city="澎湖縣",
                    plan_type=field,
                    time_from=start,
                    time_to=end,
                    phase_sequence=str(row.get("時相數（數量）", "")).strip(),
                    green_seconds=str(row.get("綠燈時制", "")).strip(),
                    yellow_seconds=str(row.get("時制（黃燈）（數量）", "")).strip(),
                    cycle_seconds=str(row.get("週期", "")).strip(),
                    logic=str(row.get("週一至週日運作", "") or row.get("週一至週五運作", "")).strip(),
                    note=f"原始運作時段：{text}；紅燈欄位原文：{row.get('時制（紅燈）（數量）', '')}；未確認為全紅秒數。",
                    lon=lon,
                    lat=lat,
                )
            )
    return points, phases


def load_town_boundaries() -> gpd.GeoDataFrame | None:
    if not TOWN_SHP.exists():
        return None
    try:
        towns = gpd.read_file(TOWN_SHP)
        if towns.crs is None:
            towns = towns.set_crs(3826, allow_override=True)
        return towns.to_crs(4326)
    except Exception:
        return None


def assign_city_district(points: pd.DataFrame, towns: gpd.GeoDataFrame | None) -> pd.DataFrame:
    if towns is None or points.empty:
        return points
    missing = points["city"].astype(str).str.len() == 0
    if not missing.any():
        return points
    gdf = gpd.GeoDataFrame(
        points.loc[missing].copy(),
        geometry=gpd.points_from_xy(points.loc[missing, "longitude"], points.loc[missing, "latitude"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(gdf, towns, how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    county_col = next((col for col in ["COUNTYNAME", "COUNTY", "縣市"] if col in joined.columns), None)
    town_col = next((col for col in ["TOWNNAME", "TOWN", "鄉鎮市區"] if col in joined.columns), None)
    if county_col:
        county = joined[county_col].fillna("").astype(str).map(normalize_city)
        valid = county.str.len() > 0
        points.loc[county.index[valid], "city"] = county[valid]
    if town_col:
        town = joined[town_col].fillna("").astype(str)
        valid = town.str.len() > 0
        points.loc[town.index[valid], "district"] = town[valid]
    return points


def parse_osm_points(spec: SourceSpec) -> list[dict[str, Any]]:
    path = raw_path(spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    rows = []
    for element in data.get("elements", []):
        lon = element.get("lon") or element.get("center", {}).get("lon")
        lat = element.get("lat") or element.get("center", {}).get("lat")
        tags = element.get("tags", {})
        if not in_taiwan(to_num(lon), to_num(lat)):
            continue
        osm_type = element.get("type", "node")
        osm_id = element.get("id")
        name = tags.get("name", "") or tags.get("crossing:name", "") or ""
        control = tags.get("traffic_signals", "") or tags.get("crossing", "")
        rows.append(
            make_point_row(
                spec,
                signal_id=f"OSM_{osm_type}_{osm_id}",
                intersection_name=name,
                city="",
                control_type=control,
                lon=float(lon),
                lat=float(lat),
                note=f"OSM tags: {json.dumps(tags, ensure_ascii=False)[:500]}",
            )
        )
    return [row for row in rows if row]


def parse_kaohsiung_pdf_phase(spec: SourceSpec) -> list[dict[str, Any]]:
    try:
        import fitz
    except Exception:
        return []
    path = raw_path(spec)
    phases = []
    try:
        doc = fitz.open(path)
    except Exception:
        return []
    for page_no, page in enumerate(doc, start=1):
        text = page.get_text("text")
        for line in text.splitlines():
            line = line.strip()
            if not re.match(r"^\d+\s+", line):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            idx = parts[0]
            district = parts[1]
            road_1 = parts[2]
            intersection = " ".join(parts[3:6])
            phases.append(
                make_phase_row(
                    spec,
                    signal_id=f"KHH_{spec.source_id}_{idx}",
                    intersection_name=intersection,
                    city="高雄市",
                    district=district,
                    plan_type=spec.source_name.replace("高雄市", ""),
                    logic=road_1,
                    note=f"PDF 第 {page_no} 頁文字列：{line[:500]}；自 PDF 粗略抽取，未提供座標與秒數欄位。",
                )
            )
    return phases


def parse_unlocated_phase_index(spec: SourceSpec, city: str) -> list[dict[str, Any]]:
    df = read_csv_loose(raw_path(spec))
    phases = []
    for idx, row in df.iterrows():
        name = str(row.get("路口", "") or row.get("路段", "") or row.get("鄉鎮市", "") or "").strip()
        phases.append(
            make_phase_row(
                spec,
                signal_id=f"{spec.source_id}_{idx + 1}",
                intersection_name=name,
                city=city,
                district=str(row.get("鄉鎮市", "") or row.get("區域", "")).strip(),
                plan_type="交通號誌路段及時段",
                logic="; ".join(f"{k}={v}" for k, v in row.items())[:700],
                note="來源未提供可直接產生點位之經緯度；保留於 full CSV，不納入 phase shapefile。",
            )
        )
    return phases


def build_sources_catalog(point_df: pd.DataFrame, phase_df: pd.DataFrame) -> None:
    download_status: dict[str, str] = {}
    log_path = LOG_DIR / "download_log.txt"
    if log_path.exists():
        log = pd.read_csv(log_path, dtype=str).fillna("")
        for sid, group in log.groupby("source_id"):
            ok = group["http_status"].astype(str).str.startswith("2").any()
            download_status[sid] = "成功" if ok else "失敗"
    catalog = pd.DataFrame()
    catalog_path = raw_path(source_by_id("GOVTW_CATALOG"))
    if catalog_path.exists():
        catalog = read_csv_loose(catalog_path)
        if "資料集識別碼" in catalog.columns:
            catalog = catalog.set_index("資料集識別碼", drop=False)
    rows = []
    for spec in SOURCE_SPECS:
        raw = raw_path(spec)
        field_summary = ""
        update_date = ""
        license_text = spec.license
        if not catalog.empty and spec.source_id in DATA_GOV_DATASET_IDS:
            dataset_id = DATA_GOV_DATASET_IDS[spec.source_id]
            if dataset_id in catalog.index:
                item = catalog.loc[dataset_id]
                update_date = str(item.get("詮釋資料更新時間", ""))
                license_text = str(item.get("授權方式", spec.license) or spec.license)
                field_summary = str(item.get("主要欄位說明", ""))
        if raw.exists() and spec.fmt.upper() == "CSV":
            df = read_csv_loose(raw)
            raw_fields = "；".join(map(str, df.columns.tolist()[:40]))
            field_summary = field_summary or raw_fields
        rows.append(
            {
                "來源ID": spec.source_id,
                "來源名稱": spec.source_name,
                "官方機關": spec.agency,
                "網址": spec.url,
                "來源頁面": spec.landing_url,
                "格式": spec.fmt,
                "涵蓋縣市": spec.covered_cities,
                "資料更新日期": update_date,
                "下載日期": TODAY,
                "授權條款": license_text,
                "是否官方": "是" if spec.official else "否",
                "欄位摘要": field_summary,
                "下載狀態": download_status.get(spec.source_id, "未下載或未記錄"),
                "點位輸出筆數": int((point_df["source_id"] == spec.source_id).sum()) if not point_df.empty else 0,
                "時相輸出筆數": int((phase_df["source_id"] == spec.source_id).sum()) if not phase_df.empty else 0,
                "備註": spec.note,
            }
        )
    pd.DataFrame(rows).to_csv(META_DIR / "sources_catalog.csv", index=False, encoding="utf-8-sig")


def build_data_dictionary() -> None:
    rows = []
    point_desc = {
        "SIG_ID": "號誌編號或自建唯一 ID",
        "INT_NAME": "路口名稱",
        "CITY": "縣市",
        "DIST": "行政區",
        "ROAD_1": "主要道路",
        "ROAD_2": "交叉道路或其他道路",
        "CTRL_TYPE": "控制方式或號誌類型",
        "LON": "WGS84 經度",
        "LAT": "WGS84 緯度",
        "SRC_ID": "來源 ID，對應 sources_catalog.csv",
        "SRC_URL": "來源頁面或來源 ID",
        "UPD_DATE": "來源更新日期",
        "DL_DATE": "實際下載日期",
        "OFFICIAL": "官方 / 非官方補充",
        "NOTE": "資料註記",
    }
    phase_desc = {
        "SIG_ID": "號誌編號或自建唯一 ID",
        "INT_NAME": "路口名稱",
        "CITY": "縣市",
        "PLAN_TYPE": "尖峰、離峰、假日、行人專用、其他",
        "TIME_FROM": "開始時間",
        "TIME_TO": "結束時間",
        "PHASE_NO": "相位序號",
        "PHASE_SEQ": "相位順序或相位數",
        "GREEN_S": "綠燈秒數",
        "YELLOW_S": "黃燈秒數",
        "ALLRED_S": "全紅秒數",
        "CYCLE_S": "週期長度",
        "ACTUATED": "是否感應式",
        "LOGIC": "感應式邏輯或原始摘要",
        "SRC_ID": "來源 ID，對應 sources_catalog.csv",
        "UPD_DATE": "來源更新日期",
        "DL_DATE": "實際下載日期",
        "NOTE": "缺漏或不確定性",
    }
    for short, full in POINT_SHP_MAP.items():
        rows.append({"shapefile": "traffic_light_points", "short_field": short, "full_field": full, "description": point_desc.get(short, "")})
    for short, full in PHASE_SHP_MAP.items():
        rows.append({"shapefile": "traffic_light_phase_records", "short_field": short, "full_field": full, "description": phase_desc.get(short, "")})
    pd.DataFrame(rows).to_csv(META_DIR / "data_dictionary.csv", index=False, encoding="utf-8-sig")


def deduplicate_points(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    priority = {"官方": 0, "非官方補充": 1, "推估": 2, "不明": 3}
    df = df.copy()
    df["_priority"] = df["source_type"].map(priority).fillna(9)
    df["_lon_round"] = pd.to_numeric(df["longitude"], errors="coerce").round(6)
    df["_lat_round"] = pd.to_numeric(df["latitude"], errors="coerce").round(6)
    df["_name_key"] = df["intersection_name"].astype(str).str.strip()
    df = df.sort_values(["_priority", "source_update_date", "source_id"], ascending=[True, False, True])
    before = len(df)
    df = df.drop_duplicates(subset=["_lon_round", "_lat_round", "_name_key"], keep="first")
    df["deduplicated"] = "是"
    df.attrs["dedup_removed"] = before - len(df)
    return df.drop(columns=["_priority", "_lon_round", "_lat_round", "_name_key"], errors="ignore")


def write_shapefile(df: pd.DataFrame, mapping: dict[str, str], output: Path) -> None:
    if df.empty:
        gdf = gpd.GeoDataFrame({short: pd.Series(dtype="str") for short in mapping}, geometry=[], crs="EPSG:4326")
    else:
        out = pd.DataFrame()
        for short, full in mapping.items():
            out[short] = df.get(full, "").astype(str).str.slice(0, 240)
        gdf = gpd.GeoDataFrame(
            out,
            geometry=gpd.points_from_xy(pd.to_numeric(df["longitude"], errors="coerce"), pd.to_numeric(df["latitude"], errors="coerce")),
            crs="EPSG:4326",
        )
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()]
    if output.exists():
        for sibling in output.parent.glob(output.stem + ".*"):
            sibling.unlink()
    gdf.to_file(output, driver="ESRI Shapefile", encoding="UTF-8")
    cpg = output.with_suffix(".cpg")
    cpg.write_text("UTF-8", encoding="ascii")


def build_coverage(point_df: pd.DataFrame, phase_df: pd.DataFrame) -> None:
    rows = []
    source_df = pd.read_csv(META_DIR / "sources_catalog.csv", dtype=str).fillna("") if (META_DIR / "sources_catalog.csv").exists() else pd.DataFrame()
    for city in TAIWAN_CITIES:
        p = point_df[point_df["city"] == city] if not point_df.empty else pd.DataFrame()
        ph = phase_df[phase_df["city"] == city] if not phase_df.empty else pd.DataFrame()
        city_sources = set(p.get("source_id", pd.Series(dtype=str)).astype(str)) | set(ph.get("source_id", pd.Series(dtype=str)).astype(str))
        source_count = len([s for s in city_sources if s])
        official_points = int((p.get("source_type", pd.Series(dtype=str)) == "官方").sum()) if not p.empty else 0
        has_points = len(p) > 0
        has_phase = len(ph) > 0
        if not has_points and not has_phase:
            gaps = "未找到可直接下載並產生座標點位或時相表的官方資料；已以 data.gov.tw 全量目錄及網頁搜尋查核。"
            confidence = "無公開資料可用"
        elif has_points and official_points == 0:
            gaps = "僅有非官方 OSM 補充點位；需人工驗證。"
            confidence = "中低"
        elif has_points and not has_phase:
            gaps = "有點位資料，未找到可機器讀取且可連結點位的完整時相秒數。"
            confidence = "中" if official_points else "中低"
        else:
            gaps = "時相資料多為行人專用/早開或部分時段；一般完整相位秒數仍不足。"
            confidence = "中"
        rows.append(
            {
                "縣市": city,
                "是否找到點位資料": "是" if has_points else "否",
                "是否找到時相資料": "是" if has_phase else "否",
                "來源數量": source_count,
                "點位筆數": len(p),
                "官方點位筆數": official_points,
                "非官方補充點位筆數": int((p.get("source_type", pd.Series(dtype=str)) == "非官方補充").sum()) if not p.empty else 0,
                "時相筆數": len(ph),
                "主要來源ID": "；".join(sorted(city_sources)),
                "主要缺漏": gaps,
                "可信度": confidence,
            }
        )
    pd.DataFrame(rows).to_csv(META_DIR / "coverage_report.csv", index=False, encoding="utf-8-sig")


def write_quality_report(point_df: pd.DataFrame, phase_df: pd.DataFrame, dedup_removed: int) -> None:
    official_points = point_df[point_df["source_type"] == "官方"] if not point_df.empty else pd.DataFrame()
    nonofficial_points = point_df[point_df["source_type"] != "官方"] if not point_df.empty else pd.DataFrame()
    out_of_range = 0
    if not point_df.empty:
        lon = pd.to_numeric(point_df["longitude"], errors="coerce")
        lat = pd.to_numeric(point_df["latitude"], errors="coerce")
        out_of_range = int((~((lon >= 118.0) & (lon <= 123.8) & (lat >= 21.5) & (lat <= 26.8))).sum())
    lines = [
        "# 臺灣紅綠燈點位與時相資料品質報告",
        "",
        f"產製日期：{TODAY}",
        "",
        "## 查核方法",
        "",
        "- 以政府資料開放平臺全量 CSV 目錄掃描關鍵字：紅綠燈、號誌、路口號誌、號誌位置、號誌時制、時相、行人專用時相、行人早開。",
        "- 針對可下載來源保留原始檔，並將可解析的 CSV、JSON、Shapefile、PDF 轉為標準欄位。",
        "- 所有輸出點位統一為 WGS84 / EPSG:4326；非 EPSG:4326 的 GIS 圖資使用 GeoPandas 轉換。",
        "- 經緯度限制為臺灣合理範圍：經度 118.0 至 123.8，緯度 21.5 至 26.8。",
        "- 去重採用座標四捨五入至 6 位小數加路口名稱，並以官方來源優先。",
        "",
        "## 統計摘要",
        "",
        f"- 去重後點位筆數：{len(point_df)}",
        f"- 官方點位筆數：{len(official_points)}",
        f"- 非官方補充點位筆數：{len(nonofficial_points)}",
        f"- 時相/時段紀錄筆數：{len(phase_df)}",
        f"- 去重移除筆數：{dedup_removed}",
        f"- 座標合理範圍外筆數：{out_of_range}",
        "",
        "## 重要限制",
        "",
        "- 多數縣市未公開完整號誌時相秒數；本成果不宣稱全臺完整時相。",
        "- 行人專用、行人早開、運作時段等資料不等同一般車流號誌完整相位秒數。",
        "- OpenStreetMap 僅作非官方補充來源，適合做空間候選點，不應直接視為交通主管機關清冊。",
        "- 臺南市公共管線號誌圖資的官方說明指出圖資僅供參考，精確使用前應向主管機關確認。",
        "- 無座標的來源保留於 raw 與 full CSV 或 sources catalog，不強行推估成 Shapefile 點位。",
        "",
        "## 可直接用於交通模擬的程度",
        "",
        "- 可直接作為空間候選點：官方點位資料與已轉換為 EPSG:4326 的 Shapefile。",
        "- 需人工確認後使用：OSM 補充點、公共管線參考圖資、行人專用/早開時段。",
        "- 不足以直接建置完整號誌控制邏輯：缺少一般相位順序、綠黃全紅秒數、感應式邏輯的縣市。",
    ]
    (META_DIR / "quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ensure_dirs()
    points: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    parser_notes: list[dict[str, str]] = []
    for spec in SOURCE_SPECS:
        try:
            print(f"parse_start={spec.source_id}", flush=True)
            if not raw_path(spec).exists():
                parser_notes.append({"source_id": spec.source_id, "status": "missing_raw", "note": str(raw_path(spec))})
                continue
            if spec.parser == "taipei_points":
                points.extend(parse_taipei_points(spec))
            elif spec.parser == "taipei_ped_phase":
                p, ph = parse_taipei_ped_phase(spec)
                points.extend(p)
                phases.extend(ph)
            elif spec.parser == "taipei_timing_csv":
                p, ph = parse_taipei_timing_csv(spec)
                points.extend(p)
                phases.extend(ph)
            elif spec.parser == "taipei_timing_json":
                phases.extend(parse_taipei_timing_json(spec))
            elif spec.parser == "tainan_signal_shp":
                points.extend(parse_tainan_signal_shp(spec))
            elif spec.parser == "tainan_wifi_points":
                points.extend(parse_tainan_wifi_points(spec))
            elif spec.parser == "tainan_ped_phase":
                phases.extend(parse_tainan_ped_phase(spec))
            elif spec.parser == "changhua_points":
                points.extend(parse_changhua_points(spec))
            elif spec.parser == "penghu_points_phase":
                p, ph = parse_penghu_points_phase(spec)
                points.extend(p)
                phases.extend(ph)
            elif spec.parser == "osm_traffic_signals":
                points.extend(parse_osm_points(spec))
            elif spec.parser == "kaohsiung_pdf_phase":
                phases.extend(parse_kaohsiung_pdf_phase(spec))
            elif spec.parser == "chiayi_county_phase_index":
                phases.extend(parse_unlocated_phase_index(spec, "嘉義縣"))
            elif spec.parser == "taichung_signal_box":
                parser_notes.append({"source_id": spec.source_id, "status": "not_geocoded", "note": "無座標，保留 raw。"})
            elif spec.parser == "taichung_gis_index":
                parser_notes.append({"source_id": spec.source_id, "status": "index_only", "note": "索引型資料，連結檔案保留 raw/linked。"})
            print(f"parse_done={spec.source_id} points={len(points)} phases={len(phases)}", flush=True)
        except Exception as exc:
            parser_notes.append({"source_id": spec.source_id, "status": "parse_error", "note": repr(exc)})
            print(f"parse_error={spec.source_id} {exc!r}", flush=True)
    point_df = pd.DataFrame(points, columns=POINT_COLUMNS).fillna("")
    print(f"point_rows_before_city_join={len(point_df)}", flush=True)
    if not point_df.empty:
        point_df = assign_city_district(point_df, load_town_boundaries())
    print("city_join_done", flush=True)
    before_dedup = len(point_df)
    point_df = deduplicate_points(point_df)
    dedup_removed = int(point_df.attrs.get("dedup_removed", before_dedup - len(point_df)))
    print(f"dedup_done points={len(point_df)} removed={dedup_removed}", flush=True)
    phase_df = pd.DataFrame(phases, columns=PHASE_COLUMNS).fillna("")
    if not phase_df.empty and not point_df.empty:
        coord_lookup = (
            point_df.drop_duplicates("signal_id")
            .set_index("signal_id")[["longitude", "latitude", "district"]]
            .rename(columns={"longitude": "longitude_point", "latitude": "latitude_point", "district": "district_point"})
        )
        joined = phase_df[["signal_id"]].join(coord_lookup, on="signal_id")
        missing_lon = phase_df["longitude"].astype(str).str.strip().eq("") & joined["longitude_point"].notna()
        phase_df.loc[missing_lon, "longitude"] = joined.loc[missing_lon, "longitude_point"].astype(str)
        phase_df.loc[missing_lon, "latitude"] = joined.loc[missing_lon, "latitude_point"].astype(str)
        missing_dist = phase_df["district"].astype(str).str.strip().eq("") & joined["district_point"].notna()
        phase_df.loc[missing_dist, "district"] = joined.loc[missing_dist, "district_point"].astype(str)
    print(f"phase_join_done phases={len(phase_df)}", flush=True)
    point_df.to_csv(PROCESSED_DIR / "traffic_light_points_full.csv", index=False, encoding="utf-8-sig")
    phase_df.to_csv(PROCESSED_DIR / "traffic_light_phase_records_full.csv", index=False, encoding="utf-8-sig")
    print("csv_written", flush=True)
    phase_geo = phase_df.copy()
    if not phase_geo.empty:
        lon = pd.to_numeric(phase_geo["longitude"], errors="coerce")
        lat = pd.to_numeric(phase_geo["latitude"], errors="coerce")
        phase_geo = phase_geo[(lon >= 118.0) & (lon <= 123.8) & (lat >= 21.5) & (lat <= 26.8)].copy()
    write_shapefile(point_df, POINT_SHP_MAP, SHP_DIR / "traffic_light_points.shp")
    print("point_shapefile_written", flush=True)
    write_shapefile(phase_geo, PHASE_SHP_MAP, SHP_DIR / "traffic_light_phase_records.shp")
    print("phase_shapefile_written", flush=True)
    build_sources_catalog(point_df, phase_df)
    build_data_dictionary()
    build_coverage(point_df, phase_df)
    write_quality_report(point_df, phase_df, dedup_removed)
    if parser_notes:
        pd.DataFrame(parser_notes).to_csv(LOG_DIR / "parser_notes.csv", index=False, encoding="utf-8-sig")
    print(f"points={len(point_df)}")
    print(f"phase_records={len(phase_df)}")
    print(f"phase_records_with_geometry={len(phase_geo)}")
    print(f"dedup_removed={dedup_removed}")
    print(f"output={ROOT}")


if __name__ == "__main__":
    main()
