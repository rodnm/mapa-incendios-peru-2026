"""Quality gate for one live output directory.

The live run writes into a temporary directory and calls gate() on it; only a directory
that passes replaces --out. It can also be run on its own:

    uv run python -m pipeline.checks build/live
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as C


class GateError(RuntimeError):
    """The live output failed the quality gate; nothing was published."""


def _time(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def firms_recent(rows_last_48h: int, data_through: datetime | None, now: datetime) -> list[str]:
    """FIRMS must have returned detections in the last 48 h (checked before computing)."""
    L, fail = C.LIVE_LIMITS, []
    if rows_last_48h < L["rows_last_48h_min"]:
        fail.append(f"FIRMS returned {rows_last_48h} detections in the last 48 h")
    if data_through is None:
        fail.append("FIRMS returned no acquisition time")
    elif (now - data_through).total_seconds() > L["data_age_max_hours"] * 3600:
        fail.append(f"latest FIRMS acquisition {data_through:%Y-%m-%d %H:%M} UTC is older than "
                    f"{L['data_age_max_hours']} h")
    return fail


def _between(fail: list, name: str, value, lo, hi) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
        fail.append(f"{name} = {value!r} is outside [{lo}, {hi}]")


def validate(out_dir: Path) -> list[str]:
    """Every failed check as a readable line; an empty list means the gate passes."""
    out_dir = Path(out_dir)
    fail: list[str] = []
    D: dict = {}
    for name in C.LIVE_FILES_JSON:
        p = out_dir / name
        if not p.is_file():
            fail.append(f"missing {name}")
            continue
        try:
            D[name] = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            fail.append(f"{name} is not valid JSON ({exc})")
    tables: dict[str, list[dict]] = {}
    for name in C.LIVE_FILES_CSV:
        p = out_dir / name
        if not p.is_file():
            fail.append(f"missing {name}")
            continue
        with open(p, encoding="utf-8", newline="") as fh:
            tables[name] = list(csv.DictReader(fh))

    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    if total > C.LIVE_MAX_BYTES:
        fail.append(f"output is {total} bytes, over the {C.LIVE_MAX_BYTES} byte budget")

    L = C.LIVE_LIMITS
    s = D.get("summary.json")
    if isinstance(s, dict):
        if s.get("mode") != "live":
            fail.append(f"summary.mode = {s.get('mode')!r}, expected 'live'")
        gen, thru, start = _time(s.get("generated_at")), _time(s.get("data_through")), _time(s.get("window_start"))
        for k, v in (("generated_at", gen), ("data_through", thru), ("window_start", start)):
            if v is None:
                fail.append(f"summary.{k} is missing or not an ISO UTC time")
        if s.get("window_days") != C.LIVE_WINDOW_DAYS:
            fail.append(f"summary.window_days = {s.get('window_days')!r}")
        if gen:
            fail += firms_recent(int((s.get("firms") or {}).get("rows_last_48h") or 0), thru, gen)
        if gen and start and not start < gen:
            fail.append("window_start is not before generated_at")

        _between(fail, "hotspots_24h", s.get("hotspots_24h"), 0, L["hotspots_24h_max"])
        _between(fail, "hotspots_7d", s.get("hotspots_7d"), s.get("hotspots_24h") or 0, L["hotspots_7d_max"])
        _between(fail, "hotspots_season", s.get("hotspots_season"), max(L["hotspots_window_min"], s.get("hotspots_7d") or 0),
                 L["hotspots_window_max"])
        _between(fail, "fires_active_all", s.get("fires_active_all"), 0, L["fires_active_max"])
        _between(fail, "fires_active", s.get("fires_active"), 0, s.get("fires_active_all") or 0)
        uf = s.get("urban_flagged") or {}
        _between(fail, "urban_flagged.fires", uf.get("fires"), 0, s.get("fires_active_all") or 0)
        if isinstance(uf.get("fires"), int) and isinstance(s.get("fires_active"), int) and \
                uf["fires"] + s["fires_active"] != s.get("fires_active_all"):
            fail.append("fires_active + urban_flagged.fires != fires_active_all")
        e = s.get("exposure") or {}
        prev_p = prev_b = 0
        for r in C.RADII_KM:
            v = e.get(str(r)) or {}
            _between(fail, f"exposure.{r}.people", v.get("people"), prev_p, L["people_max"])
            _between(fail, f"exposure.{r}.buildings", v.get("buildings"), prev_b, 10 ** 8)
            prev_p, prev_b = v.get("people") or 0, v.get("buildings") or 0

        f = D.get("fires.json")
        if not isinstance(f, list):
            fail.append("fires.json is not a list")
        else:
            if len(f) != s.get("fires_active_all"):
                fail.append(f"fires.json has {len(f)} fires, summary says {s.get('fires_active_all')}")
            if sum(1 for x in f if not x.get("urban")) != s.get("fires_active"):
                fail.append("fires.json vegetation fires do not match summary.fires_active")

        hist = tables.get("history.csv")
        if hist is not None:
            if not hist or list(hist[0].keys()) != C.HISTORY_COLUMNS:
                fail.append("history.csv has no rows or an unexpected header")
            elif hist[-1]["generated_at"] != s.get("generated_at"):
                fail.append("the last history.csv row is not this run")
            if len(hist) > C.LIVE_HISTORY_MAX_ROWS:
                fail.append(f"history.csv has {len(hist)} rows (max {C.LIVE_HISTORY_MAX_ROWS})")
    elif "summary.json" in D:
        fail.append("summary.json is not an object")

    H = D.get("hotspots.json")
    if isinstance(H, dict):
        lens = {k: len(H.get(k) or []) for k in ("x", "y", "t", "frp", "s", "f")}
        if len(set(lens.values())) != 1 or not lens["x"]:
            fail.append(f"hotspots.json arrays are empty or of different lengths: {lens}")
        if _time(H.get("t0")) is None:
            fail.append("hotspots.json t0 is not an ISO UTC time")
    topo = D.get("districts.topo.json")
    if isinstance(topo, dict):
        geoms = ((topo.get("objects") or {}).get("districts") or {}).get("geometries") or []
        if topo.get("type") != "Topology" or len(geoms) < L["districts_min"]:
            fail.append(f"districts.topo.json is not a Topology with >= {L['districts_min']} districts")
    series = D.get("series.json")
    if isinstance(series, dict) and (not series.get("dates") or len(series["dates"]) != len(series.get("total") or [])):
        fail.append("series.json dates and totals do not match")
    buffers = D.get("buffers.json")
    if isinstance(buffers, dict) and not all(str(r) in buffers for r in (1, 2, 5)):
        fail.append("buffers.json lacks the 1, 2 or 5 km zone")
    csv_rows = tables.get("exposure_by_district.csv")
    if csv_rows and "people_5km" not in csv_rows[0]:
        fail.append("exposure_by_district.csv lacks people_5km")
    return fail


def gate(out_dir: Path) -> None:
    fail = validate(out_dir)
    if fail:
        raise GateError("; ".join(fail))


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else C.LIVE_DEFAULT_OUT
    problems = validate(target)
    for line in problems:
        print("FAIL", line)
    print("quality gate:", "failed" if problems else "passed", f"({target})")
    sys.exit(1 if problems else 0)
