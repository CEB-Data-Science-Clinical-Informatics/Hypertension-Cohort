"""Build the loose hypertension-candidate cohort for a new observation chunk.

The loose stage is intentionally sensitive. A patient is a candidate when the
new chunk contains either a hypertension diagnosis or a mapped antihypertensive
medication event. Previously known hypertension patients are labelled and
removed from the new-candidate output.

The strict stage imports this module so both stages use identical source
readers, code normalization, event schemas, and patient summaries.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl


from update_config import (
    END_DATE,
    MEDICATION_MAP,
    LOOSE_FOLLOWUP_ROOT,
    LOOSE_NEWCASE_ROOT,
    PERIOD,
    PREV_PERIOD,
    START_DATE,
    read_map,
    require,
)
from ident_summary import summarize
from flow_figures import write_flow


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Period settings live in update_config.py.
IDENT_DIR = PROJECT_ROOT / "data/identification" / PERIOD

HT_PREFIXES = ("I10", "I11", "I12", "I13", "I15")
CHUNK_START = START_DATE
CHUNK_END = END_DATE
FOLLOWUP_DIAGNOSIS = LOOSE_FOLLOWUP_ROOT / "diagnosis"
FOLLOWUP_MEDICATION = LOOSE_FOLLOWUP_ROOT / "order"
NEWCASE_DIAGNOSIS = LOOSE_NEWCASE_ROOT / "Diagnosis"
NEWCASE_MEDICATION = LOOSE_NEWCASE_ROOT / "Drug (bill)"
# Previous period is the reference: its full file is the known-HT exclusion list.
PREV_IDENT_DIR = PROJECT_ROOT / "data/identification" / PREV_PERIOD
KNOWN_COHORT = PREV_IDENT_DIR / "full" / f"hthn_2010_{PREV_PERIOD.split('-')[-1]}.parquet.gzip"
DATA_DIR = IDENT_DIR / "loose"


def _normalize_code_value(value: object) -> str:
    """Normalize a diagnosis or medication code for stable matching."""

    if value is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _normalize_code_expr(column: str) -> pl.Expr:
    """Polars equivalent of :func:`_normalize_code_value`."""

    return (
        pl.col(column)
        .cast(pl.String)
        .str.to_uppercase()
        .str.replace_all(r"[^A-Z0-9]", "")
    )


def _date_expr(column: str) -> pl.Expr:
    """Parse native dates, datetimes, and YYYYMMDD strings to ``pl.Date``."""

    text = pl.col(column).cast(pl.String).str.replace(r"\.0$", "")
    return pl.coalesce(
        text.str.strptime(pl.Date, "%Y%m%d", strict=False),
        text.str.slice(0, 10).str.strptime(pl.Date, "%Y-%m-%d", strict=False),
    )


def _chunk_files(directory: Path) -> list[Path]:
    """Return supported data files in deterministic order."""

    if not directory.exists():
        raise FileNotFoundError(f"Source directory does not exist: {directory}")
    files = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.lower().endswith((".csv", ".parquet", ".parquet.gzip", ".gzip"))
    ]
    if not files:
        raise FileNotFoundError(f"No supported source files found in: {directory}")
    return sorted(files)


def _scan_file(file_path: Path) -> pl.LazyFrame:
    """Scan a CSV or parquet-like file without loading unused columns."""

    if file_path.name.lower().endswith(".csv"):
        return pl.scan_csv(file_path, infer_schema_length=10_000, ignore_errors=True)
    return pl.scan_parquet(file_path)


def _prefix_match_expr(column: str, prefixes: list[str] | tuple[str, ...]) -> pl.Expr:
    """Match normalized codes against one or more ICD prefixes."""

    normalized = [_normalize_code_value(prefix) for prefix in prefixes]
    normalized = [prefix for prefix in normalized if prefix]
    if not normalized:
        return pl.lit(False)
    pattern = r"^(?:" + "|".join(re.escape(prefix) for prefix in normalized) + ")"
    return pl.col(column).str.contains(pattern)


def _empty_diagnosis_events() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "ENC_HN": pl.String,
            "event_date": pl.Date,
            "diagnosis_code": pl.String,
            "source_population": pl.String,
            "evidence_source": pl.String,
        }
    )


def _empty_medication_events() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "ENC_HN": pl.String,
            "event_date": pl.Date,
            "medication_code": pl.String,
            "medication_class": pl.String,
            "source_population": pl.String,
            "evidence_source": pl.String,
        }
    )


def read_diagnosis_events(
    directory: Path,
    *,
    source_population: str,
    evidence_source: str,
    start_date: date,
    end_date: date,
    prefixes: tuple[str, ...] = HT_PREFIXES,
) -> pl.DataFrame:
    """Read normalized, in-window hypertension diagnosis events."""

    frames: list[pl.DataFrame] = []
    required = {"ENC_HN", "D001KEY", "D035KEY"}
    for file_path in _chunk_files(directory):
        lazy = _scan_file(file_path)
        schema = set(lazy.collect_schema().names())
        if not required.issubset(schema):
            continue
        frame = (
            lazy.select("ENC_HN", "D001KEY", "D035KEY")
            .with_columns(
                pl.col("ENC_HN").cast(pl.String),
                _date_expr("D001KEY").alias("event_date"),
                pl.col("D035KEY").cast(pl.String).str.split(",").alias("diagnosis_code"),
            )
            .explode("diagnosis_code")
            .with_columns(_normalize_code_expr("diagnosis_code").alias("diagnosis_code"))
            .filter(
                pl.col("ENC_HN").is_not_null()
                & pl.col("event_date").is_between(start_date, end_date, closed="both")
                & _prefix_match_expr("diagnosis_code", prefixes)
            )
            .select("ENC_HN", "event_date", "diagnosis_code")
            .unique()
            .collect()
            .with_columns(
                pl.lit(source_population).alias("source_population"),
                pl.lit(evidence_source).alias("evidence_source"),
            )
        )
        if not frame.is_empty():
            frames.append(frame)

    return pl.concat(frames, how="vertical_relaxed") if frames else _empty_diagnosis_events()


def load_medication_classes(map_path: Path) -> dict[str, str]:
    """Load antihypertensive medication code-to-subclass mappings."""

    mapping = read_map(map_path, "selected drug classes")
    required = {"ramadrugcode", "subclass"}
    if not required.issubset(mapping.columns):
        raise ValueError(f"{map_path} must contain columns: {sorted(required)}")
    pairs = (
        mapping.select("ramadrugcode", "subclass")
        .drop_nulls()
        .unique("ramadrugcode", keep="first")
        .iter_rows()
    )
    # Medication codes are matched exactly as delivered, never normalized.
    return {str(code): str(medication_class).strip() for code, medication_class in pairs if str(code)}


def _medication_columns(schema: set[str], source_population: str) -> tuple[str, str] | None:
    """Choose date and medication-code columns for each supported source."""

    candidates = (
        [("D001KEY", "D033KEY")]
        if source_population == "newcase"
        else [
            ("REC_DATE", "CODE"),
            ("PerformDate", "DSPCode"),
            ("D001KEY", "D033KEY"),
        ]
    )
    return next(((date_col, code_col) for date_col, code_col in candidates if {date_col, code_col}.issubset(schema)), None)


def read_medication_events(
    directory: Path,
    *,
    source_population: str,
    evidence_source: str,
    start_date: date,
    end_date: date,
    medication_classes: dict[str, str],
) -> pl.DataFrame:
    """Read normalized, mapped, in-window antihypertensive medication events."""

    map_frame = pl.DataFrame(
        {
            "medication_code": list(medication_classes),
            "medication_class": list(medication_classes.values()),
        }
    )
    frames: list[pl.DataFrame] = []
    for file_path in _chunk_files(directory):
        lazy = _scan_file(file_path)
        schema = set(lazy.collect_schema().names())
        columns = _medication_columns(schema, source_population)
        if columns is None or "ENC_HN" not in schema:
            continue
        date_column, code_column = columns
        frame = (
            lazy.select("ENC_HN", date_column, code_column)
            .with_columns(
                pl.col("ENC_HN").cast(pl.String),
                _date_expr(date_column).alias("event_date"),
                pl.col(code_column).cast(pl.String).alias("medication_code"),
            )
            .filter(
                pl.col("ENC_HN").is_not_null()
                & pl.col("event_date").is_between(start_date, end_date, closed="both")
            )
            .select("ENC_HN", "event_date", "medication_code")
            .collect()
            .join(map_frame, on="medication_code", how="inner")
            .unique()
            .with_columns(
                pl.lit(source_population).alias("source_population"),
                pl.lit(evidence_source).alias("evidence_source"),
            )
        )
        if not frame.is_empty():
            frames.append(frame)

    return pl.concat(frames, how="vertical_relaxed") if frames else _empty_medication_events()


def summarize_diagnosis_candidates(events: pl.DataFrame) -> pl.DataFrame:
    """Reduce diagnosis events to one record per candidate."""

    if events.is_empty():
        return pl.DataFrame(schema={"ENC_HN": pl.String, "first_diagnosis_date": pl.Date})
    return (
        events.group_by("ENC_HN")
        .agg(
            pl.col("event_date").min().alias("first_diagnosis_date"),
            pl.col("diagnosis_code").unique().sort().alias("diagnosis_codes"),
            pl.col("evidence_source").unique().sort().alias("diagnosis_sources"),
            (pl.col("source_population") == "followup").any().alias("has_followup_diagnosis"),
            (pl.col("source_population") == "newcase").any().alias("has_newcase_diagnosis"),
        )
        .sort("ENC_HN")
    )


def summarize_medication_candidates(events: pl.DataFrame) -> pl.DataFrame:
    """Reduce medication events to one record per candidate."""

    if events.is_empty():
        return pl.DataFrame(schema={"ENC_HN": pl.String, "first_medication_date": pl.Date})
    return (
        events.group_by("ENC_HN")
        .agg(
            pl.col("event_date").min().alias("first_medication_date"),
            pl.col("medication_code").unique().sort().alias("medication_codes"),
            pl.col("medication_class").unique().sort().alias("medication_classes"),
            pl.col("evidence_source").unique().sort().alias("medication_sources"),
            (pl.col("source_population") == "followup").any().alias("has_followup_order"),
            (pl.col("source_population") == "newcase").any().alias("has_newcase_bill"),
        )
        .sort("ENC_HN")
    )


def combine_candidates(
    diagnosis_candidates: pl.DataFrame,
    medication_candidates: pl.DataFrame,
) -> pl.DataFrame:
    """Combine both evidence sides and derive the first evidence date."""

    combined = diagnosis_candidates.join(
        medication_candidates,
        on="ENC_HN",
        how="full",
        coalesce=True,
    )
    return combined.with_columns(
        pl.min_horizontal("first_diagnosis_date", "first_medication_date").alias("first_candidate_date"),
        pl.when(pl.col("first_diagnosis_date").is_not_null() & pl.col("first_medication_date").is_not_null())
        .then(pl.lit("both"))
        .when(pl.col("first_diagnosis_date").is_not_null())
        .then(pl.lit("diagnosis_only"))
        .otherwise(pl.lit("medication_only"))
        .alias("evidence_group"),
        pl.col("first_diagnosis_date").is_not_null().alias("has_diagnosis"),
        pl.col("first_medication_date").is_not_null().alias("has_medication"),
    ).sort("ENC_HN")


def read_known_patient_ids(path: Path) -> pl.Series:
    """Read unique historical patient identifiers from a supported cohort file."""

    if path.name.lower().endswith((".parquet", ".parquet.gzip", ".gzip")):
        known = pl.read_parquet(path, columns=["ENC_HN"])
    elif path.suffix.lower() == ".csv":
        known = pl.read_csv(path, columns=["ENC_HN"])
    elif path.suffix.lower() in {".xlsx", ".xls"}:
        known = pl.read_excel(path).select("ENC_HN")
    else:
        raise ValueError(f"Unsupported known-cohort format: {path}")
    return known.get_column("ENC_HN").cast(pl.String).drop_nulls().unique()


def exclude_known_cases(
    candidates: pl.DataFrame,
    known_cohort_path: Path = KNOWN_COHORT,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Label all evaluated candidates and return only previously unknown cases."""

    known_ids = read_known_patient_ids(known_cohort_path)
    evaluated = candidates.with_columns(pl.col("ENC_HN").is_in(known_ids).alias("is_known_case"))
    return evaluated, evaluated.filter(~pl.col("is_known_case"))


