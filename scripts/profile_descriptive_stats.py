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

# --- 年齡排序（中文歲數 → 數值）---
AGE_ORDER = {"十八歲左右": 18, "二十歲左右": 20, "二十五歲左右": 25, "三十歲左右": 30,
             "三十五歲左右": 35, "四十歲左右": 40, "五十歲左右": 50, "六十歲左右": 60}

VEHICLE_EN = {"汽車": "Car", "機車": "Motorcycle"}

# --- 30 職業 → 6 sector ---
OCC_SECTOR = {
    "國中生": "Student", "高中生": "Student", "研究生": "Student", "大學生": "Student",
    "票務員": "Service", "球場工讀": "Service", "保全": "Service", "餐飲員": "Service",
    "便利店員": "Service", "店長": "Service", "停車引導員": "Service", "清潔員": "Service", "攤商": "Service",
    "護理師": "Professional", "公司主管": "Professional", "教師": "Professional", "業務員": "Professional",
    "工程師": "Professional", "上班族": "Professional", "公務員": "Professional",
    "自由業": "Gig/Freelance", "外送員": "Gig/Freelance", "司機": "Gig/Freelance",
    "親友接送者": "Other/Resident", "退休族": "Other/Resident", "家長": "Other/Resident",
    "通勤族": "Other/Resident", "附近居民": "Other/Resident",
    "球迷會長": "Other/Resident", "賽事志工": "Other/Resident",   # 球迷·志工 併入 Other/Resident
}
SECTOR_ORDER = ["Student", "Service", "Professional", "Gig/Freelance", "Other/Resident"]

# --- 13 收入 → 5 帶（萬 = ×10k TWD/月）---
WAGE_BAND = {
    "月薪三萬": "Low (≤3)", "打工三萬": "Low (≤3)", "打工兩萬": "Low (≤3)",
    "家用五萬": "Mid (4–5)", "月薪四萬": "Mid (4–5)", "月薪五萬": "Mid (4–5)",
    "月薪七萬": "High (6–8)", "月薪八萬": "High (6–8)", "月薪六萬": "High (6–8)",
    "收入不穩": "Unstable", "獎金較多": "Unstable", "無固定收入": "Unstable",
    "退休金三萬": "Retired",
}
BAND_ORDER = ["Low (≤3)", "Mid (4–5)", "High (6–8)", "Unstable", "Retired"]

TRAIT_DIMS = ["attitudes", "habits", "decision_making_tendencies", "economic_preferences_and_tradeoffs"]


def load_agents() -> list[dict]:
    return json.loads(SRC.read_text(encoding="utf-8"))["agents"]


def main() -> None:
    ags = load_agents()
    n = len(ags)

    def ident(f):
        return [a["identity"].get(f, "") for a in ags]

    # 套用對照表（並驗證無漏項）
    unmapped_occ = {o for o in ident("occupation") if o not in OCC_SECTOR}
    unmapped_wage = {w for w in ident("wage") if w not in WAGE_BAND}
    if unmapped_occ or unmapped_wage:
        sys.exit(f"⚠ 未對照: occ={unmapped_occ} wage={unmapped_wage}（請補進 mapping）")

    age_c = Counter(ident("age"))
    veh_c = Counter(VEHICLE_EN.get(v, v) for v in ident("vehicle_ownership"))
    sec_c = Counter(OCC_SECTOR[o] for o in ident("occupation"))
    band_c = Counter(WAGE_BAND[w] for w in ident("wage"))

    age_labels = sorted(age_c, key=lambda k: AGE_ORDER.get(k, 999))
    age_vals = [age_c[k] for k in age_labels]
    age_ticks = [str(AGE_ORDER[k]) for k in age_labels]
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
