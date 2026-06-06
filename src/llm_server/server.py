from pathlib import Path
from fastapi import FastAPI
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from agent_profile import run_agent_profile
from perception import run_perception
from decision_making import run_decision_making
from od_converter import convert_to_od_csv

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "output" 
AGENT_PROFILE_DIR = OUTPUT_PATH / "agent_profile_output_1.txt"

#uvicorn server:app --host 127.0.0.1 --port 8000 --reload
#檔名:伺服器名 -> server:app
app = FastAPI()

class GamaAgent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    agent_name: str = Field(alias="agent name")
    travel_memory: list[dict[str, Any]] = Field(default_factory=list)
    current_state: dict[str, Any] = Field(default_factory=dict)


class GamaRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    request_type: str | None = None
    model_name: str | None = None
    cycle: int
    agents: list[GamaAgent] = Field(default_factory=list)
    requested_agents: list[dict[str, Any]] = Field(default_factory=list)
    agents_status: list[dict[str, Any]] = Field(default_factory=list)
    roads_flow: list[dict[str, Any]] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)


#要寫一個迴圈，step總數到了就要break
@app.post("/from-gama")
def receive_from_gama(gama_body: GamaRequest):
    print("收到 GAMA 請求")

    if not AGENT_PROFILE_DIR.exists():
        run_agent_profile(output=True)

    agent_profile = Path.read_text(AGENT_PROFILE_DIR, encoding="utf-8")

    perception = run_perception(gama_body, output=True)
    decision_making = run_decision_making(agent_profile, perception, output=True)

    return decision_making




