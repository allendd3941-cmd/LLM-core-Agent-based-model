# Data and Output Policy

This document explains how data and generated outputs should be handled before publishing this repository.

## GIS Data

`data/gis/` contains spatial input data used by the traffic simulation.

These files are not automatically covered by the repository MIT License. They should be treated as external data assets and remain subject to their original data source licenses.

Before publishing, confirm and document:

- Original data source.
- License or open-data terms.
- Coordinate reference system.
- Preprocessing steps, if any.
- How each layer is used by the simulator.

Recommended README note:

```text
The GIS files in `data/gis/` are used as spatial inputs for the traffic ABM model. Please verify the original data source and license terms before reuse.
```

If the GIS files are from Taiwan government open data sources, document the source URL and attribution requirements, and reference the applicable Open Government Data License where appropriate.

## Township Population Data

`data/gis/town_population.csv` (`town_name,population`) feeds the gravity-model demand generation
that assigns each agent's origin township (see `docs/DEMAND_zh-TW.md`).

- **Current values are approximate (~2023 magnitude)** — fine for the demo and the model's *relative*
  weighting, but **replace with official figures (MOI 內政部戶政司 / 臺南市民政局 monthly report) before
  publishing the paper** (same `town_name,population` format; no code change needed).
- If the file is missing or all populations are 0, demand generation is skipped and the existing
  origin assignment is kept.

## Road Network (full Tainan City)

`data/tainan_roads.graphml` is the OSM **drive** network for the **whole Tainan City** (all 37
districts, ≈15.8k nodes / 42.5k edges, ≈24 MB), built by clipping the OSMnx download to the TOWN_MOI
county boundary (`gis_loader.load_county_boundary_wgs84`).

- **Git-ignored** (too large to track). It is **auto-built on first run** (`road_network.load_road_network`
  → OSMnx download → save) — `osmnx` is a base dependency (plain `uv sync` / `pip install -r requirements.txt`),
  so the machine running the app only needs network access the first time. To force a rebuild: delete the file
  (or `python -m llm_abm_simulator.spatial.build_roads`).
- For an offline machine, build it elsewhere and copy `data/tainan_roads.graphml` over.
- The earlier study-area-only network (`亞太棒球場_研究範圍.shp`) has been removed; coverage is now the full county.

### Speed limit, lanes, and capacity

Per-edge attributes are derived at build time from OSM and `config/simulation.toml` `[highway_specs]`:

- **Lanes** — read from the OSM `lanes` tag when present (two-way streets split per direction ≈ `lanes/2`),
  falling back to the highway-class default when absent. **OSM lane coverage is partial**, so many edges use
  the class default — document this as an approximation, not surveyed lane counts.
- **Speed** — read from the OSM `maxspeed` tag when present (mph converted to km/h), else the class estimate.
  Motorcycle speed keeps the class car/moto ratio. Also partial coverage.
- **Capacity** — a *proxy* denominator for `congestion_proxy = flow/capacity`, computed **at load time** as
  `lanes × capacity_per_lane[class]` (so `capacity_per_lane` can be tuned in the TOML without rebuilding).
  `capacity_per_lane` is monotonic by class and deliberately small so congestion is visible in the demo.
  **Not an HCM-calibrated capacity (veh/hr)** — state this plainly in the paper.

> `lanes` only feeds the capacity formula; the map renders each road as a single centerline polyline
> (no per-lane geometry).

## Analysis Exports (GIS shapefiles + detectors)

After a run, the demo can export **thematic GIS layers** (for QGIS/ArcGIS, e.g. a city traffic bureau):
road **service level (LOS, A–F)**, **volume** (cumulative passages by car/moto/event/ambient), and
**congestion** (peak proxy), plus a **detector** point layer. These are produced by
`engine.gis_road_records()` / `gis_detector_records()` → `spatial/gis_export.py` (geopandas
`to_file(driver="ESRI Shapefile")`), zipped and served via `GET /api/gis/<name>`.

- **CRS** EPSG:4326 (with `.prj`/`.cpg`); field names ≤10 chars (DBF limit).
- **Detectors** are default-bundled (the 55 validation cameras, see below) and/or user-placed, on-road
  (snapped) **passive counters** — they count vehicle **passages** by replaying every edge each vehicle
  traverses per step (independent of `step_minutes`), broken down by vehicle type × role; they do not
  affect the simulation (deterministic, reproducible).
- These are runtime artifacts (under `output/`, git-ignored); not versioned.

## Scenario Bundles

