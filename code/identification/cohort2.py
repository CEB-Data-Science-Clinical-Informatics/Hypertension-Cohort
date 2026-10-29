"""Build Cohort 2 (incident hypertension) from Cohort 1 and the 2005-2009 lookback.

Workflow boundary:
1. ``cohort.py`` builds the full HT file and Cohort 1 (prevalent cases, adults,
   first criterion met inside 2010-01-01 .. END_DATE of update_config.py).
2. This standalone script reads Cohort 1 plus the 2005-2009 lookback batch and
   removes every subject with evidence of hypertension before 2010-01-01.
3. What remains is Cohort 2, treated as incident hypertension.
4. The full HT file gets the same ``index_date_all`` column appended. It is
   written to a copy carrying the ``_index_all`` postfix; the source file is
   never modified.

Lookback evidence is either of:
* a hypertension ICD-10 diagnosis (I10, I11, I12, I13, I15), or
* a billed antihypertensive drug from ``maplist/cohort/medication_map.csv``.

There is no order/medication table before 2010, so drug exposure is taken from
the outpatient/inpatient bill (``SERVICE_ID`` carries the ``ramadrugcode``).

Deliberate rule difference from ``cohort.py``: the alternative-indication check
(``other indications`` sheet) is NOT applied to the lookback window. Any mapped
antihypertensive billed before 2010 counts as prior hypertension. For an
incident cohort a false exclusion is cheaper than a prevalent case leaking in.

This module does not import ``cohort.py`` or ``cohort_loose.py``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import polars as pl


from update_config import END_DATE, LOOKBACK_ROOT, MEDICATION_MAP, PERIOD, read_map, require
from ident_summary import summarize
from flow_figures import write_flow


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Period settings live in update_config.py.
IDENT_DIR = PROJECT_ROOT / "data/identification" / PERIOD

# Lookback batch delivered by data governance alongside the main second batch.
LOOKBACK_DIAGNOSIS_PATH = LOOKBACK_ROOT / "diagnosis"
LOOKBACK_BILL_PATH = LOOKBACK_ROOT / "bill"


FULL_DIR = IDENT_DIR / "full"
FULL_STEM = f"hthn_2010_{PERIOD.split('-')[-1]}"
# The full file is not overwritten. The augmented copy carries this postfix.
FULL_POSTFIX = "_index_all"
FULL_IN = FULL_DIR / f"{FULL_STEM}.parquet.gzip"
FULL_OUT_PARQUET = FULL_DIR / f"{FULL_STEM}{FULL_POSTFIX}.parquet.gzip"
FULL_OUT_CSV = FULL_DIR / f"{FULL_STEM}{FULL_POSTFIX}.csv"

INDEX_DIR = IDENT_DIR / "index"
COHORT1_IN = INDEX_DIR / "cohort1.parquet.gzip"
COHORT2_OUT = INDEX_DIR / "cohort2.parquet.gzip"
COHORT2_EVALUATED_OUT = INDEX_DIR / "cohort2_evaluated.parquet.gzip"
EXCLUDED_OUT = INDEX_DIR / "cohort2_excluded_prevalent.csv"
COUNTS_OUT = INDEX_DIR / "cohort2_counts.parquet"
REPORT_OUT = INDEX_DIR / "cohort2_output.txt"

HT_PREFIXES = ("I10", "I11", "I12", "I13", "I15")

# Inclusive lookback window. Nothing exists before 2005-01-01 in this datalake.
LOOKBACK_START = date(2005, 1, 1)
LOOKBACK_END = date(2009, 12, 31)
# First day of the study period. Evidence on or after this date is not lookback.
STUDY_START = date(2010, 1, 1)

# The lookback diagnosis extract is a merge of several source exports and the
# ICD column is not in the same position in all of them: ``DX_*_encrypted``
# files carry it in D035KEY, ``LOOSELY_DX_*_encoded`` files in D108KEY.
# Both are read and coalesced; reading only D035KEY silently loses cases.
DIAGNOSIS_CODE_COLUMNS = ("D035KEY", "D108KEY")
DIAGNOSIS_DATE_COLUMN = "D001KEY"
BILL_CODE_COLUMN = "SERVICE_ID"
BILL_DATE_COLUMN = "PER_DATE_2"

# Delivery checks for the data-availability log. The lookback batch has been
# delivered truncated before; the report must make that obvious rather than
# quietly producing a Cohort 2 that is too large.
#
# There is deliberately no row-count threshold. The lookback bill is a
# candidate-restricted extract: unlike the study-period bill, which covers
# every subject billed at the hospital, it is cut down to subjects already
# picked up by the data warehouse loose criteria, and possibly to eligible
# records only. A small row or subject count is therefore expected and is not
# evidence of a bad delivery. Only structural defects are flagged: a missing
# year, a missing file, an unreadable schema, an empty source. Whether drug
# coverage is adequate is a judgement call, so the log reports the coverage
# figures and leaves the call to the reader.
EXPECTED_LOOKBACK_YEARS = (2005, 2006, 2007, 2008, 2009)


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


def _date_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    """Parse a date column that may already be a temporal type or a string."""

    if dtype in (pl.Date, pl.Datetime):
        return pl.col(column).cast(pl.Date)
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
            raise FileNotFoundError(f"Lookback directory is not available: {directory}")
        return []
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.lower().endswith((".csv", ".parquet", ".parquet.gzip", ".gzip"))
    )
    if required and not files:
        raise FileNotFoundError(f"No lookback files found in: {directory}")
    return files


def _prefix_match_expr(column: str, prefixes: tuple[str, ...]) -> pl.Expr:
    normalized = [_normalize_code_value(prefix) for prefix in prefixes]
    normalized = [prefix for prefix in normalized if prefix]
    if not normalized:
        return pl.lit(False)
    pattern = r"^(?:" + "|".join(re.escape(prefix) for prefix in normalized) + ")"
    return pl.col(column).str.contains(pattern)


def _empty_evidence(extra: dict[str, pl.DataType] | None = None) -> pl.DataFrame:
    schema: dict[str, pl.DataType] = {
        "ENC_HN": pl.String,
        "EVENT_DATE": pl.Date,
        "CODE": pl.String,
    }
    schema.update(extra or {})
    return pl.DataFrame(schema=schema)


def read_cohort1(path: Path = COHORT1_IN) -> pl.DataFrame:
    """Read Cohort 1 and rename its date columns to the Cohort 2 vocabulary.

    Cohort 1's own ``HT Date (All Period)`` column is deliberately ignored. It
    was derived from ``old_cases_ht.csv`` and the prehash ``data/old_hn/``
    exports, which are legacy and no longer fed into the pipeline. Every
    pre-2010 date is rederived here from the governed lookback batch, so each
    run depends only on what data governance currently delivers.
    """

    cohort1 = pl.read_parquet(path)
    required = {"ENC_HN", "HT Date (Study Period)", "dob"}
    missing = required - set(cohort1.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    return (
        cohort1.select(
            pl.col("ENC_HN").cast(pl.String),
            pl.col("HT Date (Study Period)").cast(pl.Date).alias("index_date"),
            pl.col("dob").cast(pl.Date).alias("dob"),
        )
        .unique("ENC_HN", keep="first")
        .sort("ENC_HN")
    )


def read_full_cohort(path: Path = FULL_IN) -> pl.DataFrame:
    """Read the full HT file untouched, plus its index date parsed as a date.

    The full file is a superset of Cohort 1: it also holds subjects dropped for
    age or missing date of birth. Lookback evidence is read for all of them so
    the augmented copy has no gaps.
    """

    full = pl.read_parquet(path)
    if "ENC_HN" not in full.columns or "D001KEY" not in full.columns:
        raise ValueError(f"{path} must contain ENC_HN and D001KEY")
    return full.with_columns(
        pl.col("ENC_HN").cast(pl.String),
        _date_expr("D001KEY", full.schema["D001KEY"]).alias("_index_date"),
    )


def load_medication_codes(map_path: Path = MEDICATION_MAP) -> pl.DataFrame:
    """Map ``ramadrugcode`` to antihypertensive subclass.

    The ``other indications`` sheet is intentionally not read: the lookback
    window applies no alternative-indication exemption.
    """

    mapping = read_map(map_path, "selected drug classes")
    required = {"ramadrugcode", "subclass"}
    if not required.issubset(mapping.columns):
        raise ValueError(f"{map_path} must contain columns: {sorted(required)}")
    return (
        mapping.select(
            _medication_code_expr("ramadrugcode").alias("CODE"),
            pl.col("subclass").cast(pl.String).str.strip_chars().alias("drug_class"),
        )
        .drop_nulls()
        .filter(pl.col("CODE") != "")
        .unique("CODE", keep="first")
    )


def _raw_row_count(file_path: Path) -> int:
    """Row count of a delivered file, from metadata where possible."""

    try:
        return int(_scan_file(file_path).select(pl.len()).collect().item())
    except Exception:  # noqa: BLE001 - audit must never fail the run
        return -1


def _years_in_filename(file_path: Path) -> set[int]:
    """Years named in a delivered file's name, e.g. ``diag_20050101_20051231``."""

    return {
        int(match)
        for match in re.findall(r"(?:19|20)\d{2}", file_path.name)
        if int(match) in EXPECTED_LOOKBACK_YEARS
    }


