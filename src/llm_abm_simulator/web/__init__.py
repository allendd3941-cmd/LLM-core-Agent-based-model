"""web — 薄的 FastAPI / WebSocket 展示層。

設計原則（對齊計畫）：HTTP/WebSocket handler 保持薄，不含任何模擬邏輯；
所有模擬狀態由 ``simulation.SimulationEngine`` 擁有。handler 只負責：
接收控制指令 → 在背景執行緒呼叫 engine.step（避免阻塞事件迴圈）→ 廣播狀態快照。
"""

from __future__ import annotations

from .app import app, create_app

__all__ = ["app", "create_app"]
