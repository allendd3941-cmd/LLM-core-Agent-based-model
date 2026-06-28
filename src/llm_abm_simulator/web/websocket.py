"""websocket.py — 單一 WebSocket 連線的模擬會話。

每個連線一個 SimulationEngine。控制迴圈在背景 asyncio task 中跑，
每步以 ``asyncio.to_thread`` 呼叫同步的 engine.step（LLM 模式下 step 可能較慢，
放到執行緒避免卡住事件迴圈與其他連線）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..config import DEFAULT_CONFIG, UI_CONFIG, SimulationConfig
from ..simulation.engine import SimulationEngine
from ..spatial import gis_loader
from .logctx import session_id_var

logger = logging.getLogger(__name__)


def _sanitize_detectors(raw: Any) -> list[dict[str, Any]]:
    """把前端送來的監測器點位清成 [{lat,lng}, ...]（上限保護、丟棄無效項）。

    上限放寬到 100：預設已內建 55 台驗證相機，前端「套用設定」會把目前地圖上的偵測器
    （含這 55 台）整批回送，故須容得下 55 台 + 使用者手動加放的數台，避免被截斷。
    """
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for d in raw[:100]:
            try:
                item: dict[str, Any] = {"lat": float(d["lat"]), "lng": float(d["lng"])}
            except (KeyError, TypeError, ValueError):
                continue
            # 保留相機 UUID（ext_id/ext_name）：預設 55 台驗證相機 round-trip 後仍能對應真實相機、
            # 匯出驗證 CSV 才不會因 UUID 被洗掉而全空。手動放置的點無此欄、自然略過。
            if d.get("ext_id"):
                item["ext_id"] = d["ext_id"]
            if d.get("ext_name"):
                item["ext_name"] = d["ext_name"]
            out.append(item)
    return out


class SimulationSession:
    """管理單一連線的模擬生命週期與控制迴圈。"""

    def __init__(self, websocket: WebSocket, base_cfg: SimulationConfig | None = None) -> None:
        self.ws = websocket
        self.cfg = base_cfg or DEFAULT_CONFIG
        # 預設內建驗證用真實監視器（球場 5km 內 55 台）；前端「套用設定」送 detectors 時才被覆寫。
        self._detector_specs: list[dict[str, Any]] = gis_loader.load_default_detectors()
        self.engine = self._make_engine()
        self.speed_multiplier = 1.0
        self._run_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()   # 序列化送出：避免 run loop 與 set_view 快照並發 send 撞在一起

    def _make_engine(self) -> SimulationEngine:
        """建立引擎並套用目前的監測器點位（每次重建引擎都要重新套用）。

        後端**預設 UXsim**（正式/論文用）；本機開發若記憶體不足（UXsim 全市約 9GB）可設
        ``LLM_ABM_ENGINE=legacy`` 退回自寫物理引擎當逃生口/baseline（見 docs/UXSIM_MIGRATION_zh-TW.md）。
        """
        import os
        if os.getenv("LLM_ABM_ENGINE", "uxsim").lower() == "legacy":
            eng: SimulationEngine = SimulationEngine(self.cfg)
        else:
            from ..simulation.uxsim_engine import UXsimEngine
            eng = UXsimEngine(self.cfg)
        eng.set_detectors(self._detector_specs)
        return eng

    # ------------------------------------------------------------------
    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:   # 同一連線一次只送一個 frame（並發 send 會壞掉）
            try:
                await self.ws.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                pass

    async def status(self, message: str) -> None:
        await self.send({"type": "status", "message": message})

    # ------------------------------------------------------------------
    async def handle(self) -> None:
        session_id_var.set(uuid.uuid4().hex[:4])   # 本連線短 id（log 帶 [sess xxxx]，多連線不交錯）
        await self.ws.accept()
        await self.status("Loading GIS data and road network…")
        # init 在大規模(數萬車逐車路由)可能很久；放背景執行緒跑、同時每隔幾秒送 keepalive 進度，
        # 避免長時間無資料流動被瀏覽器/代理 idle timeout 斷線。
        await self._initialize_with_keepalive()
        await self.send(self.engine.init_payload())
        await self.status("Initialization complete; ready to run.")

        try:
            while True:
                data = await self.ws.receive_json()
                if data.get("type") == "control":
                    await self._on_control(data.get("action", ""), data.get("value"))
        except WebSocketDisconnect:
            logger.info("WebSocket 連線結束")
        except RuntimeError as e:
            # 連線已關（如長操作後 client/proxy 斷線）→ receive_json 會丟 RuntimeError；安靜收尾不噴 traceback
            logger.info("WebSocket 已關閉，結束本連線：%s", e)
        finally:
            await self._stop_run_task()

    async def _initialize_with_keepalive(self, interval_s: float = 3.0) -> None:
        """背景跑 engine.initialize，同時定期送進度，維持連線存活。init 內的例外照常往上拋。"""
        init_task = asyncio.create_task(asyncio.to_thread(self.engine.initialize))
        elapsed = 0.0
        while not init_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(init_task), timeout=interval_s)
            except asyncio.TimeoutError:
                elapsed += interval_s
                await self.status(f"Initializing… ({int(elapsed)}s; first compute of a large network/fleet takes longer)")
        await init_task   # 取結果並讓 init 內的例外正常傳遞

    # ------------------------------------------------------------------
    async def _on_control(self, action: str, value: Any) -> None:
        if action == "start" or action == "resume":
            await self._start_run()
        elif action == "pause":
            self.engine.pause()
            await self._stop_run_task()
            await self.status("Simulation paused")
        elif action == "step":
            if not self.engine.scheduler.finished:
                await self.status(f"Computing step {self.engine.scheduler.cycle + 1}… (first run / route recompute can take a few minutes)")
                state = await asyncio.to_thread(self.engine.step)
                await self.send(state.to_message())
                if self.engine.scheduler.finished:  # 手動單步走到結束 → 出分析
                    await self._send_analysis()
        elif action == "analysis":
            await self._send_analysis()
        elif action == "ask":
            await self._ask(str(value or ""))
        elif action == "intervene":
            await self._intervene(str(value or ""))
        elif action == "clear_intervention":
            msg = await asyncio.to_thread(self.engine.clear_interventions)
            await self.send({"type": "chat", "text": "🧹 " + msg})
            await self.send(self.engine.snapshot_now().to_message())
        elif action == "set_prompt":
            v = value or {}
            from llm_server import prompt_store
            prompt_store.set_override(str(v.get("name", "")), v.get("text"))
            ov = bool((v.get("text") or "").strip())
            await self.status(f"{'Applied custom' if ov else 'Restored default'} prompt: {v.get('name')}")
        elif action == "reset":
            await self._reset()
        elif action == "set_speed":
            default = UI_CONFIG.speed_default
            self.speed_multiplier = max(UI_CONFIG.speed_min,
                                        min(float(value or default), UI_CONFIG.speed_max))
            await self.status(f"Speed set to {self.speed_multiplier:.1f}×")
        elif action == "set_agents":
            await self._set_agents(int(value or self.cfg.nb_agents))
        elif action == "set_mode":
            self._set_mode(str(value or "rule"))
            await self.status(f"Decision core: {'LLM cognitive core' if self.cfg.use_llm else 'Rule-based'}")
        elif action == "set_ambient":
            await self._set_ambient(int(value) if value is not None else 0)
        elif action == "set_view":
            # ⑥ 前端回報可視範圍（zoom + bounds）；存下後立即回推一張快照，讓 zoom/pan 即時顯示
            # 範圍內的車，不必等慢的模擬步。高頻、不回狀態；snapshot 放到 thread 避免卡事件迴圈。
            v = value or {}
            self.engine.set_view(v.get("zoom", 0), v.get("bounds") or {})
            if self.engine.is_initialized:
                state = await asyncio.to_thread(self.engine.snapshot_now)
                await self.send(state.to_message())
        elif action == "set_max_steps":
            await self._set_time(max_steps=int(value) if value is not None else None)
        elif action == "set_step_minutes":
            await self._set_time(step_minutes=int(value) if value is not None else None)
        elif action == "apply_config":
            await self._apply_config(value or {})
        elif action == "set_llm":
            await self._set_llm(value or {})
        elif action == "regenerate_profiles":
            await self._regenerate_profiles()
        elif action == "set_scenario":
            await self._set_scenario(str(value or ""))
        elif action == "declare_egress":
            msg = await asyncio.to_thread(self.engine.declare_egress)
            await self.status(msg)
            if self.engine.is_initialized:
                await self.send(self.engine.snapshot_now().to_message())
        elif action == "get_agent_path":
            v = value or {}
            aid = str(v.get("agent_id", ""))
            if aid and self.engine.is_initialized:
                res = self.engine.get_agent_path(aid)
                await self.send({"type": "agent_path", **res})
        elif action == "snap_detector":
            v = value or {}
            try:
                res = self.engine.snap_point(float(v.get("lat")), float(v.get("lng")))
            except (TypeError, ValueError):
                res = {"ok": False}
            await self.send({"type": "detector_snap", **res})
        elif action == "export_gis":
            await self._export_gis(str(value or "los"))
        elif action == "export_validation":
            await self._export_validation(str(value or ""))
        else:
            logger.warning("未知控制指令: %s", action)

    # ------------------------------------------------------------------
    async def _start_run(self) -> None:
        if self._run_task and not self._run_task.done():
            return
        if self.engine.scheduler.finished:
            await self.status("Reached max steps; reset first.")
            return
        self.engine.resume()
        self._run_task = asyncio.create_task(self._run_loop())
        await self.status("Simulation started")

    async def _run_loop(self) -> None:
        try:
            while self.engine.running and not self.engine.scheduler.finished:
                # 先送「計算中」狀態：城市尺度單步(尤其首次/重算路徑)可能數分鐘，避免前端看似凍住
                await self.status(f"計算第 {self.engine.scheduler.cycle + 1} 步中…（首次/重算路徑表在城市尺度需數分鐘，請稍候）")
                state = await asyncio.to_thread(self.engine.step)
                await self.send(state.to_message())
                await asyncio.sleep(0)   # 零等待：算完一步立刻推下一步（只讓出事件迴圈）。LLM 本來就慢，不需人工延遲
        except Exception as e:  # noqa: BLE001
            logger.error("執行迴圈錯誤: %s", e, exc_info=True)
        finally:
            self.engine.pause()
            if self.engine.scheduler.finished:
                await self.status(
                    f"Simulation complete: {self.engine.scheduler.cycle} steps"
                    f" ({self.engine.scheduler.elapsed_minutes} min)"
                )
                await self._send_analysis()

    async def _stop_run_task(self) -> None:
        self.engine.pause()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def _reset(self) -> None:
        # 注意：reset 不刪 persona 池——調 agent 數/重設都重用同一批人物，避免一直重生。
        await self._stop_run_task()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status("Simulation reset")

    async def _set_agents(self, n: int) -> None:
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("Cannot change event-vehicle count while running; reset first.")
            return
        n = max(UI_CONFIG.agents_min, min(n, UI_CONFIG.agents_max))
        self.cfg = dataclasses.replace(self.cfg, nb_agents=n)
        await self._stop_run_task()
        self.engine = self._make_engine()
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"Event vehicles set to {n}")

    async def _apply_config(self, v: dict) -> None:
        """一次套用前端暫存的設定（事件車數/背景車數/週期數/每週期分鐘）→ 只重新初始化一次。

        前端滑桿改成「預覽」（拖動只更新數字、不送），使用者按「套用設定」才送這個 → 避免每次微調都重設。
        """
        from .. import config
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("Cannot change settings while running; reset first.")
            return
        changes: dict[str, Any] = {}
        if v.get("nb_agents") is not None:
            changes["nb_agents"] = max(UI_CONFIG.agents_min, min(int(v["nb_agents"]), UI_CONFIG.agents_max))
        if v.get("max_steps") is not None:
            changes["max_steps"] = max(UI_CONFIG.steps_min, min(int(v["max_steps"]), UI_CONFIG.steps_max))
        if v.get("step_minutes") is not None and int(v["step_minutes"]) in UI_CONFIG.step_minutes_options:
            changes["step_minutes"] = int(v["step_minutes"])
        self.cfg = dataclasses.replace(self.cfg, **changes)
        if v.get("ambient") is not None:
            config.set_runtime_ambient_count(max(0, min(int(v["ambient"]), config.AMBIENT_CONFIG.max_count)))
        config.set_runtime_departure(
            v.get("departure_profile"),
            int(v["departure_window"]) if v.get("departure_window") is not None else None)
        config.set_runtime_egress(
            v.get("egress_profile"),
            int(v["egress_window"]) if v.get("egress_window") is not None else None,
            v.get("egress_destination"),
            v.get("egress_carry_memory"))
        if v.get("detectors") is not None:
            self._detector_specs = _sanitize_detectors(v.get("detectors"))
        await self._stop_run_task()
        self.engine = self._make_engine()
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        dep = config.effective_departure()
        eg = config.effective_egress()
        await self.status(
            f"Applied: {self.cfg.nb_agents} event vehicles · background {config.effective_ambient_count()} · "
            f"{self.cfg.max_steps} cycles × {self.cfg.step_minutes} min · "
            f"ingress {dep.profile}/{dep.window_minutes}min · egress {eg.profile}/{eg.window_minutes}min→{eg.destination}")

    async def _set_time(self, max_steps: int | None = None, step_minutes: int | None = None) -> None:
        """設定「跑幾個週期 / 每週期幾分鐘」（比照 set_agents：進行中先擋，改了重新初始化）。"""
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("Cannot change timing while running; reset first.")
            return
        ms = self.cfg.max_steps if max_steps is None else max(UI_CONFIG.steps_min, min(max_steps, UI_CONFIG.steps_max))
        sm = self.cfg.step_minutes
        if step_minutes is not None and step_minutes in UI_CONFIG.step_minutes_options:
            sm = step_minutes   # 不在允許清單就忽略，沿用原值
        self.cfg = dataclasses.replace(self.cfg, max_steps=ms, step_minutes=sm)
        await self._stop_run_task()
        self.engine = self._make_engine()
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"Set: {ms} cycles × {sm} min/cycle")

    async def _set_ambient(self, n: int) -> None:
        """設定背景常態車數（runtime 覆寫 [ambient].count），重新初始化。模擬進行中先擋。"""
        from .. import config
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("Cannot change background count while running; reset first.")
            return
        config.set_runtime_ambient_count(max(0, min(n, config.AMBIENT_CONFIG.max_count)))
        await self._stop_run_task()
        self.engine = self._make_engine()
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"Background count set to {config.effective_ambient_count()}")

    async def _regenerate_profiles(self) -> None:
        """『重新生成人物』：清掉 persona 池並重新初始化（下次 LLM init 會重生整批）。"""
        from ..decisions import profile_pool
        await self._stop_run_task()
        profile_pool.clear_pool()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status("Persona pool cleared; will regenerate on the next LLM init.")

    async def _set_scenario(self, key: str) -> None:
        """切換場景（圖層）：設 active → 重新初始化引擎 → 重送 init。模擬進行中先停。"""
        from .. import scenarios
        if not scenarios.set_active(key):
            await self.status(f"Unknown scenario: {key}")
            return
        await self._stop_run_task()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status(f"Switched scenario: {scenarios.active().name}")

    async def _ask(self, question: str) -> None:
        """暫停對話查詢（唯讀）：用當前模擬狀態 + LLM 回答；LLM 不可用則回附狀態文字。"""
        if not question.strip():
            return
        ctx = self.engine.chat_context()
        try:
            from llm_server.sim_chat import run_sim_chat
            answer = await asyncio.to_thread(run_sim_chat, ctx, question)
        except Exception as e:  # noqa: BLE001  LLM 不可用 → fallback 附狀態
            logger.warning("sim_chat 失敗：%s", e)
            answer = "(LLM temporarily unavailable; showing current simulation state)\n" + ctx
        await self.send({"type": "chat", "text": answer})

    async def _intervene(self, text: str) -> None:
        """NL 介入：解析指令 → 套用受限動作 → 回報 + 即時更新前端。"""
        if not text.strip():
            return
        try:
            from llm_server.sim_intervene import run_intervene
            act = await asyncio.to_thread(run_intervene, text, self.engine._available_towns)
        except Exception as e:  # noqa: BLE001
            await self.send({"type": "chat", "text": f"(Intervention parse failed: {e})"})
            return
        if act["action"] == "none":
            await self.send({"type": "chat", "text": "No actionable intervention matched (try: avoid a district / send N vehicles in from a district)."})
            return
        summary = await asyncio.to_thread(
            self.engine.apply_intervention, act["action"], act["town"], act["count"])
        await self.send({"type": "chat", "text": "🛠️ " + summary})
        await self.send(self.engine.snapshot_now().to_message())

    async def _send_analysis(self) -> None:
        """送模擬後交通分析資料給前端（分析面板）。"""
        try:
            data = await asyncio.to_thread(self.engine.build_analysis)
            await self.send({"type": "analysis", **data})
        except Exception as e:  # noqa: BLE001
            logger.warning("分析資料產生失敗：%s", e)

    async def _export_gis(self, layer: str) -> None:
        """匯出 GIS 主題圖層 Shapefile（zip）→ 回下載連結。需先初始化（有路網）才能匯出。"""
        from .. import config
        if not self.engine.is_initialized:
            await self.status("Initialize the simulation before exporting layers.")
            return
        try:
            path = await asyncio.to_thread(self.engine.export_gis_layer, layer, config.OUTPUT_DIR)
        except ValueError as e:
            await self.status(f"Export failed: {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.error("GIS 匯出錯誤：%s", e, exc_info=True)
            await self.status("Export failed (server error).")
            return
        await self.send({"type": "download", "url": f"/api/gis/{path.name}", "name": path.name,
                         "label": f"GIS layer ({layer})"})

    async def _export_validation(self, case: str) -> None:
        """匯出組員 validation 腳本可直接吃的 CSV（gameday 事件車 + nogameday 全0 + 參數檔，打包 zip）。

        case = weekend / weekday（決定時間視窗起點與檔名）。需先初始化且已跑過至少一步。
        """
        from .. import config
        case = case.strip().lower()
        if case not in ("weekend", "weekday"):
            await self.status("Select weekend or weekday before exporting.")
            return
        if not self.engine.is_initialized or self.engine.scheduler.cycle <= 0:
            await self.status("Run the simulation (at least one step) before exporting the validation CSV.")
            return
        try:
            path = await asyncio.to_thread(self.engine.export_validation_csv, case, config.OUTPUT_DIR)
        except ValueError as e:
            await self.status(f"Export failed: {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.error("驗證 CSV 匯出錯誤：%s", e, exc_info=True)
            await self.status("Export failed (server error).")
            return
        await self.send({"type": "download", "url": f"/api/validation/{path.name}", "name": path.name,
                         "label": f"Validation CSV ({case})"})

    async def _set_llm(self, value: dict) -> None:
        """前端選 vLLM 模型：套用 runtime 模型名 + 連動 max_model_len（整套 LLM 共用）。

        注意：vLLM 一機一模型、不能熱切換真正的 server；此處只改 client 對齊的模型名，
        實際要 `vllm serve` 對應模型。
        """
        from .. import config
        from llm_server import llm_config, model_registry
        model = value.get("model") or ""
        max_len = (model_registry.suggested_max_model_len(model) if model
                   else model_registry.PROJECT_CONTEXT_CAP)
        llm_config.set_runtime_llm(model)
        config.set_runtime_max_model_len(max_len)
        await self.status(
            f"LLM model set to: {model or '(unset; set VLLM_MODEL in .env)'} (max_model_len={max_len})")

    def _set_mode(self, mode: str) -> None:
        # 動態切換 mock/llm；engine 內部 cfg 與本 session cfg 同步
        self.cfg = dataclasses.replace(self.cfg, use_llm=(mode == "llm"))
        self.engine.cfg = self.cfg
