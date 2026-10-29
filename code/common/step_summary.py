"""One plain-text summary per pipeline step, the same layout for identification and features.

    write_step_summary(step, outputs, out_txt, keys=..., date_col=..., notes=...)

For every output parquet: path and size; columns with dtypes; key check (rows vs unique keys, PASS/FAIL);
rows, patients, date range; non-null % per column; rows by year of the date column; a few sample rows with
the patient ID masked (SUBJECT_1..n, picked by a fixed hash so the same subjects appear every run).
Everything is computed with lazy scans: no file is loaded whole. The step's own counts go in `notes`.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import polars as pl

SAMPLE_SEED = 20261012


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _table(header: list[str], rows: list[list]) -> list[str]:
    cells = [[_fmt(v) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(header)]
    line = lambda r: "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip()
    return [line(header), line(["-" * w for w in widths]), *[line(r) for r in cells]]


def summarize_parquet(path: Path, keys: list[str] | None = None, date_col: str | None = None,
                      id_col: str = "ENC_HN", n_samples: int = 3) -> list[str]:
    path = Path(path)
    out = [f"== {path.name}", f"   path: {path}"]
    if not path.exists():
        return out + ["   MISSING", ""]
    out.append(f"   size: {path.stat().st_size / 1e6:,.1f} MB")
    lf = pl.scan_parquet(path)
    schema = lf.collect_schema()
    cols = schema.names()
    n = lf.select(pl.len()).collect().item()
    has_id = id_col in cols
    stats = lf.select(
        *([pl.col(id_col).n_unique().alias("_patients")] if has_id else []),
        *[pl.col(c).null_count().alias(f"_null_{c}") for c in cols],
        *([pl.col(date_col).min().alias("_dmin"), pl.col(date_col).max().alias("_dmax")]
          if date_col and date_col in cols else []),
    ).collect().row(0, named=True)
    out.append(f"   rows: {n:,}" + (f"; patients: {stats['_patients']:,}" if has_id else "")
               + (f"; {date_col}: {stats['_dmin']} to {stats['_dmax']}" if date_col and date_col in cols else ""))
    if keys:
        k = [c for c in keys if c in cols]
        n_keys = lf.select(pl.struct(k).n_unique()).collect().item() if k else 0
        out.append(f"   key ({', '.join(k)}): {n_keys:,} unique of {n:,} rows -> {'PASS' if n_keys == n else 'FAIL'}")
    out += ["", "   columns:"]
    out += ["   " + l for l in _table(["column", "dtype", "non-null %"],
                                     [[c, str(schema[c]), round(100 * (1 - stats[f'_null_{c}'] / n), 1) if n else ""]
                                      for c in cols])]
    if date_col and date_col in cols and schema[date_col] in (pl.Date, pl.Datetime):
        by = (lf.group_by(pl.col(date_col).dt.year().cast(pl.String).alias("year")).agg(pl.len().alias("rows"),
              *([pl.col(id_col).n_unique().alias("patients")] if has_id else [])).sort("year").collect())
        out += ["", "   by year:"] + ["   " + l for l in _table(by.columns, [list(r) for r in by.iter_rows()])]
    if n_samples and has_id and n:
        ids = (lf.select(pl.col(id_col).unique()).collect()
                 .with_columns(pl.col(id_col).hash(SAMPLE_SEED).alias("_h")).sort("_h").head(n_samples)[id_col].to_list())
        sample = lf.filter(pl.col(id_col).is_in(ids)).collect()
        sort = [c for c in (id_col, date_col) if c and c in sample.columns]
        sample = sample.sort(sort).group_by(id_col, maintain_order=True).head(3)
        label = {h: f"SUBJECT_{i + 1}" for i, h in enumerate(ids)}
        sample = sample.with_columns(pl.col(id_col).replace_strict(label)).sort(sort)
        show = [c for c in sample.columns if sample[c].null_count() < sample.height][:14]
        hidden = len(sample.columns) - len(show)
        out += ["", f"   sample rows (up to 3 per subject, ID masked; first {len(show)} non-empty columns"
                    + (f", {hidden} more not shown" if hidden else "") + "):"]
        out += ["   " + l for l in _table(show, [list(r) for r in sample.select(show).iter_rows()])]
    return out + [""]


def write_step_summary(step: str, outputs: dict[str, Path], out_txt: Path, *,
                       keys: dict[str, list[str]] | None = None, date_col: dict[str, str] | None = None,
                       id_col: str = "ENC_HN", notes: list[str] | tuple[str, ...] = (), n_samples: int = 3) -> Path:
    """Write the summary of one step; `outputs` maps a label to a parquet path."""
    keys, date_col = keys or {}, date_col or {}
    lines = [f"STEP SUMMARY: {step}", f"written {_dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    if notes:
        lines += ["NOTES"] + [f"- {x}" for x in notes] + [""]
    lines += ["OUTPUTS", ""]
    for label, path in outputs.items():
        lines += [f"[{label}]"] + summarize_parquet(path, keys.get(label), date_col.get(label), id_col, n_samples)
    out_txt = Path(out_txt)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_txt
