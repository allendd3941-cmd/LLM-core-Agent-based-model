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


def build_user_prompt(agent_count: int, rag_ctx: str = "") -> str:
    return (
        f"請生成 {agent_count} 個用於「台南市亞太棒球場球賽進出場尖峰人潮短期交通衝擊評估」的交通模擬 agents。\n\n"
        + rag_ctx   # RAG 參考知識（有上傳語料且啟用時才有；見 build_profile_rag_context）
        + prompt_store.get("agent_profile")   # 前端可即時覆寫；未覆寫＝預設
    )


def build_profile_rag_context() -> tuple[str, list]:
    """檢索一次 RAG 知識（人口/運具/活動情境）→ (rag_ctx 字串, provenance)。

    與決策端 RAG **共用同一個 rag_store**（前端上傳的語料），只是改用 profile 專屬子查詢。
    無庫/未啟用/空 → ('', [])（降級，不影響生成）。查詢與批次無關 → **整次 persona 生成只檢索這一次、
    所有批共用**（呼叫端 profile_pool._generate 檢索一次後把 rag_ctx 傳給每批）。
    """
    from . import rag_query, rag_store
    if not (rag_store.enabled and rag_store.has_docs()):
        return "", []
    if rag_store.query_mode == "single":
        chunks = rag_store.retrieve("台南市居民人口、運具持有與通勤行為", k=rag_store.DEFAULT_K)
        provenance: list = []
    else:
        subqueries = rag_query.build_profile_subqueries()
        if rag_store.hyde_active():
            subqueries = {tag: rag_query.hyde_expand(q, llm_client.generate)
                          for tag, q in subqueries.items()}
        provenance = rag_store.retrieve_multi(subqueries)
        chunks = [h["chunk"] for h in provenance]
    rag_ctx = ""
    if chunks:
        rag_ctx = "【參考人口/行為知識（RAG，請據此生成貼近真實的人物）】\n" + "\n---\n".join(chunks) + "\n\n"
    return rag_ctx, provenance


def run_agent_profile(output: bool = False, agent_count: int = 10, seed: int = 42, rag_ctx: str = ""):
    # seed 可由呼叫端帶入：分批生成 persona 池時，每批用不同 seed 以避免「每批生出一模一樣的人」
    # （同一 seed＋同 prompt → 相同輸出），同時 seed 由批次序號決定 → 仍可重現。
    # rag_ctx：由呼叫端（profile_pool）檢索一次後傳入、各批共用（避免每批都重檢索）。
    final_response = llm_client.generate(
        build_user_prompt(agent_count, rag_ctx), system=SYSTEM_PROMPT,
        options={"seed": seed}, label=FILE_NAME)

    if output:
        output_path = OUTPUT_PATH / f"{FILE_NAME}_output_1.txt"
        output_process(final_response, output_path, FILE_NAME)

    return final_response

if __name__ == "__main__":
    run_agent_profile(output=True)

