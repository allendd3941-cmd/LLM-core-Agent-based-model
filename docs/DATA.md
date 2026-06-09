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
