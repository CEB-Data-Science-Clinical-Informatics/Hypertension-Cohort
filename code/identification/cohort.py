"""Build the strict hypertension cohort from the governed second data batch.

Workflow boundary:
1. ``cohort_loose.py`` reads the first batch and discovers candidates.
2. Candidate identifiers are sent to data governance.
3. Data governance returns a second, candidate-specific ``*_nc`` batch.
4. This standalone script reads that second batch and applies exact criteria.

This module deliberately does not import or read outputs from ``cohort_loose``.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import polars as pl

# Same lookback readers as cohort2.py, so the pre-2010 HT date is defined once.
from cohort2 import (
    read_lookback_diagnosis,
    read_lookback_medication,
    summarize_lookback_diagnosis,
    summarize_lookback_medication,
)


from update_config import END_DATE, MEDICATION_MAP, PERIOD, PREV_PERIOD, START_DATE, STRICT_ROOT, read_map, require
from ident_summary import summarize
from flow_figures import write_flow


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Period settings and the governed batch location live in update_config.py.
IDENT_DIR = PROJECT_ROOT / "data/identification" / PERIOD
DATA_DIR = IDENT_DIR / "strict"

DIAGNOSIS_PATH = STRICT_ROOT / "diagnosis"
ORDER_PATH = STRICT_ROOT / "order"
MEDICATION_PATH = STRICT_ROOT / "medication"
DEMOGRAPHIC_PATH = STRICT_ROOT / "demographic"

# Previous period is the reference: its full file is the known-HT exclusion list.
PREV_IDENT_DIR = PROJECT_ROOT / "data/identification" / PREV_PERIOD
KNOWN_COHORT = PREV_IDENT_DIR / "full" / f"hthn_2010_{PREV_PERIOD.split('-')[-1]}.parquet.gzip"
PRIOR_COHORT1 = PREV_IDENT_DIR / "index/cohort1.parquet.gzip"
FULL_DIR = IDENT_DIR / "full"
FULL_STEM = f"hthn_2010_{PERIOD.split('-')[-1]}"
COHORT1_OUT = IDENT_DIR / "index/cohort1.parquet.gzip"
FULL_COLUMNS = ["ENC_HN", "ICD_DATE", "ICD", "MED_DATE", "MED", "D001KEY", "flag"]
HT_PREFIXES = ("I10", "I11", "I12", "I13", "I15")


def _normalize_code_value(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _normalize_code_expr(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.String)
        .str.to_uppercase()
        .str.replace_all(r"[^A-Z0-9]", "")
    )


def _medication_code_expr(column: str) -> pl.Expr:
    """Medication codes are matched exactly as delivered, never normalized.

    Stripping characters could merge two codes that the hospital assigns to
    different drug classes (for example "FUR#-N-" and "FUR+-N-").
    """

    return pl.col(column).cast(pl.String)


def _date_expr(column: str) -> pl.Expr:
    text = pl.col(column).cast(pl.String).str.replace(r"\.0$", "")
    return pl.coalesce(
        text.str.strptime(pl.Date, "%Y%m%d", strict=False),
        text.str.slice(0, 10).str.strptime(pl.Date, "%Y-%m-%d", strict=False),
    )


def _scan_file(file_path: Path) -> pl.LazyFrame:
    if file_path.name.lower().endswith(".csv"):
        return pl.scan_csv(file_path, infer_schema_length=10_000, ignore_errors=True)
    return pl.scan_parquet(file_path)


def _chunk_files(directory: Path, *, required: bool = True) -> list[Path]:
    if not directory.exists():
        if required:
            raise FileNotFoundError(
                f"Governed strict-batch directory is not available: {directory}"
            )
        return []
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.lower().endswith((".csv", ".parquet", ".parquet.gzip", ".gzip"))
    )
    if required and not files:
        raise FileNotFoundError(f"No strict-batch files found in: {directory}")
    return files


def _prefix_match_expr(column: str, prefixes: list[str] | tuple[str, ...]) -> pl.Expr:
    normalized = [_normalize_code_value(prefix) for prefix in prefixes]
    normalized = [prefix for prefix in normalized if prefix]
    if not normalized:
        return pl.lit(False)
    pattern = r"^(?:" + "|".join(re.escape(prefix) for prefix in normalized) + ")"
    return pl.col(column).str.contains(pattern)


def load_diagnosis_prefixes() -> list[str]:
    """Return the strict HT diagnosis prefixes."""

    return list(HT_PREFIXES)


def read_diagnosis_working_df(
    diagnosis_dir: Path = DIAGNOSIS_PATH,
    prefixes: list[str] | tuple[str, ...] = HT_PREFIXES,
    start_date: date = START_DATE,
    end_date: date = END_DATE,
) -> pl.DataFrame:
    """Read qualifying HT diagnoses from the governed second batch."""

    frames: list[pl.DataFrame] = []
    required_columns = {"ENC_HN", "D001KEY", "D035KEY"}
    for file_path in _chunk_files(diagnosis_dir):
        lazy = _scan_file(file_path)
        if not required_columns.issubset(set(lazy.collect_schema().names())):
            continue
        frame = (
            lazy.select("ENC_HN", "D001KEY", "D035KEY")
            .with_columns(
                pl.col("ENC_HN").cast(pl.String),
                _date_expr("D001KEY").alias("D001KEY"),
                pl.col("D035KEY").cast(pl.String).str.split(","),
            )
            .explode("D035KEY")
            .with_columns(_normalize_code_expr("D035KEY").alias("D035KEY"))
            .filter(
                pl.col("D001KEY").is_between(start_date, end_date, closed="both")
                & _prefix_match_expr("D035KEY", prefixes)
            )
            .select("ENC_HN", "D001KEY", "D035KEY")
            .unique()
            .collect()
        )
        if not frame.is_empty():
            frames.append(frame)
    if frames:
        return pl.concat(frames, how="vertical_relaxed").unique().sort("ENC_HN", "D001KEY")
    return pl.DataFrame(
        schema={"ENC_HN": pl.String, "D001KEY": pl.Date, "D035KEY": pl.String}
    )


def build_diagnosis_summary(dx_working_df: pl.DataFrame) -> pl.DataFrame:
    if dx_working_df.is_empty():
        return pl.DataFrame(schema={"ENC_HN": pl.String, "diagnosis_date": pl.Date})
    return (
        dx_working_df.group_by("ENC_HN")
        .agg(
            pl.col("D001KEY").min().alias("diagnosis_date"),
            pl.col("D035KEY").unique().sort().alias("dx_icd_codes"),
        )
        .sort("ENC_HN")
    )


def _load_medication_maps(map_path: Path = MEDICATION_MAP) -> pl.DataFrame:
    mapping = read_map(map_path, "selected drug classes")
    required = {"ramadrugcode", "subclass"}
    if not required.issubset(mapping.columns):
        raise ValueError(f"{map_path} must contain columns: {sorted(required)}")
    return (
        mapping.select(
            _medication_code_expr("ramadrugcode").alias("drug_code"),
            pl.col("subclass").cast(pl.String).str.strip_chars().alias("drug_class"),
        )
        .drop_nulls()
        .filter(pl.col("drug_code") != "")
        .unique("drug_code", keep="first")
    )


def _list_medication_files(
    order_dir: Path = ORDER_PATH,
    medication_dir: Path = MEDICATION_PATH,
) -> list[Path]:
    """Use all available governed order and validated-medication files."""

    files = _chunk_files(order_dir, required=False) + _chunk_files(
        medication_dir, required=False
    )
    if not files:
        raise FileNotFoundError(
            "No governed strict medication files found in either "
            f"{order_dir} or {medication_dir}"
        )
    return sorted(set(files))


def _medication_columns(schema: set[str]) -> tuple[str, str] | None:
    candidates = [
        ("REC_DATE", "CODE"),
        ("PerformDate", "DSPCode"),
        ("D001KEY", "Drugcode"),
        ("D001KEY", "D033KEY"),
        ("BillDate", "Drugcode"),
    ]
    return next(
        ((date_col, code_col) for date_col, code_col in candidates if {date_col, code_col}.issubset(schema)),
        None,
    )


def _standardize_med_file(file_path: Path) -> pl.DataFrame:
    lazy = _scan_file(file_path)
    schema = set(lazy.collect_schema().names())
    columns = _medication_columns(schema)
    if columns is None or "ENC_HN" not in schema:
        return pl.DataFrame(
            schema={"ENC_HN": pl.String, "REC_DATE": pl.Date, "CODE": pl.String}
        )
    date_column, code_column = columns
    return (
        lazy.select("ENC_HN", date_column, code_column)
        .with_columns(
            pl.col("ENC_HN").cast(pl.String),
            _date_expr(date_column).alias("REC_DATE"),
            _medication_code_expr(code_column).alias("CODE"),
        )
        .select("ENC_HN", "REC_DATE", "CODE")
        .collect()
    )


def read_medication_working_df(
    order_dir: Path = ORDER_PATH,
    medication_dir: Path = MEDICATION_PATH,
    start_date: date = START_DATE,
    end_date: date = END_DATE,
    map_path: Path = MEDICATION_MAP,
) -> pl.DataFrame:
    """Read qualifying medications from the governed second batch."""

    mapping = _load_medication_maps(map_path)
    frames: list[pl.DataFrame] = []
    for file_path in _list_medication_files(order_dir, medication_dir):
        frame = (
            _standardize_med_file(file_path)
            .filter(pl.col("REC_DATE").is_between(start_date, end_date, closed="both"))
            .join(mapping, left_on="CODE", right_on="drug_code", how="inner")
            .unique()
        )
        if not frame.is_empty():
            frames.append(frame)
    if frames:
        return pl.concat(frames, how="vertical_relaxed").unique().sort("ENC_HN", "REC_DATE")
    return pl.DataFrame(
        schema={
            "ENC_HN": pl.String,
            "REC_DATE": pl.Date,
            "CODE": pl.String,
            "drug_class": pl.String,
        }
    )


def build_medication_summary(med_working_df: pl.DataFrame) -> pl.DataFrame:
    if med_working_df.is_empty():
        return pl.DataFrame(schema={"ENC_HN": pl.String, "medication_date": pl.Date})
    return (
        med_working_df.group_by("ENC_HN")
        .agg(
            pl.col("REC_DATE").min().alias("medication_date"),
            pl.col("CODE").unique().sort().alias("medication_codes"),
            pl.col("drug_class").unique().sort().alias("medication_classes"),
        )
        .sort("ENC_HN")
    )


def _read_known_patient_ids(path: Path = KNOWN_COHORT) -> pl.Series:
    if path.name.lower().endswith((".parquet", ".parquet.gzip", ".gzip")):
        known = pl.read_parquet(path, columns=["ENC_HN"])
    elif path.suffix.lower() == ".csv":
        known = pl.read_csv(path, columns=["ENC_HN"])
    else:
        known = pl.read_excel(path).select("ENC_HN")
    return known.get_column("ENC_HN").cast(pl.String).drop_nulls().unique()


def combine_evidence(
    dx_summary_df: pl.DataFrame,
    med_summary_df: pl.DataFrame,
    known_cohort_path: Path = KNOWN_COHORT,
) -> pl.DataFrame:
    """Combine strict-batch evidence and remove historical HT cases."""

    known_ids = _read_known_patient_ids(known_cohort_path)
    return (
        dx_summary_df.join(med_summary_df, on="ENC_HN", how="full", coalesce=True)
        .filter(~pl.col("ENC_HN").is_in(known_ids))
        .with_columns(
            pl.min_horizontal("diagnosis_date", "medication_date").alias("index_date"),
            pl.when(pl.col("diagnosis_date").is_not_null() & pl.col("medication_date").is_not_null())
            .then(pl.lit("both"))
            .when(pl.col("diagnosis_date").is_not_null())
            .then(pl.lit("diagnosis_only"))
            .otherwise(pl.lit("medication_only"))
            .alias("evidence_group"),
        )
        .sort("ENC_HN")
    )


def load_other_indications(map_path: Path = MEDICATION_MAP) -> pl.DataFrame:
    source = read_map(map_path, "other indications")
    required = {"subclass", "Other Indications", "ICD-10", "Gender"}
    if not required.issubset(source.columns):
        raise ValueError(f"{map_path} must contain columns: {sorted(required)}")
    return (
        source.select(
            pl.col("subclass").cast(pl.String).str.strip_chars().alias("medication_class"),
            pl.col("Other Indications").cast(pl.String).str.strip_chars().alias("alternative_indication"),
            pl.col("ICD-10").cast(pl.String).str.split(",").alias("diagnosis_prefix"),
            pl.col("Gender").cast(pl.String).str.strip_chars().alias("required_gender"),
        )
        .explode("diagnosis_prefix")
        .with_columns(_normalize_code_expr("diagnosis_prefix").alias("diagnosis_prefix"))
        .filter(pl.col("diagnosis_prefix") != "")
        .unique()
    )


def read_relevant_diagnosis_history(
    diagnosis_dir: Path,
    medication_only: pl.DataFrame,
    indication_map: pl.DataFrame,
    end_date: date = END_DATE,
) -> pl.DataFrame:
    """Read pre-medication history from the same governed strict batch."""

    if medication_only.is_empty():
        return pl.DataFrame(
            schema={"ENC_HN": pl.String, "event_date": pl.Date, "diagnosis_code": pl.String}
        )
    patient_ids = medication_only.get_column("ENC_HN")
    prefixes = indication_map.get_column("diagnosis_prefix").unique().to_list()
    frames: list[pl.DataFrame] = []
    for file_path in _chunk_files(diagnosis_dir):
        lazy = _scan_file(file_path)
        if not {"ENC_HN", "D001KEY", "D035KEY"}.issubset(
            set(lazy.collect_schema().names())
        ):
            continue
        frame = (
            lazy.select("ENC_HN", "D001KEY", "D035KEY")
            .with_columns(
                pl.col("ENC_HN").cast(pl.String),
                _date_expr("D001KEY").alias("event_date"),
                pl.col("D035KEY").cast(pl.String).str.split(",").alias("diagnosis_code"),
            )
            .filter(pl.col("ENC_HN").is_in(patient_ids))
            .explode("diagnosis_code")
            .with_columns(_normalize_code_expr("diagnosis_code").alias("diagnosis_code"))
            .filter(
                (pl.col("event_date") <= end_date)
                & _prefix_match_expr("diagnosis_code", prefixes)
            )
            .select("ENC_HN", "event_date", "diagnosis_code")
            .unique()
            .collect()
        )
        if not frame.is_empty():
            frames.append(frame)
    if frames:
        return pl.concat(frames, how="vertical_relaxed").unique()
    return pl.DataFrame(
        schema={"ENC_HN": pl.String, "event_date": pl.Date, "diagnosis_code": pl.String}
    )


def read_demographics(
    demographic_dir: Path,
    patient_ids: pl.Series,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for file_path in _chunk_files(demographic_dir):
        lazy = _scan_file(file_path)
        schema = set(lazy.collect_schema().names())
        if "ENC_HN" not in schema or not ({"D020AT3", "H2L1KEY"} & schema):
            continue
        frame = (
            lazy.select(
                "ENC_HN",
                _date_expr("D020AT3").alias("date_of_birth")
                if "D020AT3" in schema
                else pl.lit(None, dtype=pl.Date).alias("date_of_birth"),
                pl.col("H2L1KEY")
                .cast(pl.String)
                .replace({"M": "Male", "F": "Female"})
                .alias("gender")
                if "H2L1KEY" in schema
                else pl.lit(None, dtype=pl.String).alias("gender"),
            )
            .with_columns(pl.col("ENC_HN").cast(pl.String))
            .filter(pl.col("ENC_HN").is_in(patient_ids))
            .collect()
        )
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return pl.DataFrame(
            schema={"ENC_HN": pl.String, "date_of_birth": pl.Date, "gender": pl.String}
        )
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .group_by("ENC_HN")
        .agg(
            pl.col("date_of_birth").drop_nulls().first(),
            pl.col("gender").drop_nulls().first(),
        )
    )


def match_alternative_indications(
    medication_only: pl.DataFrame,
    diagnosis_history: pl.DataFrame,
    demographics: pl.DataFrame,
    indication_map: pl.DataFrame,
) -> pl.DataFrame:
    empty = pl.DataFrame(
        schema={
            "ENC_HN": pl.String,
            "alternative_indications": pl.List(pl.String),
            "alternative_diagnosis_codes": pl.List(pl.String),
            "first_alternative_diagnosis_date": pl.Date,
        }
    )
    if medication_only.is_empty() or diagnosis_history.is_empty():
        return empty
    candidates = medication_only.join(demographics, on="ENC_HN", how="left")
    matches: list[pl.DataFrame] = []
    for rule in indication_map.iter_rows(named=True):
        eligible = candidates.filter(
            pl.col("medication_classes").list.contains(rule["medication_class"])
        )
        if rule["required_gender"].strip().lower() == "female":
            eligible = eligible.filter(pl.col("gender") == "Female")
        if eligible.is_empty():
            continue
        match = (
            diagnosis_history.join(
                eligible.select("ENC_HN", "medication_date"),
                on="ENC_HN",
                how="inner",
            )
            .filter(
                (pl.col("event_date") <= pl.col("medication_date"))
                & pl.col("diagnosis_code").str.starts_with(rule["diagnosis_prefix"])
            )
            .with_columns(
                pl.lit(rule["alternative_indication"]).alias("alternative_indication")
            )
        )
        if not match.is_empty():
            matches.append(match)
    if not matches:
        return empty
    return (
        pl.concat(matches, how="vertical_relaxed")
        .group_by("ENC_HN")
        .agg(
            pl.col("alternative_indication").unique().sort().alias("alternative_indications"),
            pl.col("diagnosis_code").unique().sort().alias("alternative_diagnosis_codes"),
            pl.col("event_date").min().alias("first_alternative_diagnosis_date"),
        )
        .sort("ENC_HN")
    )


def apply_exact_criteria(
    exact_base_df: pl.DataFrame,
    exclusion_matches_df: pl.DataFrame,
    demographics_df: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Apply HT alternative-indication exclusion and adult eligibility."""

    evaluated = (
        exact_base_df.join(exclusion_matches_df, on="ENC_HN", how="left")
        .join(demographics_df, on="ENC_HN", how="left")
        .with_columns(
            pl.col("alternative_indications").is_not_null().alias("excluded_other_indication"),
            ((pl.col("index_date") - pl.col("date_of_birth")).dt.total_days() / 365.25).alias(
                "age_at_index"
            ),
        )
        .with_columns((pl.col("age_at_index") >= 18).fill_null(False).alias("adult_eligible"))
        .with_columns(
            (~pl.col("excluded_other_indication") & pl.col("adult_eligible")).alias(
                "strict_included"
            ),
            pl.when(pl.col("excluded_other_indication"))
            .then(pl.lit("medication-only alternative indication"))
            .when(pl.col("date_of_birth").is_null())
            .then(pl.lit("missing date of birth"))
            .when(~pl.col("adult_eligible"))
            .then(pl.lit("age below 18"))
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias("exclusion_reason"),
        )
        .sort("ENC_HN")
    )
    return evaluated, evaluated.filter(pl.col("strict_included"))


