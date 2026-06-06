def output_process(response, output_path, file_name):
    print(response[:50])

    if not response:
        raise ValueError("Ollama response是空的")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(response)
        print(F"已完成{file_name}的output。")
