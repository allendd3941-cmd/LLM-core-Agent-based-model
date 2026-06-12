from .RAG import RAG
import json
from pathlib import Path
from . import llm_client
from .agent_profile import run_agent_profile
from .perception import run_perception
from .output_engine import output_process

BASE_DIR = Path(__file__).resolve().parent
FILE_NAME = Path(__file__).stem

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "decision_making_prompt.txt"
OUTPUT_PATH = BASE_DIR.parent.parent / "output"

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(USER_PROMPT_PATH, "r", encoding="utf-8") as f:
    USER_PROMPT = f.read()

count = 0

def run_decision_making(agent_profile_data, perception_data, output: bool= False):
    global count
    count += 1

    #retrieved_texts =RAG(agent_profile_data, perception_data)

    user_prompt = f'''{USER_PROMPT} \n
    {perception_data}\n
    "agent profile資料"如下:\n
    {agent_profile_data}
    '''

    final_response = llm_client.generate(
        user_prompt, system=SYSTEM_PROMPT,
        options={"seed": 42}, think="low", label=FILE_NAME)

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_{count}.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    agent_profile_data = run_agent_profile()
    perception_data = run_perception()
    run_decision_making(agent_profile_data, perception_data, output=False)