def build_counts(
    diagnosis_candidates: pl.DataFrame,
    medication_candidates: pl.DataFrame,
    evaluated: pl.DataFrame,
    new_candidates: pl.DataFrame,
) -> pl.DataFrame:
    """Create an auditable loose-stage flow table."""

    values = {
        "diagnosis_candidates": diagnosis_candidates.height,
        "medication_candidates": medication_candidates.height,
        "evaluated_candidates": evaluated.height,
        "known_cases_removed": evaluated.filter(pl.col("is_known_case")).height,
        "new_candidates": new_candidates.height,
        "new_diagnosis_only": new_candidates.filter(pl.col("evidence_group") == "diagnosis_only").height,
        "new_medication_only": new_candidates.filter(pl.col("evidence_group") == "medication_only").height,
        "new_both": new_candidates.filter(pl.col("evidence_group") == "both").height,
    }
    return pl.DataFrame({"metric": list(values), "count": list(values.values())})


def build_loose_cohort() -> dict[str, pl.DataFrame]:
    """Run the loose stage in memory and return its auditable datasets."""

    medication_classes = load_medication_classes(MEDICATION_MAP)
    diagnosis_events = pl.concat(
        [
            read_diagnosis_events(
                FOLLOWUP_DIAGNOSIS,
                source_population="followup",
                evidence_source="followup_diagnosis",
                start_date=CHUNK_START,
                end_date=CHUNK_END,
            ),
            read_diagnosis_events(
                NEWCASE_DIAGNOSIS,
                source_population="newcase",
                evidence_source="newcase_diagnosis",
                start_date=CHUNK_START,
                end_date=CHUNK_END,
            ),
        ],
        how="vertical_relaxed",
    ).unique()
    medication_events = pl.concat(
        [
            read_medication_events(
                FOLLOWUP_MEDICATION,
                source_population="followup",
                evidence_source="followup_order",
                start_date=CHUNK_START,
                end_date=CHUNK_END,
                medication_classes=medication_classes,
            ),
            read_medication_events(
                NEWCASE_MEDICATION,
                source_population="newcase",
                evidence_source="newcase_bill",
                start_date=CHUNK_START,
                end_date=CHUNK_END,
                medication_classes=medication_classes,
            ),
        ],
        how="vertical_relaxed",
    ).unique()
    diagnosis_candidates = summarize_diagnosis_candidates(diagnosis_events)
    medication_candidates = summarize_medication_candidates(medication_events)
    candidates = combine_candidates(diagnosis_candidates, medication_candidates)
    evaluated, new_candidates = exclude_known_cases(candidates)
    counts = build_counts(diagnosis_candidates, medication_candidates, evaluated, new_candidates)
    return {
        "diagnosis_events": diagnosis_events,
        "medication_events": medication_events,
        "diagnosis_candidates": diagnosis_candidates,
        "medication_candidates": medication_candidates,
        "evaluated_candidates": evaluated,
        "new_candidates": new_candidates,
        "counts": counts,
    }