def _file_audit_row(
    file_path: Path,
    scanned: pl.DataFrame,
    matched: pl.DataFrame,
    note: str = "",
) -> dict[str, object]:
    """One audit line per delivered file.

    ``scanned`` is the file restricted to cohort subjects and the lookback
    window; ``matched`` is the qualifying HT evidence inside it. Delivery
    completeness is judged on the raw and scanned figures, never on matches:
    a year with no hypertension in it is not the same as a year not delivered.
    """

    return {
        "source": file_path.parent.name,
        "file": file_path.name,
        "raw_rows": _raw_row_count(file_path),
        "scanned_rows": scanned.height,
        "scanned_patients": scanned.get_column("ENC_HN").n_unique() if scanned.height else 0,
        "first_date": scanned.get_column("EVENT_DATE").min() if scanned.height else None,
        "last_date": scanned.get_column("EVENT_DATE").max() if scanned.height else None,
        "matched_rows": matched.height,
        "matched_patients": matched.get_column("ENC_HN").n_unique() if matched.height else 0,
        "filename_years": _years_in_filename(file_path),
        "note": note,
    }


def _unreadable_audit_row(file_path: Path) -> dict[str, object]:
    return {
        "source": file_path.parent.name,
        "file": file_path.name,
        "raw_rows": _raw_row_count(file_path),
        "scanned_rows": 0,
        "scanned_patients": 0,
        "first_date": None,
        "last_date": None,
        "matched_rows": 0,
        "matched_patients": 0,
        "filename_years": _years_in_filename(file_path),
        "note": "unexpected schema, skipped",
    }


