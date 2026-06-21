import logging

logger = logging.getLogger(__name__)


def output_process(response, output_path, file_name):
    if not response:
        raise ValueError("LLM response是空的")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(response)

    # 原本 print(response[:50]) 會把原始 LLM 文字噴進 console（併發時很亂）→ 降為 DEBUG。
    logger.debug("已寫出 %s 輸出 → %s（%d 字）", file_name, output_path, len(response))
