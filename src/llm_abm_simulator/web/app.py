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
import sys

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from .websocket import SimulationSession

logger = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """為本專案 logger 設一個乾淨、有時間戳的 console handler。

    uvicorn 預設不會顯示 app logger 的 INFO，且 pipeline 原本用 print 在併發批次下會交錯亂印。
    這裡給 ``llm_abm_simulator`` 與 ``llm_server`` 各掛一個格式化 handler、關掉 propagate（不重複），
    讓「運行中狀態」乾淨呈現：每步決策摘要走 INFO、單次 LLM 呼叫走 DEBUG、卡住的呼叫走 WARNING。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    for name in ("llm_abm_simulator", "llm_server"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.setLevel(level)
        lg.propagate = False


def create_app() -> FastAPI:
    configure_logging()
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

    @app.get("/api/gis/{name}", response_model=None)
    async def get_gis_export(name: str):
        """下載匯出的 GIS 主題圖層 Shapefile zip（由 WS export_gis 動作產生於 output/）。"""
        # 防路徑穿越：只允許 output/ 內、gis_*.zip 命名的檔案
        if "/" in name or "\\" in name or ".." in name or not re.fullmatch(r"gis_[\w.]+\.zip", name):
            return JSONResponse({"error": "invalid name"}, status_code=400)
        p = config.OUTPUT_DIR / name
        if not p.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(p, media_type="application/zip", filename=name)

    @app.get("/api/llm/models")
    async def list_llm_models(backend: str = "ollama") -> JSONResponse:
        """列出可選模型：ollama→即時查 /api/tags 的實裝模型；vllm→候選登錄表。"""
        if backend == "vllm":
            from llm_server.model_registry import VLLM_MODELS
            return JSONResponse({"backend": "vllm", "models": VLLM_MODELS})
        # ollama：即時查實裝模型
        try:
            import requests
            from llm_server import llm_config
            r = requests.get(f"{llm_config.ollama_base_url()}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            return JSONResponse({"backend": "ollama", "models": [{"id": n, "label": n} for n in names]})
        except Exception as e:  # noqa: BLE001  Ollama 沒開/連不上 → 空清單
            return JSONResponse({"backend": "ollama", "models": [], "error": str(e)[:200]})

    @app.get("/api/rag/status")
    async def rag_status() -> JSONResponse:
        from llm_server import rag_store
        return JSONResponse(rag_store.stats())

    @app.post("/api/rag/add")
    async def rag_add(body: dict) -> JSONResponse:
        """加入一份知識文件（純文字）。body: {name, text}。"""
        from llm_server import rag_store
        text = str(body.get("text", ""))
        if not text.strip():
            return JSONResponse({"error": "text 為空"}, status_code=400)
        rag_store.add_text(str(body.get("name", "uploaded")), text)
        return JSONResponse(rag_store.stats())

    @app.post("/api/rag/clear")
    async def rag_clear() -> JSONResponse:
        from llm_server import rag_store
        rag_store.clear()
        return JSONResponse(rag_store.stats())

    @app.post("/api/rag/toggle")
    async def rag_toggle(body: dict) -> JSONResponse:
        from llm_server import rag_store
        rag_store.enabled = bool(body.get("enabled", True))
        return JSONResponse(rag_store.stats())

    @app.post("/api/scenario/upload")
    async def scenario_upload(body: dict) -> JSONResponse:
        """上傳自訂場景（純文字檔內容，不需 multipart）。

        body: {key, name, county_filter, dest_lat, dest_lng, dest_town,
               roads_graphml (我方格式 graphml 文字), population_csv (選填文字)}
        驗證 graphml 可載入且含座標屬性 → 寫檔 + 寫 manifest + 註冊場景。
        """
        import networkx as nx
        from .. import scenarios
        key = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("key", "")).strip())
        if not key:
            return JSONResponse({"error": "key 必填（英數）"}, status_code=400)
        graphml_text = str(body.get("roads_graphml", ""))
        if not graphml_text.strip():
            return JSONResponse({"error": "roads_graphml 為空"}, status_code=400)
        scenarios.SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        gpath = scenarios.SCENARIOS_DIR / f"{key}_roads.graphml"
        gpath.write_text(graphml_text, encoding="utf-8")
        # 驗證：可載入且節點有座標屬性（須為本專案格式，如 builder/build_roads 產生）
        try:
            g = nx.read_graphml(str(gpath))
            n0 = next(iter(g.nodes(data=True)))[1]
            if not {"x_m", "y_m", "lat", "lng"} <= set(n0):
                raise ValueError("節點缺座標屬性（x_m/y_m/lat/lng）")
        except Exception as e:  # noqa: BLE001
            gpath.unlink(missing_ok=True)
            return JSONResponse({"error": f"graphml 不合格式：{str(e)[:160]}"}, status_code=400)

        manifest = {
            "key": key, "name": str(body.get("name", key)),
            "county_filter": str(body.get("county_filter", "臺南|台南")),
            "road_graphml": str(gpath),
            "dest_lat": body.get("dest_lat"), "dest_lng": body.get("dest_lng"),
            "dest_town": str(body.get("dest_town", "")),
            "center_lat": body.get("dest_lat") or 23.06, "center_lng": body.get("dest_lng") or 120.23,
            "zoom": 12,
        }
        pop = str(body.get("population_csv", ""))
        if pop.strip():
            ppath = scenarios.SCENARIOS_DIR / f"{key}_pop.csv"
            ppath.write_text(pop, encoding="utf-8")
            manifest["population_csv"] = str(ppath)
        import json as _json
        (scenarios.SCENARIOS_DIR / f"{key}.json").write_text(
            _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        scenarios.register_manifest(manifest)
        return JSONResponse({"ok": True, "scenarios": scenarios.all_summaries(),
                             "nodes": g.number_of_nodes(), "edges": g.number_of_edges()})

    @app.get("/api/prompts")
    async def get_prompts() -> JSONResponse:
        """目前可編輯的 prompt（label / 預設 / 目前值 / 是否覆寫）。import 兩模組以確保預設已註冊。"""
        from llm_server import agent_profile, decision_making, prompt_store  # noqa: F401  觸發註冊
        return JSONResponse(prompt_store.snapshot())

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
