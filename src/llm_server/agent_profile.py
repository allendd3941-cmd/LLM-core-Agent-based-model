from pathlib import Path
import requests
from .timer import time_counter
from .llm_config import OLLAMA_URL, OLLAMA_MODE, OLLAMA_MODEL
from .output_engine import output_process
from .schemas.agentprofile_schema import AgentProfileSchema

BASE_DIR = Path(__file__).resolve().parent
FILE_NAME = Path(__file__).stem

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "agentprofile_prompt.txt"
OUTPUT_PATH = BASE_DIR.parent.parent / "output"

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(USER_PROMPT_PATH, "r", encoding="utf-8") as f:
    USER_PROMPT = f.read()


def build_user_prompt(agent_count: int) -> str:
    return (
        f"請生成 {agent_count} 個用於「台南市亞太棒球場球賽進出場尖峰人潮短期交通衝擊評估」的交通模擬 agents。\n\n"
        + USER_PROMPT
    )


def run_agent_profile(output: bool = False, agent_count: int = 10):

    url = f"{OLLAMA_URL}{OLLAMA_MODE}"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_user_prompt(agent_count),
        "system": SYSTEM_PROMPT,
        #"format": AgentProfileSchema.model_json_schema(),
        #"think": "low",
        "options": {
            "seed": 42
        },
        "stream": False
    }

    @time_counter
    def request_with_timeout(url, payload, file_name : str = FILE_NAME):
        response = requests.post(url, json = payload)
        response.raise_for_status()  
        return response

    http_response = request_with_timeout(url, payload, file_name=FILE_NAME) 
    response_data = http_response.json()
    final_response = response_data["response"]

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_1.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    run_agent_profile(output=True)