def build_exact_counts(evaluated: pl.DataFrame, final_cohort: pl.DataFrame) -> pl.DataFrame:
    values = {
        "strict_batch_candidates": evaluated.height,
        "diagnosis_supported": evaluated.filter(
            pl.col("evidence_group") != "medication_only"
        ).height,
        "medication_only_evaluated": evaluated.filter(
            pl.col("evidence_group") == "medication_only"
        ).height,
        "excluded_other_indication": evaluated.filter(
            pl.col("excluded_other_indication")
        ).height,
        "excluded_missing_date_of_birth": evaluated.filter(
            pl.col("exclusion_reason") == "missing date of birth"
        ).height,
        "excluded_age_below_18": evaluated.filter(
            pl.col("exclusion_reason") == "age below 18"
        ).height,
        "strict_new_cohort": final_cohort.height,
    }
    return pl.DataFrame({"metric": list(values), "count": list(values.values())})


def flow_values(results: dict[str, object]) -> dict[str, int]:
    """Flowchart numbers (unique ENC_HN), shared by the report and flow_figures.py.

    The known-cohort removal happens inside ``combine_evidence``, so removed
    counts are derived by difference.
    """

    dx_summary = results["dx_summary"]
    med_summary = results["med_summary"]
    evaluated = results["exact_criteria_base"]
    group = pl.col("evidence_group")
    v = {
        "dx_candidates": dx_summary.height,
        "med_candidates": med_summary.height,
        "new_candidates": evaluated.height,
        "icd_only": evaluated.filter(group == "diagnosis_only").height,
        "both": evaluated.filter(group == "both").height,
        "medication_only": evaluated.filter(group == "medication_only").height,
        "other_indication": evaluated.filter(pl.col("excluded_other_indication")).height,
        "missing_dob": evaluated.filter(pl.col("exclusion_reason") == "missing date of birth").height,
        "below_18": evaluated.filter(pl.col("exclusion_reason") == "age below 18").height,
        "cohort1": results["hypertension_index"].height,
    }
    all_candidates = pl.concat(
        [dx_summary.get_column("ENC_HN"), med_summary.get_column("ENC_HN")]
    ).n_unique()
    v["known_removed"] = all_candidates - v["new_candidates"]
    v["dx_known_removed"] = v["dx_candidates"] - (v["icd_only"] + v["both"])
    v["med_known_removed"] = v["med_candidates"] - (v["both"] + v["medication_only"])
    v["inferred"] = v["medication_only"] - v["other_indication"]
    v["ht_subjects"] = v["icd_only"] + v["both"] + v["inferred"]
    return v