def read_lookback_diagnosis(
    diagnosis_dir: Path = LOOKBACK_DIAGNOSIS_PATH,
    cohort_ids: pl.Series | None = None,
    prefixes: tuple[str, ...] = HT_PREFIXES,
) -> tuple[pl.DataFrame, list[dict[str, object]], set[str]]:
    """Read qualifying HT diagnoses from the 2005-2009 lookback batch.

    Also returns the set of requested subjects that appear anywhere in the
    extract, which is how the log judges coverage.
    """

    frames: list[pl.DataFrame] = []
    audit: list[dict[str, object]] = []
    universe: set[str] = set()
    for file_path in _chunk_files(diagnosis_dir):
        lazy = _scan_file(file_path)
        schema = lazy.collect_schema()
        names = set(schema.names())
        if "ENC_HN" not in names or DIAGNOSIS_DATE_COLUMN not in names:
            audit.append(_unreadable_audit_row(file_path))
            continue
        base = lazy.select(
            pl.col("ENC_HN").cast(pl.String),
            _date_expr(DIAGNOSIS_DATE_COLUMN, schema[DIAGNOSIS_DATE_COLUMN]).alias(
                "EVENT_DATE"
            ),
            *[
                _normalize_code_expr(column).alias(column)
                for column in DIAGNOSIS_CODE_COLUMNS
                if column in names
            ],
        )
        if cohort_ids is not None:
            base = base.filter(pl.col("ENC_HN").is_in(cohort_ids.implode()))
        base = base.filter(
            pl.col("EVENT_DATE").is_between(LOOKBACK_START, LOOKBACK_END, closed="both")
        ).collect()
        universe.update(base.get_column("ENC_HN").unique().to_list())

        per_file = [
            base.filter(_prefix_match_expr(column, prefixes)).select(
                "ENC_HN", "EVENT_DATE", pl.col(column).alias("CODE")
            )
            for column in DIAGNOSIS_CODE_COLUMNS
            if column in base.columns
        ]
        matched = (
            pl.concat(per_file, how="vertical_relaxed").unique()
            if per_file
            else _empty_evidence()
        )
        missing_code_columns = [
            column for column in DIAGNOSIS_CODE_COLUMNS if column not in names
        ]
        audit.append(
            _file_audit_row(
                file_path,
                base,
                matched,
                note=(
                    "no " + "/".join(missing_code_columns) + " column"
                    if missing_code_columns
                    else ""
                ),
            )
        )
        if not matched.is_empty():
            frames.append(matched)
    if frames:
        return (
            pl.concat(frames, how="vertical_relaxed").unique().sort("ENC_HN", "EVENT_DATE"),
            audit,
            universe,
        )
    return _empty_evidence(), audit, universe


