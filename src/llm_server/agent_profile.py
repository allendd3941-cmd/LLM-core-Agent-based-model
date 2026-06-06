from pathlib import Path
import requests
from timer import time_counter
from llm_config import OLLAMA_URL, OLLAMA_MODE, OLLAMA_MODEL
from output_engine import output_process
from schemas.agentprofile_schema import AgentProfileSchema

BASE_DIR = Path(__file__).resolve().parent
FILE_NAME = Path(__file__).stem

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "agentprofile_prompt.txt"
OUTPUT_PATH = BASE_DIR / "output" 

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(USER_PROMPT_PATH, "r", encoding="utf-8") as f:
    USER_PROMPT = f.read()

count = 0

def run_agent_profile(output: bool= False):
    global count
    count += 1

    url = f"{OLLAMA_URL}{OLLAMA_MODE}"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": USER_PROMPT,
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
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_{count}.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    run_agent_profile(output=True)

