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

from .. import config
from ..config import DEFAULT_CONFIG, UI_CONFIG, SimulationConfig
from ..simulation.engine import SimulationEngine

logger = logging.getLogger(__name__)
_AGENT_PROFILE_FILENAME = "agent_profile_output_1.txt"


class SimulationSession:
    """管理單一連線的模擬生命週期與控制迴圈。"""

    def __init__(self, websocket: WebSocket, base_cfg: SimulationConfig | None = None) -> None:
        self.ws = websocket
        self.cfg = base_cfg or DEFAULT_CONFIG
        self.engine = SimulationEngine(self.cfg)
        self.speed_multiplier = 1.0
        self._run_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    async def send(self, message: dict[str, Any]) -> None:
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
            self._set_mode(str(value or "mock"))
            await self.status(f"決策模式：{'LLM' if self.cfg.use_llm else 'Mock'}")
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

    async def _stop_run_task(self) -> None:
        self.engine.pause()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def _reset(self) -> None:
        await self._stop_run_task()
        self._clear_agent_profile_output()
        await asyncio.to_thread(self.engine.reset)
        await self.send(self.engine.init_payload())
        await self.status("模擬已重設")

    async def _set_agents(self, n: int) -> None:
        if self.engine.running or self.engine.scheduler.cycle > 0:
            await self.status("模擬進行中無法變更 agent 數，請先重設。")
            return
        n = max(UI_CONFIG.agents_min, min(n, UI_CONFIG.agents_max))
        self._clear_agent_profile_output()
        self.cfg = dataclasses.replace(self.cfg, nb_agents=n)
        await self._stop_run_task()
        self.engine = SimulationEngine(self.cfg)
        await asyncio.to_thread(self.engine.initialize)
        await self.send(self.engine.init_payload())
        await self.status(f"agent 數設為 {n}")

    def _clear_agent_profile_output(self) -> None:
        profile_path = config.OUTPUT_DIR / _AGENT_PROFILE_FILENAME
        try:
            profile_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("無法刪除 agent profile output: %s", e)

    def _set_mode(self, mode: str) -> None:
        # 動態切換 mock/llm；engine 內部 cfg 與本 session cfg 同步
        self.cfg = dataclasses.replace(self.cfg, use_llm=(mode == "llm"))
        self.engine.cfg = self.cfg
