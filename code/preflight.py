"""Read-only checks before a heavy stage. Nothing is written except the report.

    python code/preflight.py ident    after the governed batch arrives, before cohort.py / cohort2.py

ident    : the delivery folders in update_config.py and their subfolders; the 2005-2009 lookback years; the
           previous period's full file and Cohort 1; how many subjects of the previous full file appear in the
           new demographic table and in the lookback (a partial delivery shows up as a low share).
Report: data/identification/<PERIOD>/preflight_ident.txt.
Exit code 1 if anything is MISSING (warnings do not stop).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "code/identification"), str(ROOT / "code/common")]
import update_config as uc  # noqa: E402

lines: list[str] = []
missing = 0


def ok(msg: str) -> None:
    lines.append(f"ok       {msg}")


def warn(msg: str) -> None:
    lines.append(f"WARNING  {msg}")


def miss(msg: str) -> None:
    global missing
    missing += 1
    lines.append(f"MISSING  {msg}")


def check_dir(path: Path, subdirs: list[str], label: str) -> None:
    if not path.is_dir():
        miss(f"{label}: {path}")
        return
    ok(f"{label}: {path}")
    for s in subdirs:
        (ok if (path / s).is_dir() else miss)(f"  {label}/{s}")


def years_in(folder: Path) -> list[int]:
    ys = set()
    for p in folder.glob("*.parquet*"):
        m = re.search(r"_(\d{4})\d{4}_", p.name)
        if m:
            ys.add(int(m.group(1)))
    return sorted(ys)


def share_present(ids: pl.Series, files: list[Path], id_col: str = "ENC_HN") -> float:
    if not files:
        return 0.0
    seen = pl.concat([pl.scan_parquet(f).select(id_col).unique().collect() for f in files]).unique()
    return pl.DataFrame({id_col: ids}).join(seen, on=id_col, how="semi").height / max(len(ids), 1)


def ident() -> Path:
    lines.append(f"PREFLIGHT ident  period {uc.PERIOD} (previous {uc.PREV_PERIOD})  {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")
    check_dir(uc.LOOSE_FOLLOWUP_ROOT, ["diagnosis", "order"], "loose follow-up")
    check_dir(uc.LOOSE_NEWCASE_ROOT, ["Diagnosis", "Drug (bill)"], "loose new cases")
    check_dir(uc.STRICT_ROOT, ["diagnosis", "order", "demographic"], "strict (governed)")
    check_dir(uc.LOOKBACK_ROOT, ["diagnosis", "bill"], "lookback 2005-2009")
    for sub in ("diagnosis", "bill"):
        ys = years_in(uc.LOOKBACK_ROOT / sub) if (uc.LOOKBACK_ROOT / sub).is_dir() else []
        gap = [y for y in range(2005, 2010) if y not in ys]
        (miss if gap else ok)(f"  lookback {sub} years {ys or 'none'}" + (f"; missing {gap}" if gap else ""))
    prev = ROOT / "data/identification" / uc.PREV_PERIOD
    full = prev / "full" / f"hthn_2010_{uc.PREV_PERIOD.split('-')[-1]}.parquet.gzip"
    for f in (full, prev / "index/cohort1.parquet.gzip", prev / "index/cohort2.parquet.gzip"):
        (ok if f.exists() else miss)(f"previous period file {f.relative_to(ROOT)}")
    if full.exists():
        ids = pl.read_parquet(full, columns=["ENC_HN"])["ENC_HN"].unique()
        demo = sorted((uc.STRICT_ROOT / "demographic").glob("*.parquet*"))
        lb = sorted((uc.LOOKBACK_ROOT / "diagnosis").glob("*.parquet*"))
        for label, files, low in (("new demographic table", demo, 0.95), ("2005-2009 lookback diagnosis", lb, 0.30)):
            if files:
                sh = share_present(ids, files)
                (warn if sh < low else ok)(f"previous full-file subjects present in {label}: {sh:.1%}"
                                           + (f" (expected >= {low:.0%}: partial delivery?)" if sh < low else ""))
    out = ROOT / "data/identification" / uc.PERIOD / "preflight_ident.txt"
    return out


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode != "ident":
        raise SystemExit(__doc__)
    out = ident()
    lines.append("")
    lines.append(f"RESULT: {missing} missing, {sum(l.startswith('WARNING') for l in lines)} warnings")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport: {out}")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