`data/scenarios/` holds swappable-scenario bundles produced by
`python -m llm_abm_simulator.spatial.build_scenario` — each is a `<key>.json` manifest plus a
`<key>_roads.graphml`. Treat the road graphml like GIS data (OSM-derived); large city networks can be
big — consider Git LFS or gitignore for very large ones. The built-in default (Tainan stadium) uses
the bundled `data/tainan_roads.graphml`; no manifest needed. See `docs/DEMO_FEATURES_zh-TW.md`.

## Traffic Signal Data

`data/traffic_light/` holds traffic-signal data sourced from Taiwan government open data
(e.g. data.gov.tw). It is treated like GIS data above — verify source URL, attribution, and the
applicable Open Government Data License before reuse.

Repository policy for this folder:

- **Committed (small, needed at runtime):** `data/tainan_signals.json` — the derived signal artifact
  (signalised network nodes + phase axis + offset), built by `python -m llm_abm_simulator.spatial.build_signals`.
- **Ignored (large raw/intermediate, not needed at runtime):** `raw/`, `processed/`, and the source
  `shapefile/` (the `.dbf` files exceed GitHub's 100 MB limit). Keep these locally to rebuild the artifact.
- **Caveat:** Tainan has signal **point locations but no real phase timing** (timing exists only for
  Taipei/Penghu and does not join by ID). The simulation therefore uses **synthetic** cycle/yellow
  values from `[signals]`; this is documented in `spatial/signals.py` and must not be presented as a
  digital twin of real signal timing.

## Validation Cameras (default detectors)

`data/validation_cameras.csv` (`device_group_id,camera_name,lon,lat,distance_to_stadium_km`) bundles the
**55 iTraView intersection cameras within 5 km of the stadium** — the set used for case-based validation
against real traffic counts. `device_group_id` is the camera UUID and matches `device_group_id` in the
observed report CSVs, i.e. the join key for pairing each real camera with its simulated counterpart.

- Loaded by `gis_loader.load_default_detectors()` → `[{lat, lng, ext_id, ext_name}, …]` and set as the
  **default detectors** on every web session (`SimulationSession`), so the demo monitors these 55 points
  out of the box. Each registered detector carries `ext_id` (the camera UUID) so its simulated passage
  counts map back to the matching real camera.
- Manual place/clear in the frontend still works; applying frontend detectors overrides the default set.
- Derived from the validation handoff's `tainan_devices_fin.csv` (filtered to `distance_to_stadium_km ≤ 5`,
  deduped by id; `lon`=`locationWgsX`, `lat`=`locationWgsY`). If the file is missing, sessions start with no
  detectors (prior behavior). Cameras farther than the snap threshold from any road are skipped at
  registration, so the active count can be slightly below 55.

**Exporting validation CSVs.** After running a scenario in the demo, the "匯出驗證 CSV" control
(`export_validation` WS action → `engine.export_validation_csv(case)`) writes a zip containing
`<case>_gameday.csv` (per-camera 5-minute passage counts, keyed by `device_group_id`=UUID),
`<case>_nogameday.csv` (all zeros — the model produces no event traffic on a non-game day), and
`<case>_run_params.csv` (the run's parameters, for paper annotation). `case`∈{weekend,weekday} sets the
clock window (14:00 / 16:30) and filename that the validation script expects; the simulation itself only
needs to cover two hours (relative time is mapped to the window at export). Run weekend-game and
weekday-game once each, unzip the four CSVs into the validation handoff's `simulation_result/`, and run
`python main.py --max-distance-km 5`.

Each gameday/nogameday row carries **three flow columns**:
`doc_count` = **event-vehicle** flow (unchanged; the validation script reads this for game−nogame impact),
`total_count` = **all-vehicle** flow (event + background), and `background_count` = **background-vehicle**
flow (= `total_count − doc_count`). Every row satisfies `total_count == doc_count + background_count`.
The first six columns (`camera_name,device_group_id,stream_id,time_start,doc_count,avg_speed`) keep their
names and order; `total_count`/`background_count` are appended at the end, so the existing validation
pipeline is unaffected.

## Generated Outputs

`output/` contains generated local runtime artifacts from LLM calls. These files are useful during experimentation but should not be versioned as source code.

Recommended policy:

- Keep `output/` locally for debugging and analysis.
- Ignore `output/` in Git.
- Do not commit sensitive prompts, API responses, or local-only runtime logs.

## Repository License Boundary

- Source code, documentation, prompts, and curated examples: MIT License.
- GIS files: original data source license.
- Local generated outputs under `output/`: not versioned.
- Third-party dependencies: each package keeps its own license.
