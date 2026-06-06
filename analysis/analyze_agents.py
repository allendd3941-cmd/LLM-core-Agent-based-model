from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("agents_samples.json")
DEFAULT_OUTPUT = Path("agents_analysis.json")
DEFAULT_CSV = Path("agents_analysis_rows.csv")
UNKNOWN = "Unknown"

REQUIRED_IDENTITY_FIELDS = (
    "name",
    "age",
    "occupation",
    "wage",
    "household_income",
    "vehicle_ownership",
    "residential_location",
)
REQUIRED_TRAIT_FIELDS = (
    "attitudes",
    "habits",
    "decision_making_tendencies",
    "economic_preferences_and_tradeoffs",
)

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
FULL_WIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

VEHICLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "汽車": (
        "汽車",
        "開車",
        "自駕",
        "轎車",
        "休旅車",
        "私家車",
        "自小客",
        "家用車",
        "car",
        "private_car",
        "one_car",
        "multi_vehicle",
    ),
    "機車": (
        "機車",
        "騎車",
        "摩托車",
        "scooter",
        "motorcycle",
        "motorcycle_only",
    ),
    "大眾運輸": (
        "大眾運輸",
        "公共運輸",
        "公車",
        "巴士",
        "客運",
        "火車",
        "台鐵",
        "高鐵",
        "捷運",
        "接駁車",
        "接駁",
        "no_private_vehicle",
    ),
    "步行/自行車": (
        "步行",
        "走路",
        "自行車",
        "腳踏車",
        "單車",
        "ubike",
        "微笑單車",
    ),
    "計程車/叫車": (
        "計程車",
        "小黃",
        "叫車",
        "uber",
        "ride_hailing",
        "多元計程車",
    ),
}

PRIMARY_MODE_PATTERNS: dict[str, tuple[str, ...]] = {
    "汽車": (
        r"(主要|平常|通常|多半|習慣|傾向|偏好|優先).{0,12}(開車|汽車|自駕|轎車|休旅車)",
        r"(開車|自駕).{0,8}(進出|前往|到達|通勤|觀賽|接送)",
    ),
    "機車": (
        r"(主要|平常|通常|多半|習慣|傾向|偏好|優先).{0,12}(機車|騎車|摩托車)",
        r"(騎機車|騎車).{0,8}(進出|前往|到達|通勤|觀賽|接送)",
    ),
    "大眾運輸": (
        r"(主要|平常|通常|多半|習慣|傾向|偏好|優先).{0,12}(公車|火車|台鐵|高鐵|接駁|大眾運輸|公共運輸)",
        r"(搭乘|搭).{0,8}(公車|火車|台鐵|高鐵|接駁|大眾運輸|公共運輸)",
    ),
    "步行/自行車": (
        r"(主要|平常|通常|多半|習慣|傾向|偏好|優先).{0,12}(步行|走路|自行車|腳踏車|單車)",
        r"(步行|走路|騎自行車|騎單車).{0,8}(進出|前往|到達|通勤|觀賽)",
    ),
    "計程車/叫車": (
        r"(主要|平常|通常|多半|習慣|傾向|偏好|優先).{0,12}(計程車|小黃|叫車|uber)",
        r"(搭乘|搭|叫).{0,8}(計程車|小黃|uber)",
    ),
}

ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "球賽觀眾": (
        "球迷",
        "觀眾",
        "看球",
        "觀賽",
        "進場",
        "散場",
        "棒球賽",
        "球賽",
        "球票",
        "spectator",
        "baseball_game_attendance",
    ),
    "場館工作人員": (
        "場館工作",
        "球場工作",
        "工作人員",
        "保全",
        "清潔",
        "售票",
        "驗票",
        "引導人員",
        "工讀生",
    ),
    "接送者": (
        "接送",
        "載孩子",
        "載家人",
        "載朋友",
        "等人",
        "家長",
        "司機",
    ),
    "附近居民": (
        "附近居民",
        "周邊居民",
        "附近住戶",
        "當地居民",
        "住在球場附近",
        "住家",
    ),
    "一般通勤者": (
        "通勤",
        "上班",
        "下班",
        "上課",
        "下課",
        "公司",
        "工廠",
        "辦公室",
        "學生",
        "教師",
        "commute",
        "regular",
    ),
    "商家/服務業": (
        "店家",
        "商家",
        "餐飲",
        "服務業",
        "店員",
        "攤商",
        "外送",
        "超商",
    ),
}

