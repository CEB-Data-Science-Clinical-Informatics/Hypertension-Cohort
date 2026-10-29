"""Settings for one incremental update. Edit this file, and only this file, per update.

Lines marked EDIT change every update. Everything else stays.

Workflow:
1. Tentative data arrives:  check the STAGE 1 folders below, then run cohort_loose.py
2. Send data/identification/<PERIOD>/loose/loose_new_candidates.parquet to the data team
3. Governed batch arrives:  check the STAGE 2-3 folder below, then run cohort.py, then cohort2.py

Each stage stops before reading anything if an input folder is missing, and lists it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl


# ---------------------------------------------------------------------------
# PERIOD
# ---------------------------------------------------------------------------

# EDIT: the update period, written as "YYYYMM-YYYYMM" (first month - last month).
# Outputs go to data/identification/<PERIOD>/. The full file is named after the
# last month: hthn_2010_<YYYYMM>.
PERIOD = "202607-202612"

# EDIT: the period just finished. Its full file is the known-HT exclusion list,
# and its Cohort 1 supplies date of birth for existing subjects.
# Must match a folder in data/identification/. After this update, it becomes
# "202607-202612".
PREV_PERIOD = "2010-202606"

# EDIT: first and last day of the new chunk, inclusive. Must match PERIOD.
START_DATE = date(2026, 7, 1)
END_DATE = date(2026, 12, 31)


# ---------------------------------------------------------------------------
# DATALAKE FOLDERS  (look in DATALAKE_ROOT when the data arrives)
# ---------------------------------------------------------------------------

DATALAKE_ROOT = Path("/path/to/datalake")  # EDIT: your site's data root

# STAGE 1, tentative data.
# EDIT: follow-up delivery for the chunk. Needs diagnosis/ and order/ inside.
#       Last time: Data/20260630_fu
LOOSE_FOLLOWUP_ROOT = DATALAKE_ROOT / "Data/20261231_fu"
# EDIT: new-case delivery for the chunk. Needs Diagnosis/ and "Drug (bill)"/ inside.
#       Last time: Loosely_criteria_data/newcase_202601(Jan-June2026)
LOOSE_NEWCASE_ROOT = DATALAKE_ROOT / "Loosely_criteria_data/newcase_202607(Jul-Dec2026)"

# STAGES 2-3, governed second batch returned for the loose candidate list.
# EDIT: needs diagnosis/, order/, demographic/ and 2005-2009/ inside.
#       Last time: Data/20260630_fu_nc
STRICT_ROOT = DATALAKE_ROOT / "Data/20261231_fu_nc"
# Usually no edit: the lookback ships inside the governed batch. Needs
# diagnosis/ and bill/ inside. Edit only if governance delivers it elsewhere.
LOOKBACK_ROOT = STRICT_ROOT / "2005-2009"


# ---------------------------------------------------------------------------
# MAPS  (edit only when a new map version is issued)
# ---------------------------------------------------------------------------

# Antihypertensive drug maps: one CSV per table. All stages read them from here.
MEDICATION_MAP = Path(__file__).resolve().parents[2] / "maplist/cohort"
MAP_FILES = {
    "selected drug classes": "medication_map.csv",
    "other indications": "other_indication_map.csv",
}


def read_map(map_dir: Path, table: str) -> pl.DataFrame:
    """Read one drug-map table ("selected drug classes" or "other indications") as text."""

    return pl.read_csv(Path(map_dir) / MAP_FILES[table], infer_schema_length=0)


def require(paths: list[Path], stage: str) -> None:
    """Stop before reading anything if an input folder for this stage is missing."""

    missing = [str(path) for path in paths if not Path(path).exists()]
    if missing:
        raise SystemExit(
            f"{stage}: input not found:\n  "
            + "\n  ".join(missing)
            + "\nCheck the delivery and update code/identification/update_config.py."
        )
