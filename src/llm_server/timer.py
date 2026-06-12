"""timer.py — LLM 呼叫計時裝飾器（改用 logging，併發批次下不再交錯亂印）。

設計：
- 每次 LLM 呼叫的「送出 / 完成 + 耗時」降到 DEBUG（預設 INFO 不顯示，避免併發批次洗版）。
- 只有「呼叫過久（每 interval 秒）」才以 WARNING 心跳提示，用來抓住卡住的呼叫。
- 真正的「運行中狀態」由上層（engine 每步的決策摘要）以 INFO 輸出，乾淨且有脈絡。
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


def time_counter(func):
    def _heartbeat(start_time, done_event, label, interval=30):
        # 呼叫超過 interval 秒仍未回 → 週期性 WARNING（抓 hang，不洗版）
        while not done_event.wait(interval):
            logger.warning("⏳ %s 仍在等待 LLM 回應… 已 %.0fs", label, time.perf_counter() - start_time)

    def wrapper(url, payload, file_name: str):
        done_event = threading.Event()
        start_time = time.perf_counter()
        timer_thread = threading.Thread(
            target=_heartbeat, args=(start_time, done_event, file_name), daemon=True
        )
        timer_thread.start()
        logger.debug("▶ %s 送出 LLM 請求", file_name)
        try:
            return func(url, payload)
        finally:
            done_event.set()
            timer_thread.join(timeout=1)
            logger.debug("✓ %s · %.2fs", file_name, time.perf_counter() - start_time)

    return wrapper
