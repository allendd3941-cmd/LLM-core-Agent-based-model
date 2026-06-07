import os
from dotenv import load_dotenv

load_dotenv()

# 缺 .env 時退回對「本機 Ollama」的合理預設，避免組出 "NoneNone" 這種無效 URL。
# 仍可用 .env 覆寫（連到別台機器/GPU 或換 endpoint）。
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_MODE = os.getenv("OLLAMA_MODE", "/api/generate")