def read_lookback_medication(
    bill_dir: Path = LOOKBACK_BILL_PATH,
    cohort_ids: pl.Series | None = None,
    map_path: Path = MEDICATION_MAP,
) -> tuple[pl.DataFrame, list[dict[str, object]], set[str]]:
    """Read billed antihypertensives from the 2005-2009 lookback batch.

    Before 2010 there is no order or validated-medication table, so the bill is
    the only drug-exposure source. ``SERVICE_ID`` holds the ``ramadrugcode``.
    """

    mapping = load_medication_codes(map_path)
    frames: list[pl.DataFrame] = []
    audit: list[dict[str, object]] = []
    universe: set[str] = set()
    for file_path in _chunk_files(bill_dir, required=False):
        lazy = _scan_file(file_path)
        schema = lazy.collect_schema()
        names = set(schema.names())
        if not {"ENC_HN", BILL_DATE_COLUMN, BILL_CODE_COLUMN}.issubset(names):
            audit.append(_unreadable_audit_row(file_path))
            continue
        base = lazy.select(
            pl.col("ENC_HN").cast(pl.String),
            _date_expr(BILL_DATE_COLUMN, schema[BILL_DATE_COLUMN]).alias("EVENT_DATE"),
            _medication_code_expr(BILL_CODE_COLUMN).alias("CODE"),
        )
        if cohort_ids is not None:
            base = base.filter(pl.col("ENC_HN").is_in(cohort_ids.implode()))
        base = base.filter(
            pl.col("EVENT_DATE").is_between(LOOKBACK_START, LOOKBACK_END, closed="both")
        ).collect()
        universe.update(base.get_column("ENC_HN").unique().to_list())
        matched = base.join(mapping, on="CODE", how="inner").unique()
        audit.append(_file_audit_row(file_path, base, matched))
        if not matched.is_empty():
            frames.append(matched)
    if frames:
        return (
            pl.concat(frames, how="vertical_relaxed")
            .unique()
            .sort("ENC_HN", "EVENT_DATE"),
            audit,
            universe,
        )
    return _empty_evidence({"drug_class": pl.String}), audit, universe


def summarize_lookback_diagnosis(dx_working: pl.DataFrame) -> pl.DataFrame:
    if dx_working.is_empty():
        return pl.DataFrame(
            schema={
                "ENC_HN": pl.String,
                "lookback_dx_date": pl.Date,
                "lookback_dx_codes": pl.List(pl.String),
            }
        )
    return (
        dx_working.group_by("ENC_HN")
        .agg(
            pl.col("EVENT_DATE").min().alias("lookback_dx_date"),
            pl.col("CODE").unique().sort().alias("lookback_dx_codes"),
        )
        .sort("ENC_HN")
    )


def summarize_lookback_medication(med_working: pl.DataFrame) -> pl.DataFrame:
    if med_working.is_empty():
        return pl.DataFrame(
            schema={
                "ENC_HN": pl.String,
                "lookback_med_date": pl.Date,
                "lookback_med_codes": pl.List(pl.String),
                "lookback_med_classes": pl.List(pl.String),
            }
        )
    return (
        med_working.group_by("ENC_HN")
        .agg(
            pl.col("EVENT_DATE").min().alias("lookback_med_date"),
            pl.col("CODE").unique().sort().alias("lookback_med_codes"),
            pl.col("drug_class").unique().sort().alias("lookback_med_classes"),
        )
        .sort("ENC_HN")
    )


