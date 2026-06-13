from pathlib import Path
from . import llm_client
from . import prompt_store
from .output_engine import output_process

BASE_DIR = Path(__file__).resolve().parent
FILE_NAME = Path(__file__).stem

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "agentprofile_prompt.txt"
OUTPUT_PATH = BASE_DIR.parent.parent / "output"

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(USER_PROMPT_PATH, "r", encoding="utf-8") as f:
    USER_PROMPT = f.read()

prompt_store.register_default("agent_profile", USER_PROMPT)


def build_user_prompt(agent_count: int) -> str:
    return (
        f"請生成 {agent_count} 個用於「台南市亞太棒球場球賽進出場尖峰人潮短期交通衝擊評估」的交通模擬 agents。\n\n"
        + prompt_store.get("agent_profile")   # 前端可即時覆寫；未覆寫＝預設
    )


def run_agent_profile(output: bool = False, agent_count: int = 10, seed: int = 42):
    # seed 可由呼叫端帶入：分批生成 persona 池時，每批用不同 seed 以避免「每批生出一模一樣的人」
    # （同一 seed＋同 prompt → 相同輸出），同時 seed 由批次序號決定 → 仍可重現。
    final_response = llm_client.generate(
        build_user_prompt(agent_count), system=SYSTEM_PROMPT,
        options={"seed": seed}, label=FILE_NAME)

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_1.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    run_agent_profile(output=True)

