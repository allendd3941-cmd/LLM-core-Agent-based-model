import time 
import requests
import threading

def time_counter(func):
    def _print_elapsed_time(start_time, done_event, interval=60):
        while not done_event.wait(interval):
            elapsed = time.perf_counter() - start_time
            print(f"仍在等待 Ollama 回應，已運行 {elapsed:.0f} 秒")

    def wrapper(url, payload, file_name : str):
        done_event = threading.Event()
        start_time = time.perf_counter()

        timer_thread = threading.Thread(
            target= _print_elapsed_time,
            args=(start_time, done_event),
            daemon=True
        )
        timer_thread.start()
        print(f"開始等待{file_name}回應")
        try:
            response = func(url, payload)
            return response
        finally:
            done_event.set()
            timer_thread.join(timeout=1)
            elapsed_time = time.perf_counter() - start_time
            print(f"已收到 response，{file_name}總運行時間 {elapsed_time:.2f} 秒")

    return wrapper