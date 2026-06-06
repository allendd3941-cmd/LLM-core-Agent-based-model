from pathlib import Path
from timer import time_counter
import requests
from llm_config import OLLAMA_URL, OLLAMA_MODEL, OLLAMA_MODE
from output_engine import output_process

BASE_DIR = Path(__file__).resolve().parent
FILE_NAME = Path(__file__).stem

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "perception_prompt.txt"
OUTPUT_PATH = BASE_DIR / "output" 

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(USER_PROMPT_PATH, "r", encoding="utf-8") as f:
    USER_PROMPT = f.read()

count = 0

def run_perception(gama_body, output: bool= False):
    global count
    count += 1

    url = f"{OLLAMA_URL}{OLLAMA_MODE}"

    user_prompt = f"{USER_PROMPT} \n {gama_body}"
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": user_prompt,
        "system": SYSTEM_PROMPT,
        #"format": "json",
        "think": "low",
        "options": {
            "seed": 42 ,
            "temperature": 0,
            "top_k": 1,
            "num_predict": 10000
        },
        "stream": False
    }

    @time_counter
    def request_with_timeout(url, payload, file_name : str = FILE_NAME):
        response = requests.post(url, json = payload)
        response.raise_for_status()  # 確保 HTTP 狀態碼為 200
        return response

    http_response = request_with_timeout(url, payload, file_name=FILE_NAME) 
    response_data = http_response.json()
    final_response = response_data["response"]

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_{count}.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    run_perception(output=False)#沒法單獨執行但寫著
