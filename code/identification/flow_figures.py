"""Flowchart numbers for an update: previous period, this update, and the two combined, ready to copy into figures.

Flowcharts are drawn twice per update, about a month apart: after the loose stage and after the strict stage
(strict + Cohort 2, run back to back on the governed batch). So there are two files, and the loose file is never
overwritten by the strict run:
    cohort_loose.py  -> write_flow("loose", flow_values(results))   loose/flowchart_numbers_loose.txt
                        rows up to HT subjects; other indication = 0*, rows after it starred
    cohort.py        -> write_flow("strict", flow_values(results))  strict/flowchart_numbers_strict.txt
                        rows up to Cohort 1, no stars; Cohort 2 rows pending
    cohort2.py       -> write_flow("cohort2")                       strict/flowchart_numbers_strict.txt, completed
                        with the Cohort 2 rows (final)
Standalone (re-render from the saved numbers): python flow_figures.py <loose|strict>

Each .txt has a .csv beside it (flow_numbers_loose.csv, flow_numbers_strict.csv) that keeps the numbers.
Figure 1 = previous and this update side by side; figure 2 = the combined column.

Where the numbers come from
  previous   data/identification/<PREV_PERIOD>/strict/flow_numbers_strict.csv, combined column of its final stage;
             when that file does not exist, clean_slate/flow_counts.parquet of PREV_PERIOD (the 2010-202606 build)
  this update  counts of this update's stage run (Cohort 2 rows: subjects new to Cohort 1 in this update)
  combined   previous + this update for rows 1-7 and Cohort 1 (known HT cases are removed before counting, so nobody
             is counted twice; rows 1-2 add this update's candidates left after that removal). Cohort 2 rows are
             counted directly from the cumulative Cohort 2 file, because Cohort 2 is recomputed over all years.
CHECK lines compare summed numbers with direct counts from the cumulative files.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import polars as pl

from update_config import PERIOD, PREV_PERIOD

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IDENT_ROOT = PROJECT_ROOT / "data/identification"
C1_INDEX = "HT Date (Study Period)"

# (key, label, first stage that has it); "-" rows are headings
ROWS = [
    ("dx_candidates", "1. Diagnosis file, HT ICD candidates", "loose"),
    ("med_candidates", "2. Medication file, antihypertensive candidates", "loose"),
    ("known_removed", "   Known HT cases removed (previous cohort)", "loose"),
    ("-", "3. Cross-check of diagnosis vs medication:", "loose"),
    ("icd_only", "   ICD only", "loose"),
    ("both", "   Both ICD and medication", "loose"),
    ("medication_only", "   Medication only", "loose"),
    ("diagnosis_of_ht", "4. Diagnosis of HT (ICD only + both)", "loose"),
    ("other_indication", "5. Medication only, other indication (excluded)", "loose"),
    ("inferred", "6. Inferred diagnosis (medication only left)", "loose"),
    ("ht_subjects", "7. HT subjects (ICD only + both + inferred)", "loose"),
    ("missing_dob", "   Missing date of birth (excluded)", "strict"),
    ("below_18", "   Age below 18 at index (excluded)", "strict"),
    ("cohort1", "   Cohort 1 (adults)", "strict"),
    ("lookback_dx", "   Lookback 2005-2009 HT diagnosis", "cohort2"),
    ("lookback_med", "   Lookback 2005-2009 antihypertensive bill", "cohort2"),
    ("prevalent", "   Any lookback evidence (prevalent, excluded)", "cohort2"),
    ("cohort2", "   Cohort 2 (incident)", "cohort2"),
]
ORDER = ["loose", "strict", "cohort2"]
PROVISIONAL_AT_LOOSE = {"other_indication", "inferred", "ht_subjects"}   # starred: other indications not checked
NEW_ONLY = {"known_removed"}                                               # no previous / combined value
BEFORE_REMOVAL = {"dx_candidates", "med_candidates"}                       # this update counted before removal
COHORT2_KEYS = {"lookback_dx", "lookback_med", "prevalent", "cohort2"}


def previous_numbers(prev_dir: Path) -> tuple[dict[str, int], str]:
    csv = prev_dir / "strict/flow_numbers_strict.csv"
    if csv.exists():
        t = pl.read_csv(csv)
        stage = t["stage"][0]
        if stage != "cohort2":
            print(f"WARNING: {csv} is not final (stage {stage}); Cohort 2 rows of the previous column are missing")
        vals = {k: v for k, v in t.select("key", "combined").iter_rows() if v is not None}
        return vals, f"{csv.relative_to(PROJECT_ROOT)} (stage {stage})"
    seed = prev_dir / "clean_slate/flow_counts.parquet"
    if not seed.exists():
        raise SystemExit(f"no previous flow numbers: neither {csv} nor {seed}")
    c = dict(pl.read_parquet(seed).iter_rows())
    vals = {k: c[k] for k in ("dx_candidates", "med_candidates", "icd_only", "both", "medication_only",
                              "other_indication", "inferred", "ht_subjects", "missing_dob", "below_18", "cohort1",
                              "lookback_dx", "lookback_med", "prevalent", "cohort2")}
    vals["diagnosis_of_ht"] = vals["icd_only"] + vals["both"]
    return vals, f"{seed.relative_to(PROJECT_ROOT)} (clean-slate build, final)"


def cohort2_numbers(ident_dir: Path, prev_dir: Path) -> tuple[dict[str, int], dict[str, int]]:
    """(this update: subjects new to Cohort 1, combined: everyone) from the cumulative Cohort 2 evaluation."""
    ev = pl.read_parquet(ident_dir / "index/cohort2_evaluated.parquet.gzip",
                         columns=["ENC_HN", "has_lookback_dx", "has_lookback_med", "prevalent_before_2010"])
    old_ids = pl.read_parquet(prev_dir / "index/cohort1.parquet.gzip", columns=["ENC_HN"])

    def count(f: pl.DataFrame) -> dict[str, int]:
        return {"cohort1_in_cohort2_input": f.height,
                "lookback_dx": int(f["has_lookback_dx"].sum()), "lookback_med": int(f["has_lookback_med"].sum()),
                "prevalent": int(f["prevalent_before_2010"].sum()),
                "cohort2": int((~f["prevalent_before_2010"]).sum())}
    return count(ev.join(old_ids, on="ENC_HN", how="anti")), count(ev)


def write_flow(stage: str, new: dict[str, int] | None = None, period: str = PERIOD,
               prev_period: str = PREV_PERIOD) -> Path:
    d, prev_dir = IDENT_ROOT / period, IDENT_ROOT / prev_period
    sub = "loose" if stage == "loose" else "strict"
    csv, txt = d / sub / f"flow_numbers_{sub}.csv", d / sub / f"flowchart_numbers_{sub}.txt"
    old, old_src = previous_numbers(prev_dir)
    checks: list[str] = []
    if stage == "cohort2":           # rows 1-7 and Cohort 1 come from the strict run saved earlier
        if not csv.exists():
            raise SystemExit(f"{csv} missing: run cohort.py first")
        saved = pl.read_csv(csv)
        new = {k: v for k, v in saved.select("key", "new").iter_rows() if v is not None and k not in COHORT2_KEYS}
        c2_new, c2_all = cohort2_numbers(d, prev_dir)
        new |= {k: c2_new[k] for k in COHORT2_KEYS}
    elif new is None:
        raise SystemExit(f"stage {stage} needs this update's numbers")
    new = dict(new)
    new["diagnosis_of_ht"] = new["icd_only"] + new["both"]

    upto = ORDER.index(stage)
    rows = []
    for key, label, first in ROWS:
        if ORDER.index(first) > upto:
            continue
        if key == "-":
            rows.append((key, label, None, None, None, False))
            continue
        o = None if key in NEW_ONLY else old.get(key)
        n = new.get(key)
        if key in NEW_ONLY:
            comb = None
        elif key == "dx_candidates":
            comb = o + new["icd_only"] + new["both"]
        elif key == "med_candidates":
            comb = o + new["both"] + new["medication_only"]
        elif key in COHORT2_KEYS:
            comb = c2_all[key]
        else:
            comb = None if o is None or n is None else o + n
        rows.append((key, label, o, n, comb, stage == "loose" and key in PROVISIONAL_AT_LOOSE))

    vals = {r[0]: r for r in rows}
    if stage in ("strict", "cohort2"):
        c1_file = d / "index/cohort1.parquet.gzip"
        if c1_file.exists():
            direct = pl.read_parquet(c1_file, columns=["ENC_HN"]).height
            comb = vals["cohort1"][4]
            checks.append(f"{'ok' if comb == direct else 'CHECK'}: combined Cohort 1 {comb:,} vs rows in "
                          f"{c1_file.relative_to(PROJECT_ROOT)} {direct:,}")
    if stage == "cohort2":
        checks.append(f"{'ok' if c2_all['cohort1_in_cohort2_input'] == vals['cohort1'][4] else 'CHECK'}: "
                      f"Cohort 2 input {c2_all['cohort1_in_cohort2_input']:,} vs combined Cohort 1 "
                      f"{vals['cohort1'][4]:,}")
        checks.append(f"{'ok' if c2_new['cohort1_in_cohort2_input'] == vals['cohort1'][3] else 'CHECK'}: "
                      f"subjects new to Cohort 1 {c2_new['cohort1_in_cohort2_input']:,} vs this update's Cohort 1 "
                      f"{vals['cohort1'][3]:,}")
        for k in ("prevalent", "cohort2"):
            o, n, comb = vals[k][2], vals[k][3], vals[k][4]
            checks.append(f"{'ok' if o + n == comb else 'CHECK'}: {k}: previous {o:,} + this update {n:,} = "
                          f"{o + n:,} vs combined (recomputed) {comb:,}"
                          + ("" if o + n == comb else "  <- existing subjects changed Cohort 2 status; see "
                             "index/cohort2_summary.txt stability"))

    stage_title = {"loose": "LOOSE (provisional: other indications not yet checked)",
                   "strict": "STRICT (Cohort 2 rows pending: run cohort2.py)",
                   "cohort2": "STRICT + COHORT 2 (final)"}[stage]
    start, end = prev_period.split("-")[0], period.split("-")[-1]
    heads = (f"{prev_period}", f"{period}", f"{start}-{end}")
    fmt = lambda v, star: "" if v is None else f"{v:,}{'*' if star else ''}"
    W = 52
    L = [f"FLOWCHART NUMBERS  -  stage: {stage_title}",
         f"written {datetime.now():%Y-%m-%d %H:%M}; previous numbers from {old_src}", "",
         "Figure 1: previous and this update side by side.  Figure 2: combined column.", "",
         f"{'':<{W}}{heads[0]:>14}{heads[1]:>16}{heads[2]:>16}",
         f"{'':<{W}}{'(fig 1, old)':>14}{'(fig 1, new)':>16}{'(fig 2)':>16}"]
    for key, label, o, n, comb, star in rows:
        if key == "-":
            L.append(label)
            continue
        n_txt = fmt(n, star) + (" a" if key in BEFORE_REMOVAL else "")
        L.append(f"{label:<{W}}{fmt(o, False):>14}{n_txt:>16}{fmt(comb, star):>16}")
    if stage == "strict":
        L += [f"{label:<{W}}{fmt(old.get(key), False):>14}{'pending':>16}{'pending':>16}"
              for key, label, first in ROWS if first == "cohort2"]
    L += ["", "a  this update counted before known HT cases are removed; combined adds only the candidates left after",
          "   removal (row 3), so rows 1-2 of the combined column are not old + new."]
    if stage == "loose":
        L += ["*  provisional: the loose stage does not check other indications (row 5 kept as 0); rows 6-7 follow",
              "   from it. The strict stage fills them and removes the stars. Loose stops at HT subjects."]
    if stage == "cohort2":
        L += ["Cohort 2 rows: this update = subjects new to Cohort 1 in this update; combined = recounted over all",
              "   years from index/cohort2_evaluated (Cohort 2 is recomputed each update)."]
    L += ["", "CHECKS"] + [f"  {c}" for c in checks or ["(none at this stage)"]]

    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("\n".join(L) + "\n", encoding="utf-8")
    pl.DataFrame([{"key": k, "label": lab.strip(), "old": o, "new": n, "combined": c, "provisional": s,
                   "stage": stage, "period": period, "prev_period": prev_period}
                  for k, lab, o, n, c, s in rows if k != "-"],
                 schema={"key": pl.String, "label": pl.String, "old": pl.Int64, "new": pl.Int64,
                         "combined": pl.Int64, "provisional": pl.Boolean, "stage": pl.String,
                         "period": pl.String, "prev_period": pl.String}).write_csv(csv)
    print(f"flowchart numbers: {txt}")
    if any(c.startswith("CHECK") for c in checks):
        print("WARNING: flowchart CHECK items: review {txt.name}")
    return txt


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "strict"
    st = pl.read_csv(IDENT_ROOT / PERIOD / which / f"flow_numbers_{which}.csv")
    stg = st["stage"][0]
    write_flow(stg, None if stg == "cohort2" else
               {k: v for k, v in st.select("key", "new").iter_rows() if v is not None})
