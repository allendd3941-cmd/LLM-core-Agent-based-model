from __future__ import annotations

import csv
import datetime as dt
import io
import sys
from pathlib import Path

import pandas as pd
import requests

from source_config import SOURCE_SPECS, TAIWAN_CITIES


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "raw" / "GOVTW_CATALOG"
META_DIR = ROOT / "metadata"
LOG_DIR = ROOT / "logs"


KEYWORDS = [
    "紅綠燈",
    "號誌",
    "交通號誌",
    "路口號誌",
    "號誌位置",
    "號誌時制",
    "時相",
    "行人專用時相",
    "行人早開",
    "交通號誌系統號誌",
]

EXCLUDE_TITLE_WORDS = [
    "事故",
    "消費",
    "食品",
    "化粧品",
    "鐵道",
    "研究",
    "報修件數",
    "月分",
    "月份",
]


def ensure_dirs() -> None:
    for path in [RAW_DIR, META_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def download_catalog() -> Path:
    spec = next(s for s in SOURCE_SPECS if s.source_id == "GOVTW_CATALOG")
    output = RAW_DIR / spec.file_name
    response = requests.get(spec.url, timeout=120, headers={"User-Agent": "traffic-light-data-prep/1.0"})
    response.raise_for_status()
    output.write_bytes(response.content)
    return output


def read_catalog(path: Path) -> pd.DataFrame:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    return pd.read_csv(io.StringIO(text), dtype=str, engine="python").fillna("")


def is_candidate(row: pd.Series) -> bool:
    blob = " ".join(
        str(row.get(col, ""))
        for col in ["資料集名稱", "資料集描述", "主要欄位說明", "備註", "提供機關"]
    )
    title = str(row.get("資料集名稱", ""))
    if not any(keyword in blob for keyword in KEYWORDS):
        return False
    if any(word in title for word in EXCLUDE_TITLE_WORDS):
        return False
    return True


def classify_candidate(row: pd.Series) -> str:
    blob = " ".join(str(row.get(col, "")) for col in ["資料集名稱", "資料集描述", "主要欄位說明"])
    has_coord = any(word in blob for word in ["經度", "緯度", "WGS", "座標", "GPS", "圖資", "SHP", "geometry"])
    has_phase = any(word in blob for word in ["時制", "時相", "綠燈", "黃燈", "週期", "運作時段", "早開", "行人專用"])
    if has_coord and has_phase:
        return "點位+時相候選"
    if has_coord:
        return "點位候選"
    if has_phase:
        return "時相候選"
    return "其他號誌候選"


def write_search_audit(candidates: pd.DataFrame) -> None:
    rows = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    all_text = "\n".join(candidates.get("資料集名稱", pd.Series(dtype=str)).astype(str).tolist())
    for city in TAIWAN_CITIES:
        city_hits = candidates[candidates.apply(lambda r: city in " ".join(map(str, r.values)), axis=1)]
        rows.append(
            {
                "search_time": now,
                "city": city,
                "searched_keywords": "；".join([f"{city} {kw}" for kw in KEYWORDS]),
                "data_gov_candidate_count": len(city_hits),
                "candidate_titles": " | ".join(city_hits["資料集名稱"].astype(str).head(20).tolist()),
                "note": "data.gov.tw 全量目錄掃描；另以網頁搜尋查核主要地方平台。" if city in all_text else "data.gov.tw 全量目錄未找到可直接辨識之縣市點位/時相資料。",
            }
        )
    pd.DataFrame(rows).to_csv(META_DIR / "search_audit.csv", index=False, encoding="utf-8-sig")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ensure_dirs()
    catalog_path = download_catalog()
    catalog = read_catalog(catalog_path)
    candidates = catalog[catalog.apply(is_candidate, axis=1)].copy()
    candidates["候選類型"] = candidates.apply(classify_candidate, axis=1)
    candidates["資料集網址"] = candidates["資料集識別碼"].map(lambda x: f"https://data.gov.tw/dataset/{x}")
    preferred_cols = [
        "資料集識別碼",
        "資料集名稱",
        "候選類型",
        "提供機關",
        "檔案格式",
        "資料下載網址",
        "資料集描述",
        "主要欄位說明",
        "授權方式",
        "詮釋資料更新時間",
        "資料集網址",
    ]
    candidates[preferred_cols].to_csv(META_DIR / "source_candidates.csv", index=False, encoding="utf-8-sig")
    write_search_audit(candidates)
    with (LOG_DIR / "discovery_log.txt").open("a", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([dt.datetime.now().isoformat(timespec="seconds"), "download_catalog", str(catalog_path), len(catalog)])
        writer.writerow([dt.datetime.now().isoformat(timespec="seconds"), "candidate_count", len(candidates)])
    print(f"catalog_rows={len(catalog)}")
    print(f"candidate_rows={len(candidates)}")
    print(f"wrote={META_DIR / 'source_candidates.csv'}")
    print(f"wrote={META_DIR / 'search_audit.csv'}")


if __name__ == "__main__":
    main()

