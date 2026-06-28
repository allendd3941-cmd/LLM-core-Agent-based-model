"""sim_chat.py — 「暫停對話查詢」：根據當前模擬狀態用 LLM 回答使用者問題（唯讀）。

只把引擎組好的「當前狀態文字」+ 使用者問題送 LLM，請它據此回答（不杜撰未提供的數據）。
與整套 LLM 共用 llm_client（後端/模型由前端選擇器決定）。失敗由上層 fallback 成附狀態文字。
"""

from __future__ import annotations

from . import llm_client

SYSTEM_PROMPT = (
    "You are the assistant of a traffic-simulation digital twin. Answer the user's question concisely "
    "in English, based ONLY on the provided 'current simulation state'; do not invent data that was not "
    "given; if the state does not contain the answer, say so plainly."
)


def run_sim_chat(state_text: str, question: str) -> str:
    prompt = (
        f"[Current simulation state]\n{state_text}\n\n"
        f"[User question]\n{question}\n\n"
        "Answer based on the state above:"
    )
    return llm_client.generate(
        prompt, system=SYSTEM_PROMPT, options={"seed": 42}, think="low", label="sim_chat")
