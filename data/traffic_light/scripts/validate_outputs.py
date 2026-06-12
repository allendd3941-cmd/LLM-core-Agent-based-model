from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "requirements.txt",
    "scripts/discover_sources.py",
    "scripts/download_raw_data.py",
    "scripts/build_shapefiles.py",
    "scripts/validate_outputs.py",
    "processed/traffic_light_points_full.csv",
    "processed/traffic_light_phase_records_full.csv",
    "metadata/sources_catalog.csv",
    "metadata/data_dictionary.csv",
    "metadata/coverage_report.csv",
    "metadata/quality_report.md",
    "logs/download_log.txt",
]
SHP_REQUIRED = [
    "shapefile/traffic_light_points.shp",
    "shapefile/traffic_light_points.shx",
    "shapefile/traffic_light_points.dbf",
    "shapefile/traffic_light_points.prj",
    "shapefile/traffic_light_points.cpg",
    "shapefile/traffic_light_phase_records.shp",
    "shapefile/traffic_light_phase_records.shx",
    "shapefile/traffic_light_phase_records.dbf",
    "shapefile/traffic_light_phase_records.prj",
    "shapefile/traffic_light_phase_records.cpg",
]


def check_required() -> list[str]:
    issues = []
    for rel in REQUIRED + SHP_REQUIRED:
        path = ROOT / rel
        if not path.exists():
            issues.append(f"missing: {rel}")
    return issues


def check_points() -> list[str]:
    issues = []
    points_path = ROOT / "processed" / "traffic_light_points_full.csv"
    sources_path = ROOT / "metadata" / "sources_catalog.csv"
    if not points_path.exists() or not sources_path.exists():
        return ["points or sources missing"]
    points = pd.read_csv(points_path, dtype=str).fillna("")
    sources = pd.read_csv(sources_path, dtype=str).fillna("")
    source_ids = set(sources["來源ID"].astype(str))
    missing_src = sorted(set(points["source_id"].astype(str)) - source_ids)
    if missing_src:
        issues.append(f"source_id not in catalog: {missing_src}")
    lon = pd.to_numeric(points["longitude"], errors="coerce")
    lat = pd.to_numeric(points["latitude"], errors="coerce")
    bad = points[~((lon >= 118.0) & (lon <= 123.8) & (lat >= 21.5) & (lat <= 26.8))]
    if len(bad):
        issues.append(f"points out of Taiwan bounds: {len(bad)}")
    if points.empty:
        issues.append("points csv is empty")
    return issues


def check_shapefile() -> list[str]:
    issues = []
    for rel in ["shapefile/traffic_light_points.shp", "shapefile/traffic_light_phase_records.shp"]:
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:
            issues.append(f"cannot read {rel}: {exc!r}")
            continue
        epsg = gdf.crs.to_epsg() if gdf.crs else None
        if epsg != 4326:
            issues.append(f"{rel} crs is {gdf.crs}, expected EPSG:4326")
    return issues


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    issues = []
    issues.extend(check_required())
    issues.extend(check_points())
    issues.extend(check_shapefile())
    report = ROOT / "metadata" / "validation_report.txt"
    if issues:
        report.write_text("\n".join(["FAIL"] + issues) + "\n", encoding="utf-8")
        print("validation=FAIL")
        for issue in issues:
            print(issue)
        raise SystemExit(1)
    report.write_text("PASS\n", encoding="utf-8")
    print("validation=PASS")
    print(f"report={report}")


if __name__ == "__main__":
    main()