def flow_values(results: dict[str, pl.DataFrame]) -> dict[str, int]:
    """Flowchart numbers (unique ENC_HN), shared by the report and flow_figures.py.

    The loose stage has no other-indication check and no adult criterion, so
    other indication is 0, inferred diagnosis equals the medication-only group
    and the flow stops at the HT subjects total.
    """

    evaluated = results["evaluated_candidates"]
    new_candidates = results["new_candidates"]
    known = pl.col("is_known_case")
    group = pl.col("evidence_group")
    v = {
        "dx_candidates": results["diagnosis_candidates"].height,
        "med_candidates": results["medication_candidates"].height,
        "known_removed": evaluated.filter(known).height,
        "dx_known_removed": evaluated.filter(known & pl.col("has_diagnosis")).height,
        "med_known_removed": evaluated.filter(known & pl.col("has_medication")).height,
        "new_candidates": new_candidates.height,
        "icd_only": new_candidates.filter(group == "diagnosis_only").height,
        "both": new_candidates.filter(group == "both").height,
        "medication_only": new_candidates.filter(group == "medication_only").height,
        "other_indication": 0,
    }
    v["inferred"] = v["medication_only"] - v["other_indication"]
    v["ht_subjects"] = v["icd_only"] + v["both"] + v["inferred"]
    return v