def build_flow_lines(results: dict[str, object]) -> list[str]:
    """Describe the candidate flow for the strict output report."""

    v = flow_values(results)
    dx_candidates, med_candidates = v["dx_candidates"], v["med_candidates"]
    new_candidates, known_removed = v["new_candidates"], v["known_removed"]
    dx_known_removed, med_known_removed = v["dx_known_removed"], v["med_known_removed"]
    icd_only, both, med_only = v["icd_only"], v["both"], v["medication_only"]
    diagnosis_of_ht = icd_only + both
    other_indication, inferred, ht_subjects = v["other_indication"], v["inferred"], v["ht_subjects"]
    missing_dob, below_18 = v["missing_dob"], v["below_18"]
    final_cohort = results["hypertension_index"]

    return [
        "Candidate flow (unique ENC_HN)",
        "------------------------------",
        f"1. Diagnosis file, HT ICD candidates:            {dx_candidates:,}",
        f"2. Medication file, antihypertensive candidates: {med_candidates:,}",
        f"   Known HT cases removed (historical cohort):   {known_removed:,}"
        f"  [from diagnosis: {dx_known_removed:,}, from medication: {med_known_removed:,}]",
        f"   New candidates after removal:                 {new_candidates:,}"
        f"  [in diagnosis: {icd_only + both:,}, in medication: {both + med_only:,}]",
        "3. Cross-check of diagnosis vs medication:",
        f"   ICD only:                                     {icd_only:,}",
        f"   Both ICD and medication:                      {both:,}",
        f"   Medication only:                              {med_only:,}",
        f"4. Diagnosis of HT (ICD only + both):            {diagnosis_of_ht:,}",
        "5. Medication only, other-indication check:",
        f"   Other indication found (excluded):            {other_indication:,}",
        f"6. Inferred diagnosis (medication only left over): {inferred:,}",
        f"7. HT subjects (ICD only + both + inferred):     {ht_subjects:,}",
        "",
        "Adult eligibility applied to HT subjects",
        "----------------------------------------",
        f"   Missing date of birth (excluded):             {missing_dob:,}",
        f"   Age below 18 at index (excluded):             {below_18:,}",
        f"   Strict new cohort (hypertension_index):       {final_cohort.height:,}",
    ]


