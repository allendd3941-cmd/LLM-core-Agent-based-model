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