def build_flow_lines(results: dict[str, pl.DataFrame]) -> list[str]:
    """Describe the loose candidate flow in the same shape as the strict report."""

    v = flow_values(results)
    icd_only, both, med_only = v["icd_only"], v["both"], v["medication_only"]
    return [
        "Candidate flow (unique ENC_HN)",
        "------------------------------",
        f"1. Diagnosis file, HT ICD candidates:            {v['dx_candidates']:,}",
        f"2. Medication file, antihypertensive candidates: {v['med_candidates']:,}",
        f"   Known HT cases removed (historical cohort):   {v['known_removed']:,}"
        f"  [from diagnosis: {v['dx_known_removed']:,}, from medication: {v['med_known_removed']:,}]",
        f"   New candidates after removal:                 {v['new_candidates']:,}"
        f"  [in diagnosis: {icd_only + both:,}, in medication: {both + med_only:,}]",
        "3. Cross-check of diagnosis vs medication:",
        f"   ICD only:                                     {icd_only:,}",
        f"   Both ICD and medication:                      {both:,}",
        f"   Medication only:                              {med_only:,}",
        f"4. Diagnosis of HT (ICD only + both):            {icd_only + both:,}",
        "5. Medication only, other-indication check:       not applied in loose stage",
        f"   Other indication found (excluded):            {v['other_indication']:,}",
        f"6. Inferred diagnosis (medication only left over): {v['inferred']:,}",
        f"7. HT subjects (ICD only + both + inferred):     {v['ht_subjects']:,}",
    ]


