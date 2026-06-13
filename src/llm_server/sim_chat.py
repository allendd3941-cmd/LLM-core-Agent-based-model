"""sim_chat.py — 「暫停對話查詢」：根據當前模擬狀態用 LLM 回答使用者問題（唯讀）。

只把引擎組好的「當前狀態文字」+ 使用者問題送 LLM，請它據此回答（不杜撰未提供的數據）。
與整套 LLM 共用 llm_client（後端/模型由前端選擇器決定）。失敗由上層 fallback 成附狀態文字。
"""

from __future__ import annotations

from . import llm_client

SYSTEM_PROMPT = (
    "你是交通模擬數位分身的助理。只根據使用者提供的『當前模擬狀態』，"
    "用繁體中文簡潔回答問題，不杜撰未提供的數據；若狀態中沒有答案就明說。"
)


def run_sim_chat(state_text: str, question: str) -> str:
    prompt = (
        f"【當前模擬狀態】\n{state_text}\n\n"
        f"【使用者問題】\n{question}\n\n"
        "請根據上述狀態回答："
    )
    return llm_client.generate(
        prompt, system=SYSTEM_PROMPT, options={"seed": 42}, think="low", label="sim_chat")
