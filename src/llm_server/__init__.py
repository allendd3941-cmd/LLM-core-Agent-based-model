"""llm_server — LLM 決策 pipeline（Ollama）。

供模擬器（``llm_abm_simulator``）以 in-process adapter 直接呼叫：
``agent_profile`` → ``perception`` → ``decision_making``（可選 ``memory_summary``），
搭配 ``prompts/`` 範本與 ``json_utils`` 強韌解析。Ollama 連線設定見 ``llm_config``。
"""

from __future__ import annotations
