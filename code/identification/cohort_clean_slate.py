"""Build the clean-slate hypertension cohort, 2010-01-01 to 2026-06-30.

Built from the governed ``20260630_fu_nc`` delivery only. No historical cohort is
read and no earlier output is reused. The result is the baseline period
``data/identification/2010-202606/`` that later incremental updates
(cohort_loose.py -> cohort.py -> cohort2.py) read as PREV_PERIOD.

Criteria:
1. Evidence in the window: a qualifying HT diagnosis (I10-I13, I15) or a mapped
   antihypertensive medication.
2. Medication-only subjects are excluded when diagnosis history before their
   first medication supports a mapped other indication.
3. Index date: first day any criterion is met. Flag: which criteria were met on
   that day (icd, med, or both).
4. Cohort 1: age >= 18 at index. Date of birth from the governed demographics;
   subjects without one are excluded.
5. Cohort 2: Cohort 1 without HT evidence in the 2005-2009 lookback.

Definitions come from cohort.py and cohort2.py by import, so they match the
incremental stages exactly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from cohort import (
    _list_repr_expr,
    build_diagnosis_summary,
    build_medication_summary,
    index_day_flag,
    load_other_indications,
    match_alternative_indications,
    read_demographics,
    read_diagnosis_working_df,
    read_medication_working_df,
    read_relevant_diagnosis_history,
)
from cohort2 import (
    apply_lookback_criteria,
    read_lookback_diagnosis,
    read_lookback_medication,
    summarize_lookback_diagnosis,
    summarize_lookback_medication,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PERIOD = "2010-202606"
OUT_DIR = PROJECT_ROOT / "data/identification" / PERIOD
SOURCE_ROOT = Path("/path/to/datalake/Data/20260630_fu_nc")  # EDIT: your site's governed batch
LOOKBACK_ROOT = SOURCE_ROOT / "2005-2009"
# Pinned: the baseline was built with this map; update_config.py may move on.
MEDICATION_MAP = PROJECT_ROOT / "maplist/cohort"
START_DATE = date(2010, 1, 1)
END_DATE = date(2026, 6, 30)
FULL_STEM = f"hthn_2010_{PERIOD.split('-')[-1]}"


def build_evidence(dx_summary: pl.DataFrame, med_summary: pl.DataFrame) -> pl.DataFrame:
    """One row per subject with any evidence, and its evidence group over the window."""

    return dx_summary.join(med_summary, on="ENC_HN", how="full", coalesce=True).with_columns(
        pl.when(pl.col("diagnosis_date").is_not_null() & pl.col("medication_date").is_not_null())
        .then(pl.lit("both"))
        .when(pl.col("diagnosis_date").is_not_null())
        .then(pl.lit("icd_only"))
        .otherwise(pl.lit("medication_only"))
        .alias("evidence_group")
    )


def assign_index_and_flag(
    ht: pl.DataFrame,
    dx_working: pl.DataFrame,
    med_working: pl.DataFrame,
) -> pl.DataFrame:
    """Index date = first evidence day. Flag = criteria met on that day (shared rule)."""

    ht = ht.with_columns(
        pl.min_horizontal("diagnosis_date", "medication_date").alias("index_date")
    )
    return ht.join(index_day_flag(ht, dx_working, med_working), on="ENC_HN", how="left")


def build_full_rows(ht: pl.DataFrame) -> pl.DataFrame:
    """The full HT file, in the same schema the incremental stages read."""

    return ht.select(
        pl.col("ENC_HN"),
        pl.col("diagnosis_date").dt.strftime("%Y-%m-%d").alias("ICD_DATE"),
        _list_repr_expr("dx_icd_codes").alias("ICD"),
        pl.col("medication_date").dt.strftime("%Y-%m-%d").alias("MED_DATE"),
        _list_repr_expr("medication_codes").alias("MED"),
        pl.col("index_date").dt.strftime("%Y-%m-%d").alias("D001KEY"),
        pl.col("flag"),
    ).sort("ENC_HN")


def apply_adult_criteria(ht: pl.DataFrame, demographics: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    evaluated = (
        ht.select("ENC_HN", "index_date")
        .join(demographics.select("ENC_HN", pl.col("date_of_birth").alias("dob")), on="ENC_HN", how="left")
        .with_columns(
            ((pl.col("index_date") - pl.col("dob")).dt.total_days() / 365.25).alias("age_at_index")
        )
    )
    missing_dob = evaluated.filter(pl.col("dob").is_null())
    below_18 = evaluated.filter(pl.col("dob").is_not_null() & (pl.col("age_at_index") < 18))
    adults = evaluated.filter(pl.col("dob").is_not_null() & (pl.col("age_at_index") >= 18))
    return adults, {
        "missing_dob": missing_dob.height,
        "below_18": below_18.height,
        "cohort1": adults.height,
    }


def build_report(c: dict[str, int]) -> list[str]:
    def row(label: str, value: int | str) -> str:
        text = f"{value:,}" if isinstance(value, int) else str(value)
        return f"{label:<52}{text:>10}"

    return [
        "HYPERTENSION CLEAN-SLATE COHORT OUTPUT",
        "======================================",
        f"Source: {SOURCE_ROOT}",
        f"Window: {START_DATE} through {END_DATE}, inclusive",
        "No historical cohort read; no earlier output reused.",
        f"Rows dropped for missing ENC_HN: diagnosis {c['dx_null_id_rows']:,}, medication {c['med_null_id_rows']:,}",
        "",
        "Candidate flow (unique ENC_HN)",
        "------------------------------",
        row("1. Diagnosis file, HT ICD candidates", c["dx_candidates"]),
        row("2. Medication file, antihypertensive candidates", c["med_candidates"]),
        row("   Known HT cases removed", "n/a (clean slate)"),
        "3. Cross-check of diagnosis vs medication:",
        row("   ICD only", c["icd_only"]),
        row("   Both ICD and medication", c["both"]),
        row("   Medication only", c["medication_only"]),
        row("4. Diagnosis of HT (ICD only + both)", c["icd_only"] + c["both"]),
        "5. Medication only, other-indication check:",
        row("   Other indication found (excluded)", c["other_indication"]),
        row("6. Inferred diagnosis (medication only left over)", c["inferred"]),
        row("7. HT subjects (ICD only + both + inferred)", c["ht_subjects"]),
        "",
        "Index-day flag (criteria met on the index date)",
        "-----------------------------------------------",
        row("   icd", c["flag_icd"]),
        row("   med", c["flag_med"]),
        row("   both", c["flag_both"]),
        row("   no evidence on index day (should be 0)", c["flag_missing"]),
        "",
        "Adult eligibility (Cohort 1)",
        "----------------------------",
        row("   HT subjects", c["ht_subjects"]),
        row("   Missing date of birth (excluded)", c["missing_dob"]),
        row("   Age below 18 at index (excluded)", c["below_18"]),
        row("   Cohort 1 (adults)", c["cohort1"]),
        "",
        "Incident cases (Cohort 2), 2005-2009 lookback",
        "---------------------------------------------",
        row("   Cohort 1", c["cohort1"]),
        row("   Lookback HT diagnosis", c["lookback_dx"]),
        row("   Lookback antihypertensive bill", c["lookback_med"]),
        row("   Any lookback evidence (prevalent, excluded)", c["prevalent"]),
        row("   Cohort 2 (incident)", c["cohort2"]),
    ]


def main() -> None:
    dx_working = read_diagnosis_working_df(SOURCE_ROOT / "diagnosis", start_date=START_DATE, end_date=END_DATE)
    med_working = read_medication_working_df(
        SOURCE_ROOT / "order", SOURCE_ROOT / "medication", START_DATE, END_DATE, MEDICATION_MAP
    )
    # Rows without a patient ID cannot belong to a subject; grouping them would
    # create one fake subject.
    null_id_rows = {
        "dx_null_id_rows": dx_working.filter(pl.col("ENC_HN").is_null()).height,
        "med_null_id_rows": med_working.filter(pl.col("ENC_HN").is_null()).height,
    }
    dx_working = dx_working.filter(pl.col("ENC_HN").is_not_null())
    med_working = med_working.filter(pl.col("ENC_HN").is_not_null())
    evidence = build_evidence(build_diagnosis_summary(dx_working), build_medication_summary(med_working))

    indication_map = load_other_indications(MEDICATION_MAP)
    medication_only = evidence.filter(pl.col("evidence_group") == "medication_only")
    history = read_relevant_diagnosis_history(
        SOURCE_ROOT / "diagnosis", medication_only, indication_map, end_date=END_DATE
    )
    demographics = read_demographics(SOURCE_ROOT / "demographic", evidence.get_column("ENC_HN")).unique(
        subset=["ENC_HN"], keep="first"
    )
    exclusions = match_alternative_indications(medication_only, history, demographics, indication_map)

    ht = evidence.join(exclusions.select("ENC_HN"), on="ENC_HN", how="anti")
    ht = assign_index_and_flag(ht, dx_working, med_working)
    full = build_full_rows(ht)

    adults, adult_stats = apply_adult_criteria(ht, demographics)
    lookback_ids = ht.get_column("ENC_HN")
    # Pinned to this source: the cohort2.py defaults follow update_config.py.
    lookback_dx, _, _ = read_lookback_diagnosis(LOOKBACK_ROOT / "diagnosis", cohort_ids=lookback_ids)
    lookback_med, _, _ = read_lookback_medication(LOOKBACK_ROOT / "bill", cohort_ids=lookback_ids, map_path=MEDICATION_MAP)
    dx_lookback = summarize_lookback_diagnosis(lookback_dx)
    med_lookback = summarize_lookback_medication(lookback_med)
    evaluated, cohort2 = apply_lookback_criteria(
        adults.select("ENC_HN", "index_date", "dob"), dx_lookback, med_lookback
    )

    full_index_all = full.join(
        ht.select("ENC_HN", "index_date")
        .join(dx_lookback.select("ENC_HN", "lookback_dx_date"), on="ENC_HN", how="left")
        .join(med_lookback.select("ENC_HN", "lookback_med_date"), on="ENC_HN", how="left")
        .select(
            "ENC_HN",
            pl.min_horizontal("index_date", "lookback_dx_date", "lookback_med_date").alias("index_date_all"),
        ),
        on="ENC_HN",
        how="left",
    )
    cohort1 = evaluated.select(
        "ENC_HN",
        pl.col("index_date").cast(pl.Datetime("ns")).alias("HT Date (Study Period)"),
        pl.col("index_date_all").cast(pl.Datetime("ns")).alias("HT Date (All Period)"),
        pl.col("dob").cast(pl.Datetime("ns")).alias("dob"),
    ).sort("ENC_HN")

    group = evidence.get_column("evidence_group")
    flags = ht.get_column("flag")
    counts = {
        "dx_candidates": evidence.filter(pl.col("diagnosis_date").is_not_null()).height,
        "med_candidates": evidence.filter(pl.col("medication_date").is_not_null()).height,
        "icd_only": int((group == "icd_only").sum()),
        "both": int((group == "both").sum()),
        "medication_only": int((group == "medication_only").sum()),
        "other_indication": exclusions.height,
        "inferred": medication_only.height - exclusions.height,
        "ht_subjects": ht.height,
        "flag_icd": int((flags == "icd").sum()),
        "flag_med": int((flags == "med").sum()),
        "flag_both": int((flags == "both").sum()),
        "flag_missing": int(flags.is_null().sum()),
        **adult_stats,
        **null_id_rows,
        "lookback_dx": int(evaluated.get_column("has_lookback_dx").sum()),
        "lookback_med": int(evaluated.get_column("has_lookback_med").sum()),
        "prevalent": int(evaluated.get_column("prevalent_before_2010").sum()),
        "cohort2": cohort2.height,
    }

    (OUT_DIR / "full").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "clean_slate").mkdir(parents=True, exist_ok=True)
    full.write_parquet(OUT_DIR / "full" / f"{FULL_STEM}.parquet.gzip", compression="gzip")
    full.write_csv(OUT_DIR / "full" / f"{FULL_STEM}.csv")
    full_index_all.write_parquet(OUT_DIR / "full" / f"{FULL_STEM}_index_all.parquet.gzip", compression="gzip")
    full_index_all.write_csv(OUT_DIR / "full" / f"{FULL_STEM}_index_all.csv")
    cohort1.write_parquet(OUT_DIR / "index" / "cohort1.parquet.gzip", compression="gzip")
    cohort2.write_parquet(OUT_DIR / "index" / "cohort2.parquet.gzip", compression="gzip")
    evaluated.write_parquet(OUT_DIR / "index" / "cohort2_evaluated.parquet.gzip", compression="gzip")
    exclusions.write_parquet(OUT_DIR / "clean_slate" / "excluded_other_indication.parquet")
    pl.DataFrame(list(counts.items()), schema=["metric", "count"], orient="row").write_parquet(
        OUT_DIR / "clean_slate" / "flow_counts.parquet"
    )
    report = build_report(counts)
    (OUT_DIR / "clean_slate" / "flow_output.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()
