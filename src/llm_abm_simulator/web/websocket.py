"""websocket.py — 單一 WebSocket 連線的模擬會話。

每個連線一個 SimulationEngine。控制迴圈在背景 asyncio task 中跑，
每步以 ``asyncio.to_thread`` 呼叫同步的 engine.step（LLM 模式下 step 可能較慢，
放到執行緒避免卡住事件迴圈與其他連線）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..config import DEFAULT_CONFIG, UI_CONFIG, SimulationConfig
from ..simulation.engine import SimulationEngine

logger = logging.getLogger(__name__)


class SimulationSession:
    """管理單一連線的模擬生命週期與控制迴圈。"""

    def __init__(self, websocket: WebSocket, base_cfg: SimulationConfig | None = None) -> None:
        self.ws = websocket
        self.cfg = base_cfg or DEFAULT_CONFIG
        self.engine = SimulationEngine(self.cfg)
        self.speed_multiplier = 1.0
        self._run_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()   # 序列化送出：避免 run loop 與 set_view 快照並發 send 撞在一起

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
        await self.ws.accept()
        await self.status("正在載入 GIS 資料與路網…")
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status("初始化完成，可開始模擬。")

        try:
            while True:
                data = await self.ws.receive_json()
                if data.get("type") == "control":
                    await self._on_control(data.get("action", ""), data.get("value"))
        except WebSocketDisconnect:
            logger.info("WebSocket 連線結束")
        finally:
            await self._stop_run_task()

    # ------------------------------------------------------------------
    async def _on_control(self, action: str, value: Any) -> None:
        if action == "start" or action == "resume":
            await self._start_run()
        elif action == "pause":
            self.engine.pause()
            await self._stop_run_task()
            await self.status("模擬已暫停")
        elif action == "step":
            if not self.engine.scheduler.finished:
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
            await self.status(f"已{'套用自訂' if ov else '還原預設'} prompt：{v.get('name')}")
        elif action == "reset":
            await self._reset()
        elif action == "set_speed":
            default = UI_CONFIG.speed_default
            self.speed_multiplier = max(UI_CONFIG.speed_min,
                                        min(float(value or default), UI_CONFIG.speed_max))
            await self.status(f"速度設為 {self.speed_multiplier:.1f} 倍")
        elif action == "set_agents":
            await self._set_agents(int(value or self.cfg.nb_agents))
        elif action == "set_mode":
            self._set_mode(str(value or "rule"))
            await self.status(f"決策核心：{'LLM 認知核心' if self.cfg.use_llm else '規則式（Rule-based）'}")
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
        elif action == "set_llm":
            await self._set_llm(value or {})
        elif action == "regenerate_profiles":
            await self._regenerate_profiles()
        elif action == "set_scenario":
            await self._set_scenario(str(value or ""))
        else:
            logger.warning("未知控制指令: %s", action)

    # ------------------------------------------------------------------
    async def _start_run(self) -> None:
        if self._run_task and not self._run_task.done():
            return
        if self.engine.scheduler.finished:
            await self.status("已達最大步數，請先重設。")
            return
        self.engine.resume()
        self._run_task = asyncio.create_task(self._run_loop())
        await self.status("模擬開始執行")

    async def _run_loop(self) -> None:
        try:
            while self.engine.running and not self.engine.scheduler.finished:
                state = await asyncio.to_thread(self.engine.step)
                await self.send(state.to_message())
                await asyncio.sleep(max(0.15, 1.0 / self.speed_multiplier))
        except Exception as e:  # noqa: BLE001
            logger.error("執行迴圈錯誤: %s", e, exc_info=True)
        finally:
            self.engine.pause()
            if self.engine.scheduler.finished:
                await self.status(
                    f"模擬完成：共 {self.engine.scheduler.cycle} 步"
                    f"（{self.engine.scheduler.elapsed_minutes} 分鐘）"
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
        await self.status("模擬已重設")

    async def _set_agents(self, n: int) -> None:
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("模擬進行中無法變更 agent 數，請先重設。")
            return
        n = max(UI_CONFIG.agents_min, min(n, UI_CONFIG.agents_max))
        self.cfg = dataclasses.replace(self.cfg, nb_agents=n)
        await self._stop_run_task()
        self.engine = SimulationEngine(self.cfg)
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"agent 數設為 {n}")

    async def _set_time(self, max_steps: int | None = None, step_minutes: int | None = None) -> None:
        """設定「跑幾個週期 / 每週期幾分鐘」（比照 set_agents：進行中先擋，改了重新初始化）。"""
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("模擬進行中無法變更時間設定，請先重設。")
            return
        ms = self.cfg.max_steps if max_steps is None else max(UI_CONFIG.steps_min, min(max_steps, UI_CONFIG.steps_max))
        sm = self.cfg.step_minutes
        if step_minutes is not None and step_minutes in UI_CONFIG.step_minutes_options:
            sm = step_minutes   # 不在允許清單就忽略，沿用原值
        self.cfg = dataclasses.replace(self.cfg, max_steps=ms, step_minutes=sm)
        await self._stop_run_task()
        self.engine = SimulationEngine(self.cfg)
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"已設定：{ms} 週期 × {sm} 分/週期")

    async def _set_ambient(self, n: int) -> None:
        """設定背景常態車數（runtime 覆寫 [ambient].count），重新初始化。模擬進行中先擋。"""
        from .. import config
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("模擬進行中無法變更背景車數，請先重設。")
            return
        config.set_runtime_ambient_count(max(0, min(n, config.AMBIENT_CONFIG.max_count)))
        await self._stop_run_task()
        self.engine = SimulationEngine(self.cfg)
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"背景車數設為 {config.effective_ambient_count()}")

    async def _regenerate_profiles(self) -> None:
        """『重新生成人物』：清掉 persona 池並重新初始化（下次 LLM init 會重生整批）。"""
        from ..decisions import profile_pool
        await self._stop_run_task()
        profile_pool.clear_pool()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status("已清除人物池，將於下次 LLM 初始化重新生成。")

    async def _set_scenario(self, key: str) -> None:
        """切換場景（圖層）：設 active → 重新初始化引擎 → 重送 init。模擬進行中先停。"""
        from .. import scenarios
        if not scenarios.set_active(key):
            await self.status(f"未知場景：{key}")
            return
        await self._stop_run_task()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status(f"已切換場景：{scenarios.active().name}")

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
            answer = "（LLM 暫時不可用，附當前模擬狀態）\n" + ctx
        await self.send({"type": "chat", "text": answer})

    async def _intervene(self, text: str) -> None:
        """NL 介入：解析指令 → 套用受限動作 → 回報 + 即時更新前端。"""
        if not text.strip():
            return
        try:
            from llm_server.sim_intervene import run_intervene
            act = await asyncio.to_thread(run_intervene, text, self.engine._available_towns)
        except Exception as e:  # noqa: BLE001
            await self.send({"type": "chat", "text": f"（介入解析失敗：{e}）"})
            return
        if act["action"] == "none":
            await self.send({"type": "chat", "text": "未能對應到可執行的介入（可試：避開某區 / 從某區湧入 N 台）。"})
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

    async def _set_llm(self, value: dict) -> None:
        """前端選模型：套用 runtime 後端/模型，並連動 max_model_len 與 ollama num_ctx（整套 LLM 共用）。"""
        from .. import config
        from llm_server import llm_config, model_registry
        backend = value.get("backend") or llm_config.LLM_BACKEND
        model = value.get("model") or ""
        if backend == "vllm" and model:
            max_len = model_registry.suggested_max_model_len(model)
            llm_config.set_runtime_llm("vllm", model, num_ctx=None)
            config.set_runtime_max_model_len(max_len)
        else:  # ollama
            max_len = model_registry.PROJECT_CONTEXT_CAP
            llm_config.set_runtime_llm("ollama", model, num_ctx=max_len)
            config.set_runtime_max_model_len(max_len)
        await self.status(f"LLM 已切換：{backend} · {model or '(預設)'}（max_model_len={max_len}）")

    def _set_mode(self, mode: str) -> None:
        # 動態切換 mock/llm；engine 內部 cfg 與本 session cfg 同步
        self.cfg = dataclasses.replace(self.cfg, use_llm=(mode == "llm"))
        self.engine.cfg = self.cfg
