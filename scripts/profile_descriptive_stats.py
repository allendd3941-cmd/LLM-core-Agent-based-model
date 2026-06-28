"""profile_descriptive_stats.py — agent persona pool 敘述性統計 + 論文用 1×4 橫排圖。

讀 output/agent_profile_output_1.txt（20k personas）→ 統計 age / vehicle / occupation(歸6類) /
income(歸5帶) → 畫一張扁長 1×4 figure（PDF+PNG，英文標籤、論文風）→ stdout 印出可直接貼正文的
「多樣性數字」（N、各維度 distinct 數與最大佔比、汽機車比、特質維度）。

純讀取輸入、只寫圖到 output/。用法：
    uv run python scripts/profile_descriptive_stats.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "output" / "agent_profile_output_1.txt"
OUT_PDF = ROOT / "output" / "agent_profile_stats.pdf"
OUT_PNG = ROOT / "output" / "agent_profile_stats.png"

import re

# vehicle_ownership 仍是中文 enum（後端刻意保留），故維持中→英顯示。
VEHICLE_EN = {"汽車": "Car", "機車": "Motorcycle"}

# ⚠ persona 改英文後,職業/收入/年齡是自由英文片語(LLM 產);改用「關鍵字/數字擷取」分類,
#   並對未命中軟回退(不再 sys.exit 崩潰)。重跑後請對照實際 persona 微調關鍵字。
SECTOR_ORDER = ["Student", "Service", "Professional", "Gig/Freelance", "Other/Resident"]
BAND_ORDER = ["Low (≤3)", "Mid (4–5)", "High (6–8)", "Unstable", "Retired"]
_WORD_NUM = {"eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-five": 25, "thirty": 30,
             "thirty-five": 35, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60,
             "teens": 18, "twenties": 25, "thirties": 35, "forties": 45, "fifties": 55, "sixties": 60}


def _first_int(s) -> int | None:
    m = re.search(r"\d+", str(s))
    return int(m.group()) if m else None


def age_num(s) -> int:
    n = _first_int(s)
    if n is not None:
        return n
    ls = str(s).lower()
    for w, v in _WORD_NUM.items():
        if w in ls:
            return v
    return 30


def occ_sector(s) -> str:
    ls = str(s).lower()
    if any(k in ls for k in ("student", "pupil", "schoolchild", "undergrad", "graduate")):
        return "Student"
    if any(k in ls for k in ("freelanc", "driver", "delivery", "courier", "gig", "rideshare")):
        return "Gig/Freelance"
    if any(k in ls for k in ("engineer", "manager", "teacher", "nurse", "officer", "office worker",
                             "civil servant", "professional", "sales", "executive", "doctor", "accountant", "clerk")):
        return "Professional"
    if any(k in ls for k in ("cashier", "server", "waiter", "waitress", "security", "guard", "cleaner",
                             "vendor", "retail", "barista", "attendant", "ticket", "staff", "shop", "service")):
        return "Service"
    return "Other/Resident"


def wage_band(s) -> str:
    ls = str(s).lower()
    if "retir" in ls or "pension" in ls:
        return "Retired"
    if any(k in ls for k in ("unstable", "irregular", "no fixed", "variable", "bonus")):
        return "Unstable"
    n = _first_int(ls)
    if n is not None:
        wan = n / 10000.0 if n >= 1000 else (n / 10.0 if n >= 10 else float(n))  # 40000→4 / 40k→4 / 4→4
        if wan <= 3:
            return "Low (≤3)"
        if wan <= 5:
            return "Mid (4–5)"
        return "High (6–8)"
    return "Mid (4–5)"

TRAIT_DIMS = ["attitudes", "habits", "decision_making_tendencies", "economic_preferences_and_tradeoffs"]


def load_agents() -> list[dict]:
    return json.loads(SRC.read_text(encoding="utf-8"))["agents"]


def main() -> None:
    ags = load_agents()
    n = len(ags)

    def ident(f):
        return [a["identity"].get(f, "") for a in ags]

    # 關鍵字/數字分類（軟回退；未命中歸 Other/Resident 或 fallback band，不崩潰）
    age_c = Counter(ident("age"))
    veh_c = Counter(VEHICLE_EN.get(v, v) for v in ident("vehicle_ownership"))
    sec_c = Counter(occ_sector(o) for o in ident("occupation"))
    band_c = Counter(wage_band(w) for w in ident("wage"))

    age_labels = sorted(age_c, key=age_num)
    age_vals = [age_c[k] for k in age_labels]
    age_ticks = [str(age_num(k)) for k in age_labels]
    veh_labels = ["Car", "Motorcycle"]
    veh_vals = [veh_c.get(k, 0) for k in veh_labels]
    sec_vals = [sec_c.get(k, 0) for k in SECTOR_ORDER]
    band_vals = [band_c.get(k, 0) for k in BAND_ORDER]

    # ---------- 多樣性數字（給正文）----------
    def maxshare(c):
        return 100 * max(c.values()) / n
    trait_distinct = {t: len({v for a in ags for v in a["traits"].get(t, [])}) for t in TRAIT_DIMS}
    print("=== Agent persona pool — descriptive stats ===")
    print(f"N personas                = {n}")
    print(f"distinct age bands        = {len(age_c)}  (max share {maxshare(age_c):.1f}%, uniform {100/len(age_c):.1f}%)")
    print(f"distinct occupations      = {len(Counter(ident('occupation')))}  (max share {maxshare(Counter(ident('occupation'))):.1f}%)")
    print(f"distinct income levels    = {len(Counter(ident('wage')))}  (max share {maxshare(Counter(ident('wage'))):.1f}%)")
    print(f"distinct districts        = {len(Counter(ident('residential_location')))}")
    print(f"trait dims x distinct     = {trait_distinct}  -> total {sum(trait_distinct.values())} distinct trait values")
    print(f"vehicle Car:Moto          = {veh_c.get('Car',0)}:{veh_c.get('Motorcycle',0)} "
          f"({100*veh_c.get('Car',0)/n:.1f}% / {100*veh_c.get('Motorcycle',0)/n:.1f}%)")

    # ---------- 1x4 figure ----------
    plt.rcParams.update({"font.size": 7, "axes.titlesize": 8, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.dpi": 150})
    fig, axes = plt.subplots(1, 4, figsize=(11, 2.3))
    pct = lambda vals: [100 * v / n for v in vals]

    def bar(ax, ticks, vals, title, rot=0):
        ax.bar(range(len(vals)), pct(vals), color="#4C6EDB", width=0.72)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(ticks, rotation=rot, ha="right" if rot else "center")
        ax.set_title(title)
        ax.set_ylabel("share (%)")
        ax.margins(x=0.04)

    bar(axes[0], age_ticks, age_vals, "(a) Age (≈ yrs)")
    bar(axes[1], veh_labels, veh_vals, "(b) Vehicle")
    bar(axes[2], SECTOR_ORDER, sec_vals, "(c) Occupation sector", rot=35)
    bar(axes[3], BAND_ORDER, band_vals, "(d) Income (×10k TWD/mo)", rot=35)
    fig.tight_layout()
    fig.savefig(OUT_PDF, bbox_inches="tight")
    fig.savefig(OUT_PNG, bbox_inches="tight")
    print(f"\nwrote {OUT_PDF.name} + {OUT_PNG.name} -> {OUT_PDF.parent}")


if __name__ == "__main__":
    main()