def write_loose_outputs(results: dict[str, pl.DataFrame]) -> None:
    """Write dementia-style loose artifacts and a readable run summary."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_names = {
        "diagnosis_events": "loose_diagnosis_events.parquet",
        "medication_events": "loose_medication_events.parquet",
        "diagnosis_candidates": "loose_diagnosis_candidates.parquet",
        "medication_candidates": "loose_medication_candidates.parquet",
        "evaluated_candidates": "loose_candidates_before_known_exclusion.parquet",
        "new_candidates": "loose_new_candidates.parquet",
        "counts": "loose_candidate_counts.parquet",
    }
    for name, filename in output_names.items():
        results[name].write_parquet(DATA_DIR / filename)

    count_lines = [
        f"{metric}: {count:,}"
        for metric, count in results["counts"].iter_rows()
    ]
    report = [
        "HYPERTENSION LOOSE COHORT OUTPUT",
        "=================================",
        f"Observation period: {CHUNK_START} through {CHUNK_END}, inclusive",
        "Diagnosis prefixes: " + ", ".join(HT_PREFIXES),
        f"Known cohort: {KNOWN_COHORT}",
        "",
        *build_flow_lines(results),
        "",
        "Loose-stage counts (loose_candidate_counts.parquet)",
        "---------------------------------------------------",
        *count_lines,
        "",
        "Output files",
        "------------",
        *(output_names.values()),
    ]
    (DATA_DIR / "loose_output.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    require(
        [FOLLOWUP_DIAGNOSIS, FOLLOWUP_MEDICATION, NEWCASE_DIAGNOSIS, NEWCASE_MEDICATION, KNOWN_COHORT],
        "Loose stage",
    )
    results = build_loose_cohort()
    write_loose_outputs(results)
    print(results["counts"])
    summarize("loose")          # loose/loose_summary.txt (shared format)
    write_flow("loose", flow_values(results))   # loose/flowchart_numbers_loose.txt (up to HT subjects)


if __name__ == "__main__":
    main()