def apply_lookback_criteria(
    cohort1: pl.DataFrame,
    dx_summary: pl.DataFrame,
    med_summary: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Attach lookback evidence, derive ``index_date_all``, split out Cohort 2."""

    evaluated = (
        cohort1.join(dx_summary, on="ENC_HN", how="left")
        .join(med_summary, on="ENC_HN", how="left")
        .with_columns(
            pl.min_horizontal(
                "index_date",
                "lookback_dx_date",
                "lookback_med_date",
            ).alias("index_date_all")
        )
        .with_columns(
            (pl.col("index_date_all") < STUDY_START).alias("prevalent_before_2010"),
            pl.col("lookback_dx_date").is_not_null().alias("has_lookback_dx"),
            pl.col("lookback_med_date").is_not_null().alias("has_lookback_med"),
            (
                (pl.col("index_date_all") - pl.col("dob")).dt.total_days() / 365.25
            ).alias("age_at_index_all"),
        )
        .with_columns(
            pl.when(~pl.col("prevalent_before_2010"))
            .then(pl.lit(None, dtype=pl.String))
            .when(pl.col("has_lookback_dx") & pl.col("has_lookback_med"))
            .then(pl.lit("prior HT diagnosis and antihypertensive bill"))
            .when(pl.col("has_lookback_dx"))
            .then(pl.lit("prior HT diagnosis"))
            .otherwise(pl.lit("prior antihypertensive bill"))
            .alias("exclusion_reason")
        )
        .sort("ENC_HN")
    )
    cohort2 = evaluated.filter(~pl.col("prevalent_before_2010")).select(
        "ENC_HN", "index_date", "index_date_all", "dob"
    )
    return evaluated, cohort2


def build_full_with_index_all(
    full: pl.DataFrame,
    cohort1: pl.DataFrame,
    dx_summary: pl.DataFrame,
    med_summary: pl.DataFrame,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Append ``index_date_all`` to the full HT file, keeping every original column.

    Same derivation as Cohort 2, applied to every subject in the full file
    rather than to Cohort 1 only. The full file also holds subjects dropped
    from Cohort 1 for age or missing date of birth, and they get a pre-2010
    date on exactly the same terms as everyone else.
    """

    augmented = (
        full.join(dx_summary.select("ENC_HN", "lookback_dx_date"), on="ENC_HN", how="left")
        .join(med_summary.select("ENC_HN", "lookback_med_date"), on="ENC_HN", how="left")
        .with_columns(
            pl.min_horizontal(
                "_index_date",
                "lookback_dx_date",
                "lookback_med_date",
            ).alias("index_date_all")
        )
    )
    in_cohort1 = pl.col("ENC_HN").is_in(cohort1.get_column("ENC_HN").implode())
    stats = {
        "full_subjects": augmented.height,
        "in_cohort1": augmented.filter(in_cohort1).height,
        "not_in_cohort1": augmented.filter(~in_cohort1).height,
        "index_date_all_before_2010": augmented.filter(
            pl.col("index_date_all") < STUDY_START
        ).height,
        "index_date_all_before_2010_outside_cohort1": augmented.filter(
            (pl.col("index_date_all") < STUDY_START) & ~in_cohort1
        ).height,
        "index_date_all_equals_index_date": augmented.filter(
            pl.col("index_date_all") == pl.col("_index_date")
        ).height,
        "index_date_all_missing": augmented.filter(
            pl.col("index_date_all").is_null()
        ).height,
    }
    original_columns = [
        name for name in full.columns if name not in ("_index_date", "index_date_all")
    ]
    return augmented.select(*original_columns, "index_date_all"), stats


def build_old_cases(evaluated: pl.DataFrame) -> pl.DataFrame:
    """Audit table of every subject with pre-2010 hypertension evidence."""

    return (
        evaluated.filter(pl.col("prevalent_before_2010"))
        .select(
            "ENC_HN",
            "index_date",
            "index_date_all",
            "lookback_dx_date",
            pl.col("lookback_dx_codes").list.join(","),
            "lookback_med_date",
            pl.col("lookback_med_codes").list.join(","),
            pl.col("lookback_med_classes").list.join(","),
            "exclusion_reason",
        )
        .sort("ENC_HN")
    )


def build_counts(evaluated: pl.DataFrame, cohort2: pl.DataFrame) -> pl.DataFrame:
    dx = pl.col("has_lookback_dx")
    med = pl.col("has_lookback_med")
    excluded = evaluated.filter(pl.col("prevalent_before_2010"))
    rows = [
        ("cohort1_subjects", evaluated.height),
        ("lookback_dx_subjects", evaluated.filter(dx).height),
        ("lookback_med_subjects", evaluated.filter(med).height),
        ("lookback_dx_and_med_subjects", evaluated.filter(dx & med).height),
        ("lookback_dx_only_subjects", evaluated.filter(dx & ~med).height),
        ("lookback_med_only_subjects", evaluated.filter(med & ~dx).height),
        ("lookback_any_evidence_subjects", evaluated.filter(dx | med).height),
        ("excluded_prevalent_before_2010", excluded.height),
        ("cohort2_incident", cohort2.height),
    ]
    return pl.DataFrame(rows, schema=["metric", "count"], orient="row")


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _row(label: str, value: object, note: str = "") -> str:
    line = f"{label:<54}{_fmt(value):>12}"
    return f"{line}  {note}" if note else line


def build_availability_lines(
    dx_audit: list[dict[str, object]],
    med_audit: list[dict[str, object]],
    dx_universe: set[str],
    med_universe: set[str],
    requested: int,
) -> list[str]:
    """Per-file lookback audit, coverage figures, and structural warnings.

    Deliveries are judged on structure only: expected years present, files
    readable, source non-empty. Row and subject counts are reported but never
    thresholded, because the lookback extracts are candidate-restricted and a
    small one can be perfectly correct.
    """

    lines = [
        "Lookback data availability (2005-2009 batch)",
        "-------------------------------------------",
        f"Lookback root: {LOOKBACK_ROOT}",
        "delivered = rows in the file; in cohort = rows for requested subjects",
        "inside the lookback window; matched = qualifying HT evidence.",
        f"{'file':<40}{'delivered':>13}{'in cohort':>12}{'matched':>11}{'subjects':>10}  first..last",
    ]
    warnings: list[str] = []
    for label, audit, universe in (
        ("diagnosis", dx_audit, dx_universe),
        ("bill", med_audit, med_universe),
    ):
        lines.append(f"[{label}]")
        if not audit:
            lines.append("  (no files found)")
            warnings.append(
                f"WARNING: no {label} files in the lookback batch. No exclusion "
                f"can come from {label} evidence."
            )
            continue
        delivered = sum(int(e["raw_rows"]) for e in audit if int(e["raw_rows"]) > 0)
        scanned = sum(int(e["scanned_rows"]) for e in audit)
        matched = sum(int(e["matched_rows"]) for e in audit)
        covered_years: set[int] = set()
        for entry in audit:
            covered_years |= entry["filename_years"]
            for value in (entry["first_date"], entry["last_date"]):
                if isinstance(value, (date, datetime)):
                    covered_years.add(value.year)
            note = entry["note"]
            span = f"{_fmt(entry['first_date'])}..{_fmt(entry['last_date'])}"
            raw = f"{entry['raw_rows']:,}" if int(entry["raw_rows"]) >= 0 else "?"
            lines.append(
                f"  {entry['file']:<38}{raw:>13}{entry['scanned_rows']:>12,}"
                f"{entry['matched_rows']:>11,}{entry['matched_patients']:>10,}  {span}"
                + (f"  [{note}]" if note else "")
            )
        lines.append(f"  {'TOTAL':<38}{delivered:>13,}{scanned:>12,}{matched:>11,}")
        share = 100.0 * len(universe) / requested if requested else 0.0
        lines.append(
            f"  Requested subjects present in this source: {len(universe):,}"
            f" of {requested:,} ({share:.1f}%)"
        )

        missing_years = [
            year for year in EXPECTED_LOOKBACK_YEARS if year not in covered_years
        ]
        if missing_years:
            warnings.append(
                f"WARNING: {label} lookback has no file or record for "
                + ", ".join(str(year) for year in missing_years)
                + f". Expected {EXPECTED_LOOKBACK_YEARS[0]}-{EXPECTED_LOOKBACK_YEARS[-1]}. "
                "Years are missing from the delivery, so Cohort 2 is overstated."
            )
        if delivered == 0:
            warnings.append(
                f"WARNING: {label} lookback delivered 0 rows. No exclusion came "
                f"from {label} evidence."
            )
        unreadable = [e["file"] for e in audit if e["note"] == "unexpected schema, skipped"]
        if unreadable:
            warnings.append(
                f"WARNING: {label} lookback files skipped for unexpected schema: "
                + ", ".join(unreadable)
            )
    lines.extend(
        [
            "",
            "How to read the coverage figures",
            "The study-period bill covers every subject billed at the hospital.",
            "The lookback bill does not: it is cut down to subjects already picked",
            "up by the data warehouse loose criteria, and possibly to eligible",
            "records only. A low subject count or a low percentage above is",
            "therefore expected and is not by itself a bad delivery. Judge the",
            "delivery on the years present and on whether the subject coverage",
            "matches what governance said they were sending.",
        ]
    )
    if warnings:
        lines.extend(["", *warnings])
    else:
        lines.extend(
            ["", "No structural defects found: all expected years present, all files read."]
        )
    return lines


def build_flow_lines(counts: pl.DataFrame) -> list[str]:
    """Flowchart figures: every number needed for the CONSORT-style diagram."""

    c = dict(counts.iter_rows())
    return [
        "Cohort 2 flow (unique ENC_HN)",
        "-----------------------------",
        _row("1. Cohort 1 (prevalent HT, adult, index in study period)", c["cohort1_subjects"]),
        "2. Pre-2010 hypertension evidence in lookback:",
        _row("   HT diagnosis (I10, I11, I12, I13, I15)", c["lookback_dx_subjects"]),
        _row("   Antihypertensive on bill (no other-indication check)", c["lookback_med_subjects"]),
        _row("   Both diagnosis and bill", c["lookback_dx_and_med_subjects"]),
        _row("   Diagnosis only", c["lookback_dx_only_subjects"]),
        _row("   Bill only", c["lookback_med_only_subjects"]),
        _row("   Any lookback evidence", c["lookback_any_evidence_subjects"]),
        _row("3. Excluded, prevalent before 2010-01-01", c["excluded_prevalent_before_2010"]),
        _row("4. Cohort 2, incident hypertension", c["cohort2_incident"]),
    ]


def build_full_file_lines(stats: dict[str, int]) -> list[str]:
    return [
        "Full file with index_date_all",
        "-----------------------------",
        _row(f"Source file ({FULL_IN.name}), unchanged", stats["full_subjects"]),
        _row("   Also in cohort 1", stats["in_cohort1"]),
        _row("   Not in cohort 1 (age < 18 or no date of birth)", stats["not_in_cohort1"]),
        _row("   index_date_all before 2010-01-01 (prevalent)", stats["index_date_all_before_2010"]),
        _row("      of those, outside cohort 1", stats["index_date_all_before_2010_outside_cohort1"]),
        _row("   index_date_all unchanged from D001KEY", stats["index_date_all_equals_index_date"]),
        _row("   index_date_all null (unparsable D001KEY)", stats["index_date_all_missing"]),
        f"Written to {FULL_STEM}{FULL_POSTFIX}.csv and .parquet.gzip.",
        "All original columns are kept in their original order and dtype;",
        "index_date_all is appended as the last column. The source file is not",
        "modified.",
    ]


def build_exclusion_reason_lines(evaluated: pl.DataFrame) -> list[str]:
    reasons = (
        evaluated.filter(pl.col("prevalent_before_2010"))
        .group_by("exclusion_reason")
        .len()
        .sort("len", descending=True)
    )
    lines = ["Exclusion reasons", "-----------------"]
    if reasons.is_empty():
        lines.append("(none)")
        return lines
    lines.extend(_row(reason, count) for reason, count in reasons.iter_rows())
    return lines


def build_drug_class_lines(
    med_working: pl.DataFrame,
    evaluated: pl.DataFrame,
) -> list[str]:
    """Which drug classes drive the bill-only exclusions."""

    lines = ["Bill-only exclusions by drug class", "----------------------------------"]
    bill_only = evaluated.filter(
        pl.col("prevalent_before_2010")
        & pl.col("has_lookback_med")
        & ~pl.col("has_lookback_dx")
    ).select("ENC_HN")
    if med_working.is_empty() or bill_only.is_empty():
        lines.append("(none)")
        return lines
    classes = (
        med_working.join(bill_only, on="ENC_HN", how="inner")
        .select("ENC_HN", "drug_class")
        .unique()
        .group_by("drug_class")
        .len()
        .sort("len", descending=True)
    )
    lines.extend(_row(name, count) for name, count in classes.iter_rows())
    lines.append(
        "Note: a subject may appear under several classes; the column sums above "
        f"{bill_only.height:,} unique bill-only subjects."
    )
    return lines


def build_year_lines(evaluated: pl.DataFrame) -> list[str]:
    """Distribution of the recovered pre-2010 index dates, per year."""

    years = (
        evaluated.filter(pl.col("prevalent_before_2010"))
        .select(pl.col("index_date_all").dt.year().alias("year"))
        .group_by("year")
        .len()
        .sort("year")
    )
    lines = ["Excluded subjects by earliest pre-2010 evidence year", "----------------------------------------------------"]
    if years.is_empty():
        lines.append("(none)")
        return lines
    lines.extend(_row(str(year), count) for year, count in years.iter_rows())
    return lines


def save_pl_dataset(frame: pl.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.name.endswith(".gzip"):
        frame.write_parquet(output_path, compression="gzip")
    else:
        frame.write_parquet(output_path)


def write_outputs(results: dict[str, object]) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    save_pl_dataset(results["cohort2"], COHORT2_OUT)
    save_pl_dataset(results["evaluated"], COHORT2_EVALUATED_OUT)
    save_pl_dataset(results["counts"], COUNTS_OUT)
    results["old_cases"].write_csv(EXCLUDED_OUT)

    FULL_DIR.mkdir(parents=True, exist_ok=True)
    results["full_with_index_all"].write_csv(FULL_OUT_CSV)
    results["full_with_index_all"].write_parquet(FULL_OUT_PARQUET, compression="gzip")

    report = [
        "HYPERTENSION COHORT 2 OUTPUT (INCIDENT CASES)",
        "==============================================",
        f"Cohort 1 input: {COHORT1_IN.relative_to(PROJECT_ROOT)}",
        f"Lookback batch: {LOOKBACK_ROOT}",
        f"Lookback window: {LOOKBACK_START} through {LOOKBACK_END}, inclusive",
        f"Study period starts: {STUDY_START}",
        "Rule: any HT diagnosis (I10, I11, I12, I13, I15) or any mapped",
        "      antihypertensive on the bill before 2010-01-01 marks a prevalent case.",
        "Rule: the other-indication exemption is NOT applied to the lookback window.",
        "      A billed antihypertensive alone is taken as prior hypertension.",
        "Rule: before 2010 there is no order or medication table, so drug exposure",
        "      comes from the bill (SERVICE_ID carries the ramadrugcode).",
        "Rule: the pre-2010 date is rederived from the lookback batch for every",
        "      subject on every run. No legacy old-case file is read.",
        "",
        *build_availability_lines(
            results["dx_audit"],
            results["med_audit"],
            results["dx_universe"],
            results["med_universe"],
            results["requested_subjects"],
        ),
        "",
        *build_flow_lines(results["counts"]),
        "",
        *build_full_file_lines(results["full_stats"]),
        "",
        *build_exclusion_reason_lines(results["evaluated"]),
        "",
        *build_year_lines(results["evaluated"]),
        "",
        *build_drug_class_lines(results["med_working"], results["evaluated"]),
        "",
        "Index dates",
        "-----------",
        f"index_date      first date HT criteria met inside 2010-01-01..{END_DATE}",
        "index_date_all  first date HT criteria met over all available data,",
        "                = min(index_date, lookback diagnosis, lookback bill)",
        "Every pre-2010 date is rederived from the governed lookback batch on each",
        "run. The legacy old_cases_ht.csv and the prehash data/old_hn/ exports are",
        "not read, and cohort 1's own all-period column, which came from them, is",
        "ignored. Counts therefore reflect only what governance currently delivers.",
        "For every Cohort 2 subject the two are equal by construction: lookback",
        "evidence only ever moves the date before 2010, which excludes the subject.",
        "Age eligibility is therefore unchanged from Cohort 1 for all survivors.",
        "",
        "Caveat for the methods section",
        "------------------------------",
        f"The lookback is {LOOKBACK_START.year}-{LOOKBACK_END.year} only; no data exists before",
        f"{LOOKBACK_START}. Subjects first diagnosed before {LOOKBACK_START.year} who did not",
        "re-present during the lookback window remain misclassified as incident.",
        "",
        "Output files",
        "------------",
        str(COHORT2_OUT.relative_to(PROJECT_ROOT)),
        str(COHORT2_EVALUATED_OUT.relative_to(PROJECT_ROOT)),
        str(EXCLUDED_OUT.relative_to(PROJECT_ROOT)),
        str(COUNTS_OUT.relative_to(PROJECT_ROOT)),
        str(FULL_OUT_PARQUET.relative_to(PROJECT_ROOT)),
        str(FULL_OUT_CSV.relative_to(PROJECT_ROOT)),
        str(REPORT_OUT.relative_to(PROJECT_ROOT)),
    ]
    REPORT_OUT.write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> None:
    require([LOOKBACK_DIAGNOSIS_PATH, LOOKBACK_BILL_PATH, FULL_IN, COHORT1_IN], "Cohort 2 stage")
    full = read_full_cohort()
    cohort1 = read_cohort1()
    # The full file is the wider population; scan the lookback once for it.
    scan_ids = (
        pl.concat([full.get_column("ENC_HN"), cohort1.get_column("ENC_HN")])
        .unique()
        .sort()
    )

    dx_working, dx_audit, dx_universe = read_lookback_diagnosis(cohort_ids=scan_ids)
    med_working, med_audit, med_universe = read_lookback_medication(cohort_ids=scan_ids)

    dx_summary = summarize_lookback_diagnosis(dx_working)
    med_summary = summarize_lookback_medication(med_working)

    evaluated, cohort2 = apply_lookback_criteria(cohort1, dx_summary, med_summary)
    counts = build_counts(evaluated, cohort2)
    old_cases = build_old_cases(evaluated)
    full_with_index_all, full_stats = build_full_with_index_all(
        full, cohort1, dx_summary, med_summary
    )

    results = {
        "cohort1": cohort1,
        "full_with_index_all": full_with_index_all,
        "full_stats": full_stats,
        "dx_working": dx_working,
        "med_working": med_working,
        "dx_audit": dx_audit,
        "med_audit": med_audit,
        "dx_universe": dx_universe,
        "med_universe": med_universe,
        "requested_subjects": len(scan_ids),
        "evaluated": evaluated,
        "cohort2": cohort2,
        "old_cases": old_cases,
        "counts": counts,
    }
    write_outputs(results)
    print(counts)
    print(f"\nReport: {REPORT_OUT}")
    summarize("cohort2")        # index/cohort2_summary.txt (shared format) + Cohort 1 / 2 stability check
    write_flow("cohort2")       # strict/flowchart_numbers_strict.txt completed (final)


if __name__ == "__main__":
    main()
