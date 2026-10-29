"""Shared-format summaries for the identification stages, plus the stability check against the previous period.

Called at the end of each stage (one line in its main()):
    cohort_loose.py  -> summarize("loose")    loose/loose_summary.txt
    cohort.py        -> summarize("strict")   strict/strict_summary.txt   (+ Cohort 1 stability)
    cohort2.py       -> summarize("cohort2")  index/cohort2_summary.txt   (+ Cohort 1 / Cohort 2 stability)
Standalone: python ident_summary.py <loose|strict|cohort2>

Stability: an update must not change existing subjects. Compared with data/identification/<PREV_PERIOD>/index:
subjects lost from Cohort 1, index dates that moved, subjects already in Cohort 1 who left or entered Cohort 2
(a partial 2005-2009 lookback delivery would show up here), and new Cohort 1 entrants with an index date before
START_DATE (e.g. date of birth only now available). Any non-zero count is flagged CHECK in the summary and on
the console; the outputs are still written, the decision is the user's.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from update_config import PERIOD, PREV_PERIOD, START_DATE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "code/common"))
from step_summary import write_step_summary  # noqa: E402

IDENT_ROOT = PROJECT_ROOT / "data/identification"
C1_INDEX = "HT Date (Study Period)"


def _index(path: Path, col: str) -> pl.DataFrame:
    return pl.read_parquet(path, columns=["ENC_HN", col]).select("ENC_HN", pl.col(col).cast(pl.Date).alias("idx"))


def stability(ident_dir: Path, prev_dir: Path, with_cohort2: bool) -> tuple[list[str], bool]:
    """Lines for the summary and whether anything needs checking."""
    lines, flag = [f"stability vs {prev_dir.name}:"], False
    new1, old1 = ident_dir / "index/cohort1.parquet.gzip", prev_dir / "index/cohort1.parquet.gzip"
    if not old1.exists() or not new1.exists():
        return lines + [f"  skipped: {old1 if not old1.exists() else new1} not found"], False
    a, b = _index(old1, C1_INDEX), _index(new1, C1_INDEX)
    lost = a.join(b, on="ENC_HN", how="anti").height
    j = a.join(b, on="ENC_HN", suffix="_new")
    moved = j.filter(pl.col("idx") != pl.col("idx_new")).height
    entered = b.join(a, on="ENC_HN", how="anti")
    old_idx = entered.filter(pl.col("idx") < START_DATE).height
    lines += [f"  Cohort 1: previous {a.height:,}, now {b.height:,}; new entrants {entered.height:,}",
              f"  {'CHECK' if lost else 'ok'}: previous Cohort 1 subjects missing now: {lost:,}",
              f"  {'CHECK' if moved else 'ok'}: index date changed for existing subjects: {moved:,}",
              f"  {'CHECK' if old_idx else 'ok'}: new entrants with an index date before {START_DATE}: {old_idx:,}"]
    flag |= bool(lost or moved or old_idx)
    if with_cohort2:
        new2, old2 = ident_dir / "index/cohort2.parquet.gzip", prev_dir / "index/cohort2.parquet.gzip"
        if old2.exists() and new2.exists():
            c_old, c_new = _index(old2, "index_date"), _index(new2, "index_date")
            existing = a.select("ENC_HN")          # subjects already in the previous Cohort 1
            o = existing.join(c_old.select("ENC_HN"), on="ENC_HN", how="semi")
            n = existing.join(c_new.select("ENC_HN"), on="ENC_HN", how="semi")
            left = o.join(n, on="ENC_HN", how="anti").height
            joined = n.join(o, on="ENC_HN", how="anti").height
            moved2 = (c_old.join(c_new, on="ENC_HN", suffix="_new")
                      .filter(pl.col("idx") != pl.col("idx_new")).height)
            lines += [f"  Cohort 2: previous {c_old.height:,}, now {c_new.height:,}",
                      f"  {'CHECK' if left else 'ok'}: existing subjects who left Cohort 2: {left:,}",
                      f"  {'CHECK' if joined else 'ok'}: existing subjects who entered Cohort 2 (were prevalent): "
                      f"{joined:,}  <- a partial 2005-2009 lookback delivery shows up here",
                      f"  {'CHECK' if moved2 else 'ok'}: Cohort 2 index date changed: {moved2:,}"]
            flag |= bool(left or joined or moved2)
    return lines, flag


def summarize(stage: str, period: str = PERIOD, prev_period: str = PREV_PERIOD) -> Path:
    d, prev = IDENT_ROOT / period, IDENT_ROOT / prev_period
    end = period.split("-")[-1]
    notes = [f"identification period {period} (previous {prev_period})"]
    keys, dates = {}, {}
    if stage == "loose":
        outputs = {p.stem.split(".")[0]: p for p in sorted((d / "loose").glob("*.parquet*"))}
        out_txt = d / "loose" / "loose_summary.txt"
    elif stage == "strict":
        outputs = {"full": d / "full" / f"hthn_2010_{end}.parquet.gzip", "cohort1": d / "index/cohort1.parquet.gzip"}
        outputs |= {p.stem.split(".")[0]: p for p in sorted((d / "strict").glob("*.parquet*"))}
        keys = {"full": ["ENC_HN"], "cohort1": ["ENC_HN"]}
        dates = {"cohort1": C1_INDEX}
        out_txt = d / "strict" / "strict_summary.txt"
    elif stage == "cohort2":
        outputs = {"cohort1": d / "index/cohort1.parquet.gzip", "cohort2": d / "index/cohort2.parquet.gzip",
                   "cohort2_evaluated": d / "index/cohort2_evaluated.parquet.gzip",
                   "full_index_all": d / "full" / f"hthn_2010_{end}_index_all.parquet.gzip"}
        keys = {k: ["ENC_HN"] for k in outputs}
        dates = {"cohort1": C1_INDEX, "cohort2": "index_date"}
        out_txt = d / "index" / "cohort2_summary.txt"
    else:
        raise SystemExit(f"unknown stage {stage}: loose | strict | cohort2")
    flag = False
    if stage in ("strict", "cohort2") and prev != d:
        lines, flag = stability(d, prev, with_cohort2=(stage == "cohort2"))
        notes += lines
    path = write_step_summary(f"identification {stage}", outputs, out_txt, keys=keys, date_col=dates, notes=notes)
    print(f"summary: {path}")
    if flag:
        print("WARNING: stability CHECK items in the summary: review before continuing")
    return path


if __name__ == "__main__":
    summarize(sys.argv[1], *(sys.argv[2:4]))
