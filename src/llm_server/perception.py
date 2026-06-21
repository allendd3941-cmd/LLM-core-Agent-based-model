"""perception.py — 由結構化 step payload「確定性」組出環境感知文字（不再呼叫 LLM）。

原本 run_perception 會把 payload 丟給 LLM 再「整理」一次，但 payload 已是結構化質性資料
（overall_traffic / congestion_hotspots / 每車 traffic_here·road_ahead…），這層 LLM 呼叫是
冗餘的——多一次往返、每批多送 ~1.3k token 的 perception 模板。改成確定性模板後：
  - 每批 LLM 呼叫由 2 次（perception+decision）降為 1 次（decision）
  - 移除 perception 模板與其 LLM 成本/延遲
  - 輸出格式穩定、零隨機、可重現

輸出文字直接供 decision_making 使用。保留 ``run_perception(gama_body, output=False)`` 簽章，
呼叫端無須改動；``output=True`` 時才把文字落檔（除錯用，預設關閉）。
"""

from __future__ import annotations

from pathlib import Path

OUTPUT_PATH = Path(__file__).resolve().parent.parent.parent / "output"
count = 0


def _fmt_global(env: dict) -> list[str]:
    lines = ["【全域路況】"]
    lines.append(
        f"整體交通：{env.get('overall_traffic', '未知')}；"
        f"壅塞趨勢：{env.get('congestion_trend', '未知')}；"
        f"目的地：{env.get('destination_town', '')}"
    )
    hotspots = env.get("congestion_hotspots") or []
    if hotspots:
        hs = "；".join(
            f"{h.get('town', '?')}（{h.get('level', '?')}／{h.get('crowded_roads', '?')}條壅塞）"
            for h in hotspots
        )
        lines.append(f"壅塞熱點：{hs}")
    else:
        lines.append("壅塞熱點：無")
    return lines


def _fmt_agent(a: dict) -> str:
    e = a.get("environment", {}) or {}
    mem = a.get("memory", {}) or {}
    name = a.get("agent_name") or a.get("agent_id", "?")
    return (
        f"・{name}："
        f"位於 {e.get('current_town', '?')}／{e.get('current_road', '?')}，"
        f"腳下{e.get('traffic_here', '?')}、{e.get('speed_status', '?')}、前方{e.get('road_ahead', '?')}，"
        f"距終點約 {e.get('distance_to_destination_m', '?')} 公尺、鄰近 {e.get('nearby_agent_count', '?')} 車，"
        f"現用模式「{a.get('action_mode', '?')}」，旅次印象：{mem.get('summary', '') or '（無）'}"
    )


def global_situation_text(perception_text: str) -> str:
    """從環境感知文字抽出【全域路況】區塊（不含各車狀況）。

    供 RAG 多重查詢的「路況」子查詢使用：用全域壅塞情勢去搜知識庫，避免把
    各車個別狀況（雜訊）也丟進查詢。找不到區塊時回整段（保底，不會壞）。
    """
    text = perception_text or ""
    start = text.find("【全域路況】")
    if start == -1:
        return text.strip()
    rest = text[start:]
    nxt = rest.find("【各車當前狀況】")
    return (rest if nxt == -1 else rest[:nxt]).strip()


def agents_situation_text(perception_text: str) -> str:
    """從環境感知文字抽出【各車當前狀況】區塊（不含全域路況）。

    與 global_situation_text 互補：decision prompt 把「每步相同的全域路況」放到共用前綴、
    「每批不同的各車狀況」放後面，提高 vLLM prefix cache 命中率。找不到區塊時回整段（保底）。
    """
    text = perception_text or ""
    start = text.find("【各車當前狀況】")
    if start == -1:
        return text.strip()
    return text[start:].strip()


def run_perception(gama_body, output: bool = False) -> str:
    """把 step payload（{environment, agents_status}）確定性組成環境感知文字。"""
    global count
    count += 1

    body = gama_body if isinstance(gama_body, dict) else {}
    env = body.get("environment", {}) or {}
    agents = body.get("agents_status", []) or []

    lines = _fmt_global(env)
    lines.append("")
    lines.append("【各車當前狀況】")
    lines.extend(_fmt_agent(a) for a in agents)
    text = "\n".join(lines)

    if output:  # 除錯落檔（預設關閉；不影響模擬）
        try:
            OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
            (OUTPUT_PATH / f"perception_output_{count}.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text