def _list_repr_expr(column: str) -> pl.Expr:
    """Render a list column the way the historical full file stores it: "['A', 'B']"."""

    return (
        pl.when(pl.col(column).is_null())
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(
            pl.concat_str(
                [
                    pl.lit("['"),
                    pl.col(column).list.join("', '"),
                    pl.lit("']"),
                ]
            )
        )
    )


def index_day_flag(
    subjects: pl.DataFrame,
    dx_working: pl.DataFrame,
    med_working: pl.DataFrame,
) -> pl.DataFrame:
    """Flag = criteria met on the index date: icd, med, or both.

    ``subjects`` needs ENC_HN and index_date. Evidence on other days does not
    count, so a subject with a diagnosis on the index date and a medication a
    week later is ``icd``.
    """

    dx_days = (
        dx_working.select("ENC_HN", pl.col("D001KEY").alias("index_date"))
        .unique()
        .with_columns(pl.lit(True).alias("dx_on_index"))
    )
    med_days = (
        med_working.select("ENC_HN", pl.col("REC_DATE").alias("index_date"))
        .unique()
        .with_columns(pl.lit(True).alias("med_on_index"))
    )
    return (
        subjects.select("ENC_HN", "index_date")
        .join(dx_days, on=["ENC_HN", "index_date"], how="left")
        .join(med_days, on=["ENC_HN", "index_date"], how="left")
        .select(
            "ENC_HN",
            pl.when(pl.col("dx_on_index") & pl.col("med_on_index"))
            .then(pl.lit("both"))
            .when(pl.col("dx_on_index"))
            .then(pl.lit("icd"))
            .when(pl.col("med_on_index"))
            .then(pl.lit("med"))
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias("flag"),
        )
    )


