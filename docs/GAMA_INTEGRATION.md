# GAMA Integration

This project is designed to work as the Python LLM backend for a GAMA traffic ABM model.

## Connection Settings

The GAMA HTTP client is configured to call:

```text
Host: 127.0.0.1
Port: 8000
Endpoint: /from-gama
Method: POST
```

The corresponding FastAPI command is:

```bash
PYTHONPATH=src uvicorn llm_server.server:app --host 127.0.0.1 --port 8000 --reload
```

## Request Lifecycle

1. GAMA starts the simulation and creates vehicle agents.
2. GAMA sends an initialization request with `request_type = init_agents`.
3. The Python server generates or loads LLM agent profiles.
4. During each simulation step, GAMA sends `request_type = step_update`.
5. The server builds perception context and decision context.
6. Ollama returns a decision response.
7. GAMA parses the response and updates agent behavior.

## Payload Compatibility

The Python server accepts both:

- `requested_agents` for initialization requests.
- `agents_status` for step-update requests.
- `agents` for earlier compatibility payloads.

This keeps the FastAPI schema compatible with the current GAMA `.gaml` files while preserving the earlier Python request format.

## Notes for GitHub Review

When presenting this project as a portfolio artifact, include:

- The GAMA model files in `gama_moudle/`.
- The FastAPI endpoint documentation in `docs/API.md`.
- Example payloads in `examples/`.
- A short explanation of how GAMA, FastAPI, Ollama, and the LLM modules interact.

## Known Limitations

- The LLM response format is prompt-driven and not yet fully schema-enforced.
- Running the complete workflow requires both GAMA and Ollama.
- Local simulation outputs should be treated as runtime artifacts, not source code.
