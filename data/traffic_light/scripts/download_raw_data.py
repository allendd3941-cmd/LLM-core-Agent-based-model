from __future__ import annotations

import csv
import datetime as dt
import re
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError

from source_config import SOURCE_SPECS, SourceSpec


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "raw"
LOG_DIR = ROOT / "logs"
META_DIR = ROOT / "metadata"


def ensure_dirs() -> None:
    for path in [RAW_DIR, LOG_DIR, META_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def safe_name(text: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("_")
    return text[:120] or "download"


def log_download(row: dict[str, object]) -> None:
    log_path = LOG_DIR / "download_log.txt"
    exists = log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "download_time",
                "source_id",
                "source_name",
                "url",
                "http_status",
                "output_path",
                "file_size",
                "error",
            ],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def request_source(spec: SourceSpec) -> requests.Response:
    headers = {"User-Agent": "traffic-light-data-prep/1.0"}
    try:
        if spec.request_method.upper() == "POST":
            return requests.post(spec.url, data={"data": spec.request_body}, timeout=240, headers=headers)
        return requests.get(spec.url, timeout=180, headers=headers, allow_redirects=True)
    except SSLError:
        return request_source_legacy_tls(spec, headers)


class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args: object, **kwargs: object) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers("DEFAULT:@SECLEVEL=1")
        except ssl.SSLError:
            pass
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


def request_source_legacy_tls(spec: SourceSpec, headers: dict[str, str]) -> requests.Response:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.mount("https://", LegacyTLSAdapter())
    if spec.request_method.upper() == "POST":
        return session.post(spec.url, data={"data": spec.request_body}, timeout=240, headers=headers, verify=False)
    return session.get(spec.url, timeout=180, headers=headers, allow_redirects=True, verify=False)


def download_one(spec: SourceSpec) -> Path | None:
    source_dir = RAW_DIR / spec.source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    output = source_dir / spec.file_name
    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        response = request_source(spec)
        output.write_bytes(response.content)
        error = "" if response.ok else response.text[:500]
        log_download(
            {
                "download_time": now,
                "source_id": spec.source_id,
                "source_name": spec.source_name,
                "url": spec.url,
                "http_status": response.status_code,
                "output_path": str(output),
                "file_size": output.stat().st_size if output.exists() else 0,
                "error": error,
            }
        )
        return output if response.ok else None
    except Exception as exc:
        log_download(
            {
                "download_time": now,
                "source_id": spec.source_id,
                "source_name": spec.source_name,
                "url": spec.url,
                "http_status": "",
                "output_path": str(output),
                "file_size": 0,
                "error": repr(exc),
            }
        )
        return None


def read_csv_loose(path: Path) -> pd.DataFrame:
    for encoding in ["utf-8-sig", "utf-8", "cp950", "big5", "latin1"]:
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding).fillna("")
        except Exception:
            continue
    return pd.DataFrame()


def download_linked_resources(source_id: str, index_path: Path) -> None:
    df = read_csv_loose(index_path)
    if df.empty:
        return
    link_cols = [col for col in df.columns if "連結" in col or "網址" in col or col.lower() in {"url", "link"}]
    if not link_cols:
        return
    linked_dir = RAW_DIR / source_id / "linked"
    linked_dir.mkdir(parents=True, exist_ok=True)
    for col in link_cols:
        for idx, url in enumerate(df[col].astype(str).tolist(), start=1):
            if not url.startswith(("http://", "https://")):
                continue
            parsed = urlparse(url)
            ext = Path(parsed.path).suffix or ".bin"
            output = linked_dir / f"{safe_name(col)}_{idx}{ext}"
            now = dt.datetime.now().isoformat(timespec="seconds")
            try:
                response = requests.get(url, timeout=120, headers={"User-Agent": "traffic-light-data-prep/1.0"})
                output.write_bytes(response.content)
                log_download(
                    {
                        "download_time": now,
                        "source_id": f"{source_id}_LINKED",
                        "source_name": f"linked resource from {source_id}",
                        "url": url,
                        "http_status": response.status_code,
                        "output_path": str(output),
                        "file_size": output.stat().st_size if output.exists() else 0,
                        "error": "" if response.ok else response.text[:500],
                    }
                )
            except Exception as exc:
                log_download(
                    {
                        "download_time": now,
                        "source_id": f"{source_id}_LINKED",
                        "source_name": f"linked resource from {source_id}",
                        "url": url,
                        "http_status": "",
                        "output_path": str(output),
                        "file_size": 0,
                        "error": repr(exc),
                    }
                )


def write_static_sources_catalog_seed() -> None:
    rows = []
    today = dt.date.today().isoformat()
    for spec in SOURCE_SPECS:
        rows.append(
            {
                "來源ID": spec.source_id,
                "來源名稱": spec.source_name,
                "官方機關": spec.agency,
                "網址": spec.url,
                "來源頁面": spec.landing_url,
                "格式": spec.fmt,
                "涵蓋縣市": spec.covered_cities,
                "資料更新日期": "",
                "下載日期": today,
                "授權條款": spec.license,
                "是否官方": "是" if spec.official else "否",
                "欄位摘要": "",
                "下載狀態": "",
                "備註": spec.note,
            }
        )
    pd.DataFrame(rows).to_csv(META_DIR / "sources_catalog_seed.csv", index=False, encoding="utf-8-sig")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ensure_dirs()
    write_static_sources_catalog_seed()
    downloaded: dict[str, Path] = {}
    for spec in SOURCE_SPECS:
        path = download_one(spec)
        if path:
            downloaded[spec.source_id] = path
            if spec.parser in {"taichung_gis_index", "chiayi_county_phase_index"}:
                download_linked_resources(spec.source_id, path)
            print(f"downloaded {spec.source_id}: {path}")
        else:
            print(f"failed {spec.source_id}")
    print(f"download_count={len(downloaded)}")
    print(f"log={LOG_DIR / 'download_log.txt'}")


if __name__ == "__main__":
    main()