def build_full_cohort_rows(evaluated: pl.DataFrame, flags: pl.DataFrame) -> pl.DataFrame:
    """Convert strict HT subjects (before adult criteria) to the full-file schema."""

    ht_subjects = evaluated.filter(~pl.col("excluded_other_indication")).join(
        flags, on="ENC_HN", how="left"
    )
    return ht_subjects.select(
        pl.col("ENC_HN"),
        pl.col("diagnosis_date").dt.strftime("%Y-%m-%d").alias("ICD_DATE"),
        _list_repr_expr("dx_icd_codes").alias("ICD"),
        pl.col("medication_date").dt.strftime("%Y-%m-%d").alias("MED_DATE"),
        _list_repr_expr("medication_codes").alias("MED"),
        pl.col("index_date").dt.strftime("%Y-%m-%d").alias("D001KEY"),
        pl.col("flag"),
    ).sort("ENC_HN")


# Legacy rows stored an ICD code as a list of its characters: "['I', '1', '0']".
LEGACY_CHAR_SPLIT = r"^\[('[A-Z0-9]', )*'[A-Z0-9]'\]$"
MISSING_TOKENS = ["None", "NaT", ""]


def _clean_code_column(column: str) -> pl.Expr:
    """One missing marker (null), and one list format: "['A', 'B']".

    Missing values become null. A character-split list is rejoined into the
    single code it spells, so "['I', '1', '0']" becomes "['I10']". Legacy lists
    separated by spaces instead of ", " are rebuilt with ", ".
    """

    value = pl.col(column)
    missing = value.is_null() | value.is_in(MISSING_TOKENS)
    char_split = value.str.contains(LEGACY_CHAR_SPLIT)
    if column == "MED":
        # Medication codes stay exactly as delivered: take each quoted value whole.
        tokens = (
            value.str.extract_all(r"'[^']+'")
            .list.eval(pl.element().str.strip_chars("'"))
            .list.unique()
            .list.sort()
        )
    else:
        # Diagnosis codes are normalized: no dots or dashes, same as the strict reader.
        tokens = (
            value.str.to_uppercase()
            .str.replace_all(r"[^A-Z0-9',\[\] ]", "")
            .str.extract_all(r"[A-Z0-9]+")
            .list.unique()
            .list.sort()
        )
    as_list = pl.concat_str([pl.lit("['"), tokens.list.join("', '"), pl.lit("']")])
    rejoined = pl.concat_str([pl.lit("['"), tokens.list.join(""), pl.lit("']")])
    return (
        pl.when(missing)
        .then(pl.lit(None, dtype=pl.String))
        .when(char_split)
        .then(rejoined)
        .when(value.str.starts_with("['"))
        .then(as_list)
        .otherwise(value)
        .alias(column)
    )