TRAFFIC_KEYWORDS = (
    "準時",
    "提早",
    "延誤",
    "壅塞",
    "塞車",
    "停車",
    "接駁",
    "公車",
    "火車",
    "步行",
    "自行車",
    "機車",
    "汽車",
    "共乘",
    "接送",
    "省錢",
    "成本",
    "時間",
    "安全",
    "熟悉",
    "避開",
    "彈性",
    "等待",
    "尖峰",
    "球賽",
    "人潮",
)

TAINAN_DISTRICTS = (
    "中西區",
    "東區",
    "南區",
    "北區",
    "安平區",
    "安南區",
    "永康區",
    "歸仁區",
    "新化區",
    "左鎮區",
    "玉井區",
    "楠西區",
    "南化區",
    "仁德區",
    "關廟區",
    "龍崎區",
    "官田區",
    "麻豆區",
    "佳里區",
    "西港區",
    "七股區",
    "將軍區",
    "學甲區",
    "北門區",
    "新營區",
    "後壁區",
    "白河區",
    "東山區",
    "六甲區",
    "下營區",
    "柳營區",
    "鹽水區",
    "善化區",
    "大內區",
    "山上區",
    "新市區",
    "安定區",
)
OTHER_CITY_KEYWORDS = (
    "高雄",
    "嘉義",
    "屏東",
    "台中",
    "臺中",
    "台北",
    "臺北",
    "新北",
    "桃園",
    "彰化",
    "雲林",
)


