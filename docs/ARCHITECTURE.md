# Architecture

High-level architecture of `LLM_abm_model` — a Python-native, LLM-driven microscopic traffic ABM
on a real OSM road network, with an interactive web demo.

> For the parameter reference and run instructions see
> [`PYTHON_SIMULATOR_zh-TW.md`](PYTHON_SIMULATOR_zh-TW.md); for the demo overview see the top-level
> [`README.md`](../README.md).

## System role

The **`llm_abm_simulator`** package owns the entire simulation: GIS/road network, vehicle agents,
movement, congestion, perception, memory, metrics, and the web layer. Behavioural decisions come
from a pluggable, user-selectable **decision core** (registry in `decisions/registry.py`) — either a
deterministic **rule-based** policy (key `rule`) or an **LLM** policy (key `llm`) that calls the
**`llm_server`** pipeline **in-process** (which in turn calls a local Ollama/vLLM model). Agents carry a
`role`: **event** cars head to the venue (may use either core); **ambient** cars are background everyday
traffic (always rule-based, no memory) — see `docs/AMBIENT_zh-TW.md`.

There is **no GAMA and no HTTP hop** in the simulation loop. The project originated as a GAMA +
FastAPI (`/from-gama`) prototype; that standalone HTTP server has since been removed in favour of
the in-process pipeline described here.

## Module responsibilities

### `llm_abm_simulator` (the simulator)

| Layer / module | Responsibility |
| --- | --- |
| `domain/` | Pure data models + state transitions: `agent` (state, active-mode, `role`, single trip `memory`, payloads), `road` (flow → congestion → dynamic weight), `town`, `state` (output snapshots), `events`. No I/O, unit-testable. |
| `spatial/` | `road_network` (OSM `graphml` → directed graph + `Road` objects), `routing` (weighted shortest path / Dijkstra + dynamic congestion weight), `gis_loader`, `geojson`, `build_roads`. |
| `mobility/` | `demand` — gravity model: event-car origins (population × distance-to-venue) and ambient OD pairs (double-ended gravity over town pairs). |
| `decisions/` | `registry` (named selectable cores), `base` (the `DecisionPolicy` protocol), `mock_policy` (rule-based core), `llm_adapter` (in-process call into `llm_server`), `response_parser` (robust LLM-JSON → rows), `profile_pool` (stable persona pool + slicing). |
| `simulation/` | `engine` (owns state, the per-step loop, ambient generation/respawn, perception features, hotspots/trend, on-decision memory summary, two-layer analysis, snapshots), `scheduler`, `metrics`, `random_seed`. |
| `web/` | `app` (FastAPI: serves the frontend, `/ws`, decision-output routes), `websocket` (one `SimulationSession` per connection, driving `engine.step` off-thread). |
| `config.py` | Typed config schema + `config/simulation.toml` loader (single source of truth for all tunables). |

### `llm_server` (the LLM pipeline, called in-process)

| Module | Responsibility |
| --- | --- |
| `agent_profile.py` | Generate agent personas (identity / traits) via the LLM. |
| `perception.py` | **Deterministic template (no LLM call)** — reformats the simulator's structured, qualitative state into a compact per-agent + global summary for decision-making. |
| `decision_making.py` | Build the decision prompt and call the LLM with a structured-output schema (`DECISION_SCHEMA`); returns each agent's active mode + `reason`. The only LLM call per batch. |
| `memory_summary.py` | Optional small-model summariser for the long-term `trip_summary`. |
| `json_utils.py` | Robust LLM-JSON salvage (handles trailing commas, fences, truncated arrays). |
| `llm_client.py` | Unified Ollama/vLLM transport (`generate()`); structured output via Ollama `format` / vLLM `guided_json`. |
| `llm_config.py` / `model_registry.py` | Backend/model connection settings from `.env`; vLLM candidate registry. |
| `prompts/` | Prompt templates (system / decision-making / agent-profile / memory). |
| `rag_store.py` | Lightweight TF-IDF retrieval — **opt-in** (toggled via `/api/rag`); when enabled, `decision_making` retrieves a few chunks per batch and injects them into the decision prompt. |
| `sim_chat.py` / `sim_intervene.py` | Pause-time read-only Q&A; constrained NL intervention parsing. |

## Per-step data flow

```mermaid
sequenceDiagram
    participant UI as Web demo
    participant WS as web/ session
    participant ENG as SimulationEngine
    participant DEC as Decision core (Rule | LLM)
    participant PIPE as llm_server pipeline
    participant LLM as Ollama

    UI->>WS: control (start / step / set_agents …) via WebSocket
    loop each simulation step
        ENG->>ENG: perceive (speed limit, congestion, neighbours)
        ENG->>DEC: decide active mode (+ vehicle type, reason)
        alt LLM policy
            DEC->>PIPE: in-process call (persona + deterministic perception → decision LLM)
            PIPE->>LLM: prompt
            LLM-->>PIPE: raw text
            PIPE-->>DEC: decision text → response_parser
        else Rule-based core / LLM unavailable (and all ambient cars)
            DEC-->>ENG: deterministic rule-based decision
        end
        ENG->>ENG: move along weighted path (reroute if stuck); respawn arrived ambient cars
        ENG->>ENG: recompute road flow / congestion / weights (event + ambient)
        ENG->>ENG: update metrics (event KPIs + network layer), single trip memory, snapshot
        ENG-->>WS: state_update
        WS-->>UI: render (map, agents, roads, charts)
    end
```

## Decision cores: Rule-based vs. LLM

A user-selectable **decision core** drives the **event** cars (registry: `decisions/registry.py`).
**Ambient** cars always use the rule-based core (no LLM, no memory).

- **Rule-based** (`rule`, default): `mock_policy` picks an active mode per agent from deterministic rules
  (congestion / distance / vehicle type) and supplies a short `reason`. Fully reproducible; serves as the
  paper's baseline for the LLM-vs-rule comparison.
- **LLM** (`llm`): `llm_adapter` builds the init/step payload, calls `llm_server` **in-process**
  (`profile_pool.ensure_and_slice` → `perception` → `decision_making`), and parses the result with
  `response_parser`. On any failure (import error, Ollama down, unparseable output) it **falls back
  to the rule-based core** without crashing; the active source is reported to the UI. In this mode each
  triggered car's single `memory.summary` is rewritten by the LLM **at decision time** (`_summarize_memory`).

## Cross-cutting properties

- **Determinism.** Seeded RNG throughout; the same seed yields identical trajectories. LLM decision
  text and (optionally) the LLM trip-summary are the only non-deterministic elements, and they do
  not feed back into the deterministic physics.
- **Robustness.** Malformed LLM JSON is salvaged object-by-object (`json_utils`); persona generation
  is cached into a stable pool and reused rather than regenerated on every change.
- **Configuration.** All tunables flow from `config/simulation.toml` through typed dataclasses in
  `config.py`; `config.py` defaults are the fallback.

## Runtime assumptions

- Web demo served by FastAPI/uvicorn (default `127.0.0.1:8080`).
- For LLM mode: a local Ollama API (default `http://127.0.0.1:11434/api/generate`) with the
  configured model pulled.
- The bundled `data/tainan_roads.graphml` lets the demo run fully offline with the rule-based core.
