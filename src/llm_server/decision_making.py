import json
from pathlib import Path
from . import llm_client
from . import prompt_store
from . import rag_store
from . import rag_query
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

prompt_store.register_default("decision_making", USER_PROMPT)

count = 0

# 結構化輸出 schema：限制模型只能吐這個形狀的合法 JSON（受限解碼），綁住輸出長度、提升解析成功率。
# 與 decision_making_prompt.txt 的輸出結構、response_parser 的 key 別名一致。
# 註：origin/residential_location 由 persona 決定，決策輸出不再需要它。
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent name": {"type": "string"},
                    "active mode": {
                        "type": "string",
                        "enum": ["fast", "tolerate_congestion", "avoid_congestion",
                                 "comfortable", "short_distance"],
                    },
                    "vehicle_type": {"type": "string", "enum": ["機車", "汽車"]},
                    "reason": {"type": "string"},
                },
                "required": ["agent name", "active mode", "vehicle_type", "reason"],
            },
        }
    },
    "required": ["agents"],
}


def run_decision_making(agent_profile_data, perception_data, output: bool= False):
    """回傳 (LLM 原始文字, RAG provenance)。

    provenance 是本批檢索到的結構化 hit（multi 模式：含 source/idx/via/scores；single 模式：[]），
    隨回傳值往上帶（**不可用模組全域**——批次可能並行，全域會被互相蓋掉）。
    """
    global count
    count += 1

    # RAG：每批全域檢索一次相關知識，注入決策 prompt（控 token；不做 per-agent）。
    # multi＝多重查詢(路況/任務/人格)+RRF；single＝只用 perception 當 query（ablation 對照）。
    rag_ctx = ""
    provenance: list[dict] = []
    if rag_store.enabled and rag_store.has_docs():
        if rag_store.query_mode == "single":
            chunks = rag_store.retrieve(str(perception_data), k=rag_store.DEFAULT_K)
        else:
            subqueries = rag_query.build_subqueries(str(perception_data), str(agent_profile_data))
            # HyDE（選用、長文件才啟用）：各子查詢先改寫成假想文件片段再檢索（標籤保留）。
            if rag_store.hyde_active():
                subqueries = {tag: rag_query.hyde_expand(q, llm_client.generate)
                              for tag, q in subqueries.items()}
            provenance = rag_store.retrieve_multi(subqueries)
            chunks = [h["chunk"] for h in provenance]
        if chunks:
            rag_ctx = "【參考知識（RAG，請在合理時納入考量）】\n" + "\n---\n".join(chunks) + "\n\n"

    user_prompt = f'''{prompt_store.get("decision_making")} \n
    {rag_ctx}{perception_data}\n
    "agent profile資料"如下:\n
    {agent_profile_data}
    '''

    final_response = llm_client.generate(
        user_prompt, system=SYSTEM_PROMPT,
        options={"seed": 42}, think="low", label=FILE_NAME, fmt=DECISION_SCHEMA)

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_{count}.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response, provenance

if __name__ == "__main__":
    agent_profile_data = run_agent_profile()
    perception_data = run_perception()
    run_decision_making(agent_profile_data, perception_data, output=False)