@dataclass(slots=True)
class AgentAnalysisRow:
    agent_index: int
    name: str
    occupation_text: str
    role: str
    age_text: str
    age_group: str
    vehicle_text: str
    primary_vehicle: str
    residential_location_text: str
    residential_area: str
    wage_text: str
    wage_group: str
    household_income_text: str
    household_income_group: str
    warning_count: int
    warnings: list[str]


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_value(text: str) -> Any:
    stripped = strip_code_fence(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("找不到可解析的 JSON 內容。")


def extract_agents(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ValueError("輸入 JSON 最外層必須是 array 或 object。")

    if isinstance(payload.get("agents"), list):
        return [item for item in payload["agents"] if isinstance(item, dict)]

    if isinstance(payload.get("agents"), str):
        return extract_agents(parse_json_value(payload["agents"]))

    if isinstance(payload.get("response"), str):
        return extract_agents(parse_json_value(payload["response"]))

    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return extract_agents(parse_json_value(message["content"]))

    for key in ("agent_samples", "profiles", "items", "data"):
        if isinstance(payload.get(key), list):
            return [item for item in payload[key] if isinstance(item, dict)]

    if {"identity", "traits"}.issubset(payload.keys()):
        return [payload]

    raise ValueError("無法在輸入 JSON 中找到 agent list。")


def load_agents(path: Path) -> list[dict[str, Any]]:
    payload = parse_json_value(path.read_text(encoding="utf-8-sig"))
    return extract_agents(payload)


def normalize_digits(text: str) -> str:
    return text.translate(FULL_WIDTH_DIGITS)


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(text_of(item) for item in value if text_of(item)).strip()
    if isinstance(value, dict):
        return " ".join(text_of(item) for item in value.values() if text_of(item)).strip()
    return str(value).strip()


def identity_of(agent: dict[str, Any]) -> dict[str, Any]:
    identity = agent.get("identity", {})
    return identity if isinstance(identity, dict) else {}


def traits_of(agent: dict[str, Any]) -> dict[str, Any]:
    traits = agent.get("traits", {})
    return traits if isinstance(traits, dict) else {}


def full_agent_text(agent: dict[str, Any]) -> str:
    return text_of(agent)


def chinese_to_int(token: str) -> int | None:
    if not token:
        return None

    total = 0
    section = 0
    number = 0
    units = {"十": 10, "百": 100, "千": 1000}

    for char in token:
        if char in CHINESE_DIGITS:
            number = CHINESE_DIGITS[char]
        elif char in units:
            unit = units[char]
            section += (number or 1) * unit
            number = 0
        elif char == "萬":
            section += number
            total += section * 10000
            section = 0
            number = 0
        else:
            return None

    return total + section + number


def age_to_group(age: int | None) -> str:
    if age is None:
        return UNKNOWN
    if age < 20:
        return "Under 20"
    if age < 30:
        return "20-29"
    if age < 40:
        return "30-39"
    if age < 50:
        return "40-49"
    if age < 60:
        return "50-59"
    return "60+"


def extract_age(age_text: str) -> int | None:
    text = normalize_digits(age_text)
    if not text:
        return None

    range_match = re.search(r"(\d{1,3})\s*(?:-|~|到|至|_to_)\s*(\d{1,3})", text)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if 0 < start <= end <= 120:
            return round((start + end) / 2)

    exact_match = re.search(r"(\d{1,3})(?:\s*多)?\s*(?:歲|歲左右|歲上下|出頭|左右|上下)?", text)
    if exact_match:
        age = int(exact_match.group(1))
        if 0 < age <= 120:
            if "多歲" in text or "多" in text[exact_match.end() : exact_match.end() + 2]:
                return min(age - age % 10 + 5, 120)
            if "出頭" in text:
                return min(age + 2, 120)
            return age

    chinese_match = re.search(
        r"([零〇一二兩三四五六七八九十百]{1,6})(多歲|歲|歲左右|歲上下|出頭|左右|上下)",
        text,
    )
    if chinese_match:
        age = chinese_to_int(chinese_match.group(1))
        if age and 0 < age <= 120:
            suffix = chinese_match.group(2)
            if suffix == "多歲":
                return min(age - age % 10 + 5, 120)
            if suffix == "出頭":
                return min(age + 2, 120)
            return age

    return None


def classify_age(agent: dict[str, Any]) -> tuple[str, str]:
    identity = identity_of(agent)
    age_text = text_of(identity.get("age") or identity.get("age_group"))
    return age_text, age_to_group(extract_age(age_text))


def keyword_scores(text: str, keyword_map: dict[str, tuple[str, ...]]) -> Counter[str]:
    lowered = text.lower()
    scores: Counter[str] = Counter()
    for category, keywords in keyword_map.items():
        for keyword in keywords:
            count = lowered.count(keyword.lower())
            if count:
                scores[category] += count
    return scores


def classify_vehicle(agent: dict[str, Any]) -> tuple[str, str]:
    identity = identity_of(agent)
    traits = traits_of(agent)
    vehicle_text = text_of(identity.get("vehicle_ownership"))
    decision_text = " ".join(
        (
            vehicle_text,
            text_of(traits.get("habits")),
            text_of(traits.get("decision_making_tendencies")),
            text_of(traits.get("economic_preferences_and_tradeoffs")),
            text_of(traits.get("travel_habits")),
            text_of(traits.get("decision_tendencies")),
            text_of(traits.get("economic_preferences")),
            text_of(traits.get("additional_metadata")),
        )
    )
    scores = keyword_scores(decision_text, VEHICLE_KEYWORDS)

    for category, patterns in PRIMARY_MODE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, decision_text, flags=re.IGNORECASE):
                scores[category] += 4

    no_private_vehicle = re.search(r"(沒有|無|不具備|不持有).{0,8}(自有|私人|私有)?(車|車輛|運具)", decision_text)
    no_car = re.search(r"(沒有|無|不具備|不持有).{0,8}(汽車|自用車|私家車|轎車)", decision_text)
    no_motorcycle = re.search(r"(沒有|無|不具備|不持有).{0,8}(機車|摩托車)", decision_text)
    if no_private_vehicle:
        scores["汽車"] -= 2
        scores["機車"] -= 2
    if no_car:
        scores["汽車"] -= 3
    if no_motorcycle:
        scores["機車"] -= 3

    if not scores or max(scores.values()) <= 0:
        if no_private_vehicle:
            return vehicle_text, "無私人運具/不明"
        return vehicle_text, UNKNOWN

    return vehicle_text, scores.most_common(1)[0][0]


def classify_role(agent: dict[str, Any]) -> str:
    text = full_agent_text(agent)
    scores = keyword_scores(text, ROLE_KEYWORDS)
    if not scores or max(scores.values()) <= 0:
        return UNKNOWN

    priority = list(ROLE_KEYWORDS.keys())
    return max(scores, key=lambda category: (scores[category], -priority.index(category)))


def classify_location(location_text: str) -> str:
    text = location_text.strip()
    if not text:
        return UNKNOWN

    for district in sorted(TAINAN_DISTRICTS, key=len, reverse=True):
        if district in text:
            return district

    for district in sorted(TAINAN_DISTRICTS, key=len, reverse=True):
        district_name = district.removesuffix("區")
        if district_name and district_name in text:
            return district

    for city in OTHER_CITY_KEYWORDS:
        if city in text:
            normalized = city.replace("臺", "台")
            return f"{normalized}地區"

    if "台南" in text or "臺南" in text or "tainan" in text.lower():
        return "台南市未細分"

    if "外縣市" in text or "外地" in text or "外站" in text or "outstation" in text.lower():
        return "外縣市"

    return "其他/未辨識地區"


def parse_money_amount(text: str) -> float | None:
    normalized = normalize_digits(text.replace(",", ""))
    if not normalized:
        return None

    amounts: list[float] = []

    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(萬|千)?", normalized):
        value = float(match.group(1))
        unit = match.group(2)
        if unit == "萬":
            value *= 10000
        elif unit == "千":
            value *= 1000
        if 1000 <= value <= 50_000_000:
            amounts.append(value)

    chinese_wan_match = re.search(
        r"([零〇一二兩三四五六七八九十百千]+)萬(?:([零〇一二兩三四五六七八九十百千]+)千)?",
        normalized,
    )
    if chinese_wan_match:
        wan = chinese_to_int(chinese_wan_match.group(1)) or 0
        thousand = chinese_to_int(chinese_wan_match.group(2) or "") or 0
        value = wan * 10000 + thousand * 1000
        if value:
            amounts.append(float(value))

    chinese_thousand_match = re.search(r"([零〇一二兩三四五六七八九十百]+)千", normalized)
    if chinese_thousand_match:
        thousand = chinese_to_int(chinese_thousand_match.group(1)) or 0
        if thousand:
            amounts.append(float(thousand * 1000))

    if not amounts:
        return None

    amount = max(amounts)
    if "年薪" in normalized or "年收入" in normalized or "每年" in normalized:
        amount /= 12
    return amount


def income_group(text: str, kind: str) -> str:
    if not text.strip():
        return UNKNOWN
    if any(keyword in text for keyword in ("低收入", "中低收入", "收入有限")):
        return "低收入"
    if any(keyword in text for keyword in ("高收入", "收入寬裕", "收入穩定且偏高")):
        return "高收入"

    amount = parse_money_amount(text)
    if amount is None:
        return UNKNOWN

    if kind == "household":
        if amount < 50_000:
            return "5萬以下/月"
        if amount < 100_000:
            return "5-10萬/月"
        if amount < 150_000:
            return "10-15萬/月"
        return "15萬以上/月"

    if amount < 30_000:
        return "3萬以下/月"
    if amount < 50_000:
        return "3-5萬/月"
    if amount < 80_000:
        return "5-8萬/月"
    return "8萬以上/月"


def validate_agent_shape(agent: dict[str, Any], index: int) -> list[str]:
    warnings: list[str] = []
    identity = agent.get("identity")
    traits = agent.get("traits")
    memory = agent.get("memory")

    if not isinstance(identity, dict):
        warnings.append(f"agent[{index}].identity missing or not object")
        identity = {}
    if not isinstance(traits, dict):
        warnings.append(f"agent[{index}].traits missing or not object")
        traits = {}
    if memory is not None and not isinstance(memory, dict):
        warnings.append(f"agent[{index}].memory is not object")

    for field in REQUIRED_IDENTITY_FIELDS:
        if not text_of(identity.get(field)):
            warnings.append(f"agent[{index}].identity.{field} empty")

    for field in REQUIRED_TRAIT_FIELDS:
        value = traits.get(field)
        if value is None:
            warnings.append(f"agent[{index}].traits.{field} missing")
        elif not isinstance(value, list):
            warnings.append(f"agent[{index}].traits.{field} should be list")

    if isinstance(memory, dict):
        for field in ("short_term_memory", "long_term_memory"):
            value = memory.get(field)
            if value is None:
                warnings.append(f"agent[{index}].memory.{field} missing")
            elif not isinstance(value, list):
                warnings.append(f"agent[{index}].memory.{field} should be list")

    return warnings


def analyze_agent(agent: dict[str, Any], index: int) -> AgentAnalysisRow:
    identity = identity_of(agent)
    warnings = validate_agent_shape(agent, index)
    age_text, age_group = classify_age(agent)
    vehicle_text, primary_vehicle = classify_vehicle(agent)
    wage_text = text_of(identity.get("wage") or identity.get("income_group"))
    household_income_text = text_of(identity.get("household_income"))
    residential_location_text = text_of(identity.get("residential_location"))

    return AgentAnalysisRow(
        agent_index=index,
        name=text_of(identity.get("name") or identity.get("agent_id")) or f"agent_{index:04d}",
        occupation_text=text_of(identity.get("occupation") or identity.get("occupation_or_role")),
        role=classify_role(agent),
        age_text=age_text,
        age_group=age_group,
        vehicle_text=vehicle_text,
        primary_vehicle=primary_vehicle,
        residential_location_text=residential_location_text,
        residential_area=classify_location(residential_location_text),
        wage_text=wage_text,
        wage_group=income_group(wage_text, "wage"),
        household_income_text=household_income_text,
        household_income_group=income_group(household_income_text, "household"),
        warning_count=len(warnings),
        warnings=warnings,
    )


def count_trait_keywords(agent: dict[str, Any]) -> Counter[str]:
    traits_text = text_of(traits_of(agent))
    counter: Counter[str] = Counter()
    for keyword in TRAFFIC_KEYWORDS:
        if keyword in traits_text:
            counter[keyword] += 1
    return counter


def build_analysis(agents: list[dict[str, Any]], source: Path) -> dict[str, Any]:
    rows = [analyze_agent(agent, index + 1) for index, agent in enumerate(agents)]
    trait_counter: Counter[str] = Counter()
    warning_counter: Counter[str] = Counter()
    for agent, row in zip(agents, rows):
        trait_counter.update(count_trait_keywords(agent))
        for warning in row.warnings:
            warning_counter[warning.split("].")[-1]] += 1

    return {
        "source_file": str(source),
        "total_agents": len(rows),
        "distributions": {
            "age_group": dict(Counter(row.age_group for row in rows)),
            "primary_vehicle": dict(Counter(row.primary_vehicle for row in rows)),
            "role": dict(Counter(row.role for row in rows)),
            "residential_area": dict(Counter(row.residential_area for row in rows)),
            "wage_group": dict(Counter(row.wage_group for row in rows)),
            "household_income_group": dict(Counter(row.household_income_group for row in rows)),
        },
        "top_trait_keywords": dict(trait_counter.most_common()),
        "schema_quality": {
            "agents_with_warnings": sum(1 for row in rows if row.warning_count),
            "warning_total": sum(row.warning_count for row in rows),
            "warning_types": dict(warning_counter.most_common()),
        },
        "agent_rows": [asdict(row) for row in rows],
    }


def write_json(result: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            serializable_row = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
                for key, value in row.items()
            }
            writer.writerow(serializable_row)


def print_summary(result: dict[str, Any]) -> None:
    print(f"已分析 {result['total_agents']} 個 agent。")
    for title, distribution in result["distributions"].items():
        print(f"\n[{title}]")
        if not distribution:
            print("  無資料")
            continue
        for key, count in Counter(distribution).most_common():
            print(f"  {key}: {count}")

    schema_quality = result["schema_quality"]
    print("\n[schema_quality]")
    print(f"  agents_with_warnings: {schema_quality['agents_with_warnings']}")
    print(f"  warning_total: {schema_quality['warning_total']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze natural-language agent profile samples and export statistics."
    )
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="agent JSON input path")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="analysis JSON output path")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="per-agent CSV output path")
    parser.add_argument("--no-csv", action="store_true", help="skip CSV export")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        print(f"找不到輸入檔：{args.input}")
        return 1

    try:
        agents = load_agents(args.input)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"讀取或解析輸入檔失敗：{exc}")
        return 1

    result = build_analysis(agents, args.input)
    write_json(result, args.output)
    if not args.no_csv:
        write_csv(result["agent_rows"], args.csv)

    print_summary(result)
    print(f"\n已輸出 JSON：{args.output}")
    if not args.no_csv:
        print(f"已輸出 CSV：{args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
