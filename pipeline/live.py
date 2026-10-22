"""Live monitor run: the snapshot methods with the cut-off at "now" and a rolling window.

    uv run python -m pipeline.run_all --mode live --out build/live

1. Previous history rows are read (deployed site, CI cache copy, current --out).
2. FIRMS is updated incrementally for the last LIVE_WINDOW_DAYS days (live_sources).
3. Cleaning, clustering, urban/fixed-source flags, exposure at 1/2/5/10 km and priority
   run exactly as in the snapshot (hotspots.py, exposure.py). No case study.
4. Everything is written to a temporary directory next to --out, checked by the quality
   gate (checks.py) and only then swapped into place. On any failure the process exits
   non-zero and the previous --out stays as it was.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import checks, config as C, export, exposure, history, hotspots, live_sources

SCHEDULE = {"cron_utc": "30 */3 * * *", "every_hours": 3,
            "note": "Most new VIIRS data arrive after the ~06:30 and ~18:30 UTC passes plus ~3 h of processing."}


def iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def lima(ts: pd.Timestamp) -> str:
    return (ts - pd.Timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S-05:00")  # Peru has no DST


def swap(tmp: Path, out: Path) -> None:
    """Replace `out` with `tmp` (two renames on the same filesystem)."""
    old = out.with_name(f".{out.name}.old-{os.getpid()}")
    if out.exists():
        os.replace(out, old)
    try:
        os.replace(tmp, out)
    except OSError:
        if old.exists() and not out.exists():
            os.replace(old, out)
        raise
    shutil.rmtree(old, ignore_errors=True)


def run(out: Path, key: str) -> dict:
    t0 = pd.Timestamp.now(tz="UTC").floor("s")
    start_day = (t0 - pd.Timedelta(days=C.LIVE_WINDOW_DAYS)).date()
    window_start = pd.Timestamp(start_day, tz="UTC")

    live_sources.ensure_static()
    previous = history.load_previous(out)

    raw, firms_info = live_sources.firms_window(key, start_day, t0.date())
    cutoff = pd.Timestamp.now(tz="UTC").floor("s")
    raw = hotspots.prepare_raw(raw)
    raw = raw[(raw["time"] >= window_start) & (raw["time"] <= cutoff)].reset_index(drop=True)
    data_through = raw["time"].max() if len(raw) else None
    rows_48h = int((raw["time"] >= cutoff - pd.Timedelta(hours=48)).sum())
    problems = checks.firms_recent(rows_48h, data_through.to_pydatetime() if data_through is not None else None,
                                   cutoff.to_pydatetime())
    if problems:
        raise checks.GateError("; ".join(problems))
    print(f"[firms] {len(raw)} detections in the window, {rows_48h} in the last 48 h, "
          f"latest {iso(data_through)}")

    districts = hotspots.load_districts()
    h, removed = hotspots.clean(raw, districts)
    if h.empty:
        raise checks.GateError("no hotspot left in Peru after cleaning")
    h["cluster"] = hotspots.cluster_fires(h)
    fires = hotspots.build_fires(h, cutoff, districts)
    hstats = {"rows_downloaded": int(len(raw)), "removed": removed, **hotspots.counts(h, fires, cutoff),
              "districts": int(len(districts))}
    print("[hotspots]", json.dumps(hstats))

    res = exposure.compute(h, fires, cutoff, districts, fetch=live_sources.fetch_buildings_tiled)
    active, exp = res["active"], res["by_district"]
    print("[exposure]", json.dumps(res["totals"]))
    print("[flagged]", json.dumps(res["flagged"]))

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        exposure.export_buffers(res["zones"], out_dir=tmp, write_meta=False)
        points = export.export_hotspots(h, set(active.loc[~active["urban"], "cluster"]), out_dir=tmp, t0=window_start)
        export.export_series(h, cutoff, out_dir=tmp, start=start_day.isoformat())
        export.export_fires(active, out_dir=tmp)
        topo_kind = export.export_districts(districts, exp, out_dir=tmp)
        table = export.export_district_csv(districts, exp, out_dir=tmp)

        meta = {"cutoff_utc": iso(cutoff), "cutoff_lima": lima(cutoff)}
        summary = export.build_summary(meta, hstats, res["totals"], res["flagged"], active, exp, table, points,
                                       topo_kind, season_start=start_day.isoformat(), case_study=None)
        generated = pd.Timestamp.now(tz="UTC").floor("s")
        summary.update({
            "mode": "live",
            "generated_at": iso(generated),
            "generated_lima": lima(generated),
            "data_through": iso(data_through),
            "window_start": iso(window_start),
            "window_days": C.LIVE_WINDOW_DAYS,
            "firms": {**firms_info, "rows_window": int(len(raw)), "rows_last_48h": rows_48h},
            "schedule": SCHEDULE,
        })
        export.dump(summary, tmp / "summary.json")
        rows = history.append(previous, history.row_from_summary(summary))
        history.write(rows, tmp / "history.csv")

        checks.gate(tmp)
        swap(tmp, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    history.save_state(out / "history.csv")
    return summary


def main(out: Path) -> int:
    out = Path(out).resolve()
    for frozen in (C.WEB, C.OUTPUTS):
        if out == frozen.resolve() or frozen.resolve() in out.parents:
            print(f"Live mode never writes into {frozen.name}/ (the frozen v1.0.0 snapshot). "
                  "Use --out build/live.", file=sys.stderr)
            return 2
    key = live_sources.map_key()
    if not key:
        print("FIRMS_MAP_KEY is not set (environment variable or .env).", file=sys.stderr)
        return 1
    try:
        s = run(out, key)
    except checks.GateError as exc:
        print(f"[live] quality gate failed, {out.name}/ left untouched: {live_sources.redact(exc)}", file=sys.stderr)
        return 1
    except Exception:
        print(f"[live] failed, {out.name}/ left untouched:\n{live_sources.redact(traceback.format_exc())}",
              file=sys.stderr)
        return 1
    e = s["exposure"]
    sizes = {p.name: p.stat().st_size for p in sorted(out.iterdir()) if p.is_file()}
    print(json.dumps({
        "generated_at": s["generated_at"], "data_through": s["data_through"], "window_start": s["window_start"],
        "hotspots_24h": s["hotspots_24h"], "hotspots_7d": s["hotspots_7d"], "fires_active": s["fires_active"],
        "urban_flagged_fires": s["urban_flagged"]["fires"],
        "people": {r: e[r]["people"] for r in e}, "buildings": {r: e[r]["buildings"] for r in e},
        "firms": s["firms"], "bytes": sizes, "total_bytes": sum(sizes.values()),
    }, indent=1))
    return 0
