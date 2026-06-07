"""app.py — FastAPI 應用程式（靜態前端 + WebSocket）。

啟動方式：

    uvicorn llm_abm_simulator.web.app:app --host 0.0.0.0 --port 8080

路由：
    GET  /             → 前端 index.html
    GET  /<file>       → 前端靜態檔（app.js / map.js / ...）
    WS   /ws           → 模擬即時通訊
    GET  /healthz      → 健康檢查
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from .websocket import SimulationSession

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="LLM ABM 交通模擬器", version="0.1.0")
    frontend = config.FRONTEND_DIR

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # noqa: D401
        return JSONResponse({"status": "ok"})

    @app.get("/")
    async def index() -> FileResponse:
        index_path = frontend / "index.html"
        if not index_path.exists():
            return JSONResponse({"error": "frontend index.html not found"}, status_code=404)
        return FileResponse(index_path)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        session = SimulationSession(websocket, config.DEFAULT_CONFIG)
        await session.handle()

    @app.get("/api/decision-outputs")
    async def list_decision_outputs() -> JSONResponse:
        """列出 output/ 內所有 decision_making_output_N.txt 的 N（升冪）。"""
        out = config.OUTPUT_DIR
        steps: list[int] = []
        if out.exists():
            for p in out.glob("decision_making_output_*.txt"):
                m = re.search(r"_(\d+)\.txt$", p.name)
                if m:
                    steps.append(int(m.group(1)))
        return JSONResponse({"steps": sorted(steps)})

    @app.get("/api/decision-outputs/{n}")
    async def get_decision_output(n: int) -> JSONResponse:
        """回傳第 N 份 decision_making 原始輸出文字。"""
        p = config.OUTPUT_DIR / f"decision_making_output_{n}.txt"
        if not p.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"step": n, "text": text})

    # 靜態前端檔（CSS / JS）。掛在 /static 與根層級各檔。
    if frontend.exists():
        app.mount("/static", StaticFiles(directory=str(frontend)), name="static")

        @app.get("/{filename}")
        async def frontend_file(filename: str) -> FileResponse:
            target = frontend / filename
            if target.exists() and target.is_file():
                return FileResponse(target)
            return JSONResponse({"error": "not found"}, status_code=404)

    logger.info("FastAPI app 建立完成，frontend=%s", frontend)
    return app


app = create_app()