def _clean_date_column(column: str) -> pl.Expr:
    value = pl.col(column)
    return (
        pl.when(value.is_null() | value.is_in(MISSING_TOKENS))
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(value)
        .alias(column)
    )


def normalize_full_file(full: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    """Apply one missing-value rule and one list format to every row of the full file."""

    before_missing = sum(
        full.filter(pl.col(c).is_in(MISSING_TOKENS)).height for c in ["ICD", "MED", "ICD_DATE", "MED_DATE"]
    )
    rejoined = full.filter(pl.col("ICD").str.contains(LEGACY_CHAR_SPLIT)).height
    space_separated = full.filter(
        pl.col("ICD").str.contains(r"' '") | pl.col("MED").str.contains(r"' '")
    ).height
    cleaned = full.with_columns(
        _clean_code_column("ICD"),
        _clean_code_column("MED"),
        _clean_date_column("ICD_DATE"),
        _clean_date_column("MED_DATE"),
    )
    stats = {
        "legacy_codes_rejoined": rejoined,
        "legacy_lists_reformatted": space_separated,
        "missing_markers_blanked": before_missing,
    }
    return cleaned, stats


def append_full_cohort(
    new_rows: pl.DataFrame,
    known_cohort_path: Path = KNOWN_COHORT,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Append new HT subjects to the previous full file. Duplicate ENC_HN are dropped."""

    previous = pl.read_parquet(known_cohort_path).select(FULL_COLUMNS)
    previous = previous.with_columns(pl.col(column).cast(pl.String) for column in FULL_COLUMNS)
    previous, legacy_stats = normalize_full_file(previous)
    duplicates = new_rows.filter(pl.col("ENC_HN").is_in(previous.get_column("ENC_HN")))
    appended = new_rows.filter(~pl.col("ENC_HN").is_in(previous.get_column("ENC_HN")))
    full = pl.concat([previous, appended], how="vertical")
    stats = {
        "previous_full": previous.height,
        "new_ht_subjects": new_rows.height,
        "already_in_previous_full": duplicates.height,
        "appended": appended.height,
        "full": full.height,
        "full_unique_hn": full.get_column("ENC_HN").n_unique(),
        **legacy_stats,
    }
    return full, stats


def lookback_first_ht_dates(ids: pl.Series) -> pl.DataFrame:
    """Earliest pre-2010 HT evidence per subject, from the 2005-2009 lookback.

    Evidence is a qualifying HT diagnosis or a billed antihypertensive, the same
    definition cohort2.py uses for ``index_date_all``.
    """

    dx_working, _, _ = read_lookback_diagnosis(cohort_ids=ids)
    med_working, _, _ = read_lookback_medication(cohort_ids=ids)
    dx = summarize_lookback_diagnosis(dx_working).select("ENC_HN", "lookback_dx_date")
    med = summarize_lookback_medication(med_working).select("ENC_HN", "lookback_med_date")
    return (
        dx.join(med, on="ENC_HN", how="full", coalesce=True)
        .select(
            "ENC_HN",
            pl.min_horizontal("lookback_dx_date", "lookback_med_date").alias("lookback_first_date"),
        )
    )


def build_cohort1(
    full: pl.DataFrame,
    demographics: pl.DataFrame,
    prior_cohort1_path: Path = PRIOR_COHORT1,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, int]]:
    """Exclude subjects younger than 18 at index date from the full file.

    Date of birth comes from the previous cohort 1 first, then from the governed
    demographic batch for newly appended subjects. Subjects with no date of
    birth from either source cannot be age-checked and are excluded.

    HT Date (All Period) is kept from the previous cohort 1. For new subjects it
    is the earlier of the study-period date and the 2005-2009 lookback HT date.
    """

    prior = pl.read_parquet(prior_cohort1_path).select(
        pl.col("ENC_HN").cast(pl.String),
        pl.col("HT Date (All Period)").cast(pl.Date).alias("prior_all_period"),
        pl.col("dob").cast(pl.Date).alias("prior_dob"),
    )
    new_dob = demographics.select(
        pl.col("ENC_HN"), pl.col("date_of_birth").alias("new_dob")
    )
    new_ids = (
        full.select(pl.col("ENC_HN").cast(pl.String))
        .unique()
        .join(prior.select("ENC_HN"), on="ENC_HN", how="anti")
        .get_column("ENC_HN")
    )
    lookback_first = lookback_first_ht_dates(new_ids)
    evaluated = (
        full.select(
            pl.col("ENC_HN"),
            pl.col("D001KEY").str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias("study_date"),
        )
        .join(prior, on="ENC_HN", how="left")
        .join(new_dob, on="ENC_HN", how="left")
        .join(lookback_first, on="ENC_HN", how="left")
        .with_columns(
            pl.coalesce("prior_dob", "new_dob").alias("dob"),
            pl.when(pl.col("prior_dob").is_not_null())
            .then(pl.lit("previous cohort 1"))
            .when(pl.col("new_dob").is_not_null())
            .then(pl.lit("governed demographic batch"))
            .otherwise(pl.lit("none"))
            .alias("dob_source"),
            pl.coalesce(
                "prior_all_period",
                pl.min_horizontal("study_date", "lookback_first_date"),
            ).alias("all_period_date"),
        )
        .with_columns(
            ((pl.col("study_date") - pl.col("dob")).dt.total_days() / 365.25).alias("age_at_index")
        )
        .with_columns(
            pl.when(pl.col("dob").is_null())
            .then(pl.lit("missing date of birth"))
            .when(pl.col("age_at_index") < 18)
            .then(pl.lit("age below 18"))
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias("exclusion_reason")
        )
    )
    cohort1 = (
        evaluated.filter(pl.col("exclusion_reason").is_null())
        .select(
            pl.col("ENC_HN"),
            pl.col("study_date").cast(pl.Datetime("ns")).alias("HT Date (Study Period)"),
            pl.col("all_period_date").cast(pl.Datetime("ns")).alias("HT Date (All Period)"),
            pl.col("dob").cast(pl.Datetime("ns")).alias("dob"),
        )
        .sort("ENC_HN")
    )
    reason = pl.col("exclusion_reason")
    stats = {
        "full": evaluated.height,
        "dob_from_previous_cohort1": evaluated.filter(pl.col("dob_source") == "previous cohort 1").height,
        "dob_from_demographic_batch": evaluated.filter(pl.col("dob_source") == "governed demographic batch").height,
        "excluded_missing_dob": evaluated.filter(reason == "missing date of birth").height,
        "excluded_age_below_18": evaluated.filter(reason == "age below 18").height,
        "cohort1": cohort1.height,
        "prior_cohort1": prior.height,
        "cohort1_growth": cohort1.height - prior.height,
    }
    return cohort1, evaluated, stats


def build_append_lines(append_stats: dict[str, int], cohort1_stats: dict[str, int]) -> list[str]:
    a, c = append_stats, cohort1_stats

    def row(label: str, value: int | str) -> str:
        text = f"{value:,}" if isinstance(value, int) else str(value)
        return f"{label:<52}{text:>10}"

    return [
        "Append to full cohort file",
        "--------------------------",
        row(f"Previous full file ({KNOWN_COHORT.name})", a["previous_full"]),
        row("New HT subjects from this chunk (incl. age < 18)", a["new_ht_subjects"]),
        row("   Already present in previous file (dropped)", a["already_in_previous_full"]),
        row("   Appended", a["appended"]),
        row(f"Full file {FULL_STEM} (.csv, .parquet.gzip)", a["full"])
        + f"  [unique ENC_HN: {a['full_unique_hn']:,}]",
        "Notes: new rows use normalized ICD codes (no dots/dashes) and medication codes as delivered, null instead of 'None'/'NaT',",
        "       flag icd/med/both = criteria met on the index date.",
        "",
        "Cohort 1 (full file, age >= 18 at index date)",
        "---------------------------------------------",
        row("Full file subjects", c["full"]),
        row("   DOB from previous cohort 1", c["dob_from_previous_cohort1"]),
        row("   DOB from governed demographic batch", c["dob_from_demographic_batch"]),
        row("   Excluded, missing date of birth", c["excluded_missing_dob"]),
        row("   Excluded, age below 18 at index", c["excluded_age_below_18"]),
        row(f"Cohort 1 ({COHORT1_OUT.relative_to(PROJECT_ROOT)})", c["cohort1"]),
        row("   Previous cohort 1", c["prior_cohort1"]),
        row("   Growth", f"{c['cohort1_growth']:+,}"),
        "Notes: HT Date (All Period) kept from previous cohort 1; for new subjects it is",
        "       min(index date, 2005-2009 lookback HT date). Subjects absent from previous",
        "       cohort 1 and from the demographic batch have no DOB and stay excluded.",
    ]


def save_pl_dataset(frame: pl.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output_path)


def write_outputs(results: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_names = {
        "dx_working": "dx_working.parquet",
        "dx_summary": "dx_summary.parquet",
        "med_working": "med_working.parquet",
        "med_summary": "med_summary.parquet",
        "exact_criteria_base": "exact_criteria_base.parquet",
        "excluded_alternative_indications": "excluded_alternative_indications.parquet",
        "hypertension_index": "hypertension_index.parquet",
        "counts": "exact_criteria_counts.parquet",
    }
    for name, filename in output_names.items():
        save_pl_dataset(results[name], DATA_DIR / filename)

    FULL_DIR.mkdir(parents=True, exist_ok=True)
    results["full_cohort"].write_csv(FULL_DIR / f"{FULL_STEM}.csv")
    results["full_cohort"].write_parquet(FULL_DIR / f"{FULL_STEM}.parquet.gzip", compression="gzip")
    save_pl_dataset(results["cohort1"], COHORT1_OUT)
    save_pl_dataset(results["cohort1_evaluated"], COHORT1_OUT.parent / "cohort1_evaluated.parquet.gzip")
    full_outputs = [
        str((FULL_DIR / f"{FULL_STEM}.csv").relative_to(PROJECT_ROOT)),
        str((FULL_DIR / f"{FULL_STEM}.parquet.gzip").relative_to(PROJECT_ROOT)),
        str(COHORT1_OUT.relative_to(PROJECT_ROOT)),
        str((COHORT1_OUT.parent / "cohort1_evaluated.parquet.gzip").relative_to(PROJECT_ROOT)),
    ]

    count_lines = [f"{metric}: {count:,}" for metric, count in results["counts"].iter_rows()]
    report = [
        "HYPERTENSION STRICT COHORT OUTPUT",
        "==================================",
        f"Governed second batch: {STRICT_ROOT}",
        f"Observation period: {START_DATE} through {END_DATE}, inclusive",
        "Loose-stage files and outputs were not read by this script.",
        "Medication-only evidence: exclude mapped alternative indications.",
        "Adult eligibility: age at index >= 18 years.",
        "",
        *build_flow_lines(results),
        "",
        "Exact-criteria counts (exact_criteria_counts.parquet)",
        "-----------------------------------------------------",
        *count_lines,
        "",
        *build_append_lines(results["append_stats"], results["cohort1_stats"]),
        "",
        "Output files",
        "------------",
        *output_names.values(),
        *full_outputs,
    ]
    (DATA_DIR / "strict_output.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    require(
        [DIAGNOSIS_PATH, ORDER_PATH, DEMOGRAPHIC_PATH, KNOWN_COHORT, PRIOR_COHORT1],
        "Strict stage",
    )
    prefixes = load_diagnosis_prefixes()
    # Rows without a patient ID cannot belong to a subject; grouping them would
    # create one fake subject.
    dx_working = read_diagnosis_working_df(prefixes=prefixes).filter(pl.col("ENC_HN").is_not_null())
    dx_summary = build_diagnosis_summary(dx_working)
    med_working = read_medication_working_df().filter(pl.col("ENC_HN").is_not_null())
    med_summary = build_medication_summary(med_working)
    exact_base = combine_evidence(dx_summary, med_summary)

    indication_map = load_other_indications()
    medication_only = exact_base.filter(pl.col("evidence_group") == "medication_only")
    diagnosis_history = read_relevant_diagnosis_history(
        DIAGNOSIS_PATH,
        medication_only,
        indication_map,
    )
    demographics = read_demographics(
        DEMOGRAPHIC_PATH,
        exact_base.get_column("ENC_HN"),
    )
    exclusions = match_alternative_indications(
        medication_only,
        diagnosis_history,
        demographics,
        indication_map,
    )
    evaluated, hypertension_index = apply_exact_criteria(
        exact_base,
        exclusions,
        demographics,
    )
    counts = build_exact_counts(evaluated, hypertension_index)
    flags = index_day_flag(evaluated, dx_working, med_working)
    full_cohort, append_stats = append_full_cohort(build_full_cohort_rows(evaluated, flags))
    cohort1, cohort1_evaluated, cohort1_stats = build_cohort1(full_cohort, demographics)
    results = {
        "full_cohort": full_cohort,
        "append_stats": append_stats,
        "cohort1": cohort1,
        "cohort1_evaluated": cohort1_evaluated,
        "cohort1_stats": cohort1_stats,
        "dx_working": dx_working,
        "dx_summary": dx_summary,
        "med_working": med_working,
        "med_summary": med_summary,
        "exact_criteria_base": evaluated,
        "excluded_alternative_indications": exclusions,
        "hypertension_index": hypertension_index,
        "counts": counts,
    }
    write_outputs(results)
    print(counts)
    summarize("strict")         # strict/strict_summary.txt (shared format) + Cohort 1 stability check
    write_flow("strict", flow_values(results))  # strict/flowchart_numbers_strict.txt (up to Cohort 1)


if __name__ == "__main__":
    main()
