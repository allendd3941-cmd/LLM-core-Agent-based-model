"""Convert travel plans written as natural language or JSON into OD CSV rows."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


OD_COLUMNS = [
    "trip_id",
    "leg_id",
    "origin",
    "destination",
    "departure_time",
    "arrival_time",
    "mode",
    "total_distance_km",
    "estimated_duration_minutes",
    "position_at_5_min_km",
    "date",
    "raw_text",
]

TIME_PATTERN = r"(?:上午|早上|中午|下午|晚上|凌晨)?\s*\d{1,2}\s*(?::|：|點|点)\s*\d{0,2}\s*分?"
LEG_SPLIT_PATTERN = re.compile(r"(?:\r?\n|；|;|，然後|然後|接著|再)")

KEY_ALIASES = {
    "trip_id": ("trip_id", "tripId", "行程id", "行程ID", "行程編號"),
    "leg_id": ("leg_id", "legId", "段次", "路段", "序號"),
    "origin": ("origin", "from", "start", "departure", "出發點", "起點", "出發地"),
    "destination": ("destination", "to", "end", "arrival", "抵達點", "終點", "目的地"),
    "departure_time": ("departure_time", "depart_time", "start_time", "出發時間", "出發"),
    "arrival_time": ("arrival_time", "arrive_time", "end_time", "抵達時間", "抵達"),
    "mode": (
        "mode",
        "transport",
        "transportation",
        "vehicle_type",
        "vehicleType",
        "車種",
        "交通方式",
        "運具",
        "方式",
    ),
    "total_distance_km": (
        "total_distance_km",
        "distance_km",
        "distance",
        "總路徑長公里",
        "路徑長公里",
        "總距離公里",
        "距離公里",
    ),
    "date": ("date", "日期", "出行日期"),
}

DEFAULT_SPEED_KMH = {
    "步行": 4.5,
    "走路": 4.5,
    "自行車": 15,
    "腳踏車": 15,
    "機車": 35,
    "電動機車": 30,
    "小客車": 50,
    "汽車": 50,
    "開車": 50,
    "計程車": 45,
    "公車": 28,
    "客運": 45,
    "捷運": 35,
    "火車": 55,
    "高鐵": 160,
}

DEFAULT_DURATION_MINUTES = {
    "步行": 30,
    "走路": 30,
    "自行車": 25,
    "腳踏車": 25,
    "機車": 20,
    "電動機車": 20,
    "小客車": 40,
    "汽車": 40,
    "開車": 40,
    "計程車": 35,
    "公車": 50,
    "客運": 60,
    "捷運": 35,
    "火車": 60,
    "高鐵": 45,
}
FALLBACK_DURATION_MINUTES = 40
FALLBACK_SPEED_KMH = 40
POSITION_MINUTE = 5


def convert_to_od_csv(source: str | Path, output_csv: str | Path) -> list[dict[str, str]]:
    """Read natural-language text or a JSON file/string and write OD data to CSV."""

    content, source_type = _read_source(source)
    rows = parse_json_to_od(content) if source_type == "json" else parse_text_to_od(content)
    
    write_od_csv(rows, output_csv)
    return rows


def parse_json_to_od(json_content: str | dict[str, Any] | list[Any]) -> list[dict[str, str]]:
    """Normalize JSON itinerary data into OD rows."""

    data = json.loads(json_content) if isinstance(json_content, str) else json_content
    records = _extract_records(data)
    rows = []

    for index, record in enumerate(records, start=1):
        normalized = _normalize_record(record)
        normalized["trip_id"] = normalized["trip_id"] or "1"
        normalized["leg_id"] = normalized["leg_id"] or str(index)
        _fill_estimates(normalized)
        rows.append(_ordered_row(normalized))

    return rows


def parse_text_to_od(text: str) -> list[dict[str, str]]:
    """Parse natural-language itinerary text into OD rows."""

    chunks = [part.strip() for part in LEG_SPLIT_PATTERN.split(text) if part.strip()]
    rows: list[dict[str, str]] = []

    for chunk in chunks:
        row = _parse_text_chunk(chunk)
        if row["origin"] or row["destination"] or row["departure_time"] or row["arrival_time"]:
            row["trip_id"] = "1"
            row["leg_id"] = str(len(rows) + 1)
            _fill_estimates(row)
            rows.append(_ordered_row(row))

    if not rows:
        rows.append(_ordered_row({"trip_id": "1", "leg_id": "1", "raw_text": text.strip()}))

    return rows


def write_od_csv(rows: Iterable[dict[str, Any]], output_csv: str | Path) -> None:
    """Write OD rows using UTF-8 with BOM so Excel opens Chinese text correctly."""

    output_path = Path(output_csv)
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=OD_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_ordered_row(row))


def _read_source(source: str | Path) -> tuple[str, str]:
    source_text = str(source)
    source_path = Path(source_text)

    if source_path.exists():
        content = source_path.read_text(encoding="utf-8-sig")
        if source_path.suffix.lower() == ".json":
            return content, "json"
        return content, "text"

    stripped = source_text.strip()
    if stripped.startswith(("{", "[")):
        return stripped, "json"
    return source_text, "text"


def _extract_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if not isinstance(data, dict):
        raise ValueError("JSON content must be an object or an array of objects.")

    for key in ("trips", "legs", "routes", "segments", "行程", "路段"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    return [data]


def _normalize_record(record: dict[str, Any]) -> dict[str, str]:
    row = {column: "" for column in OD_COLUMNS}

    for column, aliases in KEY_ALIASES.items():
        for alias in aliases:
            if alias in record and record[alias] is not None:
                row[column] = str(record[alias]).strip()
                break

    row["departure_time"] = _normalize_time(row["departure_time"])
    row["arrival_time"] = _normalize_time(row["arrival_time"])
    row["total_distance_km"] = _normalize_number(row["total_distance_km"])
    row["raw_text"] = str(record.get("raw_text", record.get("原文", ""))).strip()
    return row


def _parse_text_chunk(chunk: str) -> dict[str, str]:
    row = {column: "" for column in OD_COLUMNS}
    row["raw_text"] = chunk
    row["departure_time"] = _find_time_near(chunk, ("出發", "離開", "啟程"))
    row["arrival_time"] = _find_time_near(chunk, ("抵達", "到達", "到"))

    times = [_normalize_time(match.group()) for match in re.finditer(TIME_PATTERN, chunk)]
    if not row["departure_time"] and times:
        row["departure_time"] = times[0]
    if not row["arrival_time"] and len(times) >= 2:
        row["arrival_time"] = times[1]

    row["origin"] = _first_match(
        chunk,
        (
            r"(?:從|自|由)\s*(?P<value>.+?)\s*(?:出發|離開|啟程|前往|到|至)",
            r"(?:出發點|起點|出發地)\s*(?:是|為|:|：)?\s*(?P<value>.+?)(?:，|,|。|$)",
        ),
    )
    row["destination"] = _first_match(
        chunk,
        (
            r"(?:前往|到|至)\s*(?P<value>.+?)\s*(?:抵達|到達|，|,|。|$)",
            r"(?:抵達|到達)\s*(?P<value>.+?)(?:，|,|。|$)",
            r"(?:目的地|終點|抵達點)\s*(?:是|為|:|：)?\s*(?P<value>.+?)(?:，|,|。|$)",
        ),
    )
    row["mode"] = _first_match(
        chunk,
        (
            r"(?:搭乘|搭|乘坐|坐|開車|騎車|使用)\s*(?P<value>公車|捷運|火車|高鐵|客運|計程車|汽車|小客車|機車|電動機車|自行車|腳踏車|步行|走路|uber|Uber|UBER)",
            r"(?:交通方式|運具|方式|車種)\s*(?:是|為|:|：)?\s*(?P<value>.+?)(?:，|,|。|$)",
        ),
    )
    row["total_distance_km"] = _normalize_number(
        _first_match(
            chunk,
            (
                r"(?:總路徑長|路徑長|總距離|距離)\s*(?:是|為|:|：)?\s*(?P<value>\d+(?:\.\d+)?)\s*(?:公里|km|KM)?",
            ),
        )
    )

    row["origin"] = _clean_place(row["origin"])
    row["destination"] = _clean_place(row["destination"])
    return row


def _find_time_near(text: str, keywords: tuple[str, ...]) -> str:
    for keyword in keywords:
        before = re.search(rf"(?P<time>{TIME_PATTERN})\D{{0,8}}{keyword}", text)
        if before:
            return _normalize_time(before.group("time"))

        after = re.search(rf"{keyword}\D{{0,6}}(?P<time>{TIME_PATTERN})", text)
        if after:
            return _normalize_time(after.group("time"))

    return ""


def _first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group("value").strip()
    return ""


def _clean_place(value: str) -> str:
    return re.sub(rf"^\s*(?:{TIME_PATTERN})\s*", "", value).strip(" ，,。")


def _fill_estimates(row: dict[str, str]) -> None:
    duration = _estimate_duration_minutes(row)
    if duration is not None:
        row["estimated_duration_minutes"] = str(duration)

    if not row["arrival_time"] and row["departure_time"] and duration is not None:
        departure = datetime.strptime(row["departure_time"], "%H:%M")
        row["arrival_time"] = (departure + timedelta(minutes=duration)).strftime("%H:%M")

    row["position_at_5_min_km"] = _estimate_position_at_minute(row, POSITION_MINUTE)


def _estimate_duration_minutes(row: dict[str, str]) -> int | None:
    distance = _to_float(row["total_distance_km"])
    if distance is not None and distance > 0:
        speed = _speed_kmh_for_mode(row["mode"])
        return max(1, round(distance / speed * 60))

    if row["arrival_time"] and row["departure_time"]:
        return _minutes_between(row["departure_time"], row["arrival_time"])

    if row["departure_time"]:
        return _duration_minutes_for_mode(row["mode"])

    return None


def _estimate_position_at_minute(row: dict[str, str], minute: int) -> str:
    distance = _to_float(row["total_distance_km"])
    duration = _to_float(row["estimated_duration_minutes"])
    if distance is None or duration is None or distance <= 0 or duration <= 0:
        return ""

    position = min(distance, distance * minute / duration)
    return _format_number(position)


def _minutes_between(start_time: str, end_time: str) -> int:
    start = datetime.strptime(start_time, "%H:%M")
    end = datetime.strptime(end_time, "%H:%M")
    if end < start:
        end += timedelta(days=1)
    return max(1, round((end - start).total_seconds() / 60))


def _duration_minutes_for_mode(mode: str) -> int:
    normalized_mode = mode.strip()
    for keyword, minutes in DEFAULT_DURATION_MINUTES.items():
        if keyword.lower() in normalized_mode.lower():
            return minutes
    return FALLBACK_DURATION_MINUTES


def _speed_kmh_for_mode(mode: str) -> float:
    normalized_mode = mode.strip()
    for keyword, speed in DEFAULT_SPEED_KMH.items():
        if keyword.lower() in normalized_mode.lower():
            return speed
    return FALLBACK_SPEED_KMH


def _normalize_time(value: str) -> str:
    if not value:
        return ""

    text = re.sub(r"\s+", "", value)
    period = ""
    for marker in ("上午", "早上", "中午", "下午", "晚上", "凌晨"):
        if text.startswith(marker):
            period = marker
            text = text.removeprefix(marker)
            break

    match = re.match(r"(?P<hour>\d{1,2})(?::|：|點|点)(?P<minute>\d{0,2})分?$", text)
    if not match:
        return value.strip()

    hour = int(match.group("hour"))
    minute_text = match.group("minute") or "00"
    minute = int(minute_text)

    if period in ("下午", "晚上") and hour < 12:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0

    return f"{hour:02d}:{minute:02d}"


def _normalize_number(value: str) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return _format_number(number)


def _to_float(value: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None

    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group())


def _format_number(value: float) -> str:
    rounded = round(value, 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _ordered_row(row: dict[str, Any]) -> dict[str, str]:
    return {column: str(row.get(column, "") or "") for column in OD_COLUMNS}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Convert itinerary text or JSON to OD CSV.")
    parser.add_argument("source", help="Natural-language text, JSON string, or .txt/.json path")
    parser.add_argument("output_csv", help="Output CSV path")
    args = parser.parse_args()

    converted_rows = convert_to_od_csv(args.source, args.output_csv)
    print(f"Wrote {len(converted_rows)} OD row(s) to {args.output_csv}")
