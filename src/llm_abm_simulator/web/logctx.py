"""logctx.py — 連線層級的 logging 上下文（session id）。

每個 WebSocket 連線設一個短 id（contextvar），透過 logging.Filter 注入每行 log（`[sess xxxx]`），
讓多連線並行時 console 不交錯難讀。未設時顯示佔位符。``asyncio.to_thread`` 會帶著 context 進
引擎執行緒，故引擎的 log 也會有同一個 sid。
"""

from __future__ import annotations

import contextvars
import logging

session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("sid", default="----")


class SessionIdFilter(logging.Filter):
    """把目前連線的 session id 塞進 log record（供 formatter 的 %(sid)s 使用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.sid = session_id_var.get()
        return True
