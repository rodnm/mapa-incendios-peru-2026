"""history.csv: one row per successful live run, persisted without committing to the repo.

At the start of a run the previous rows are read from (1) the deployed site,
`<LIVE_BASE_URL>data/history.csv`, (2) the copy kept in data/raw/live_state/ (restored from
the CI cache) and (3) the current --out directory. They are merged by `generated_at`, the
new row is appended and the file is capped at LIVE_HISTORY_MAX_ROWS rows. The state copy
is refreshed only after a run passes the quality gate.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config as C

MAX_REMOTE_BYTES = 2_000_000
INT_COLUMNS = C.HISTORY_COLUMNS[2:]


def base_url() -> str | None:
    """Published site root; LIVE_BASE_URL overrides the default and '' disables it."""
    value = os.environ.get("LIVE_BASE_URL")
    value = C.LIVE_BASE_URL_DEFAULT if value is None else value.strip()
    return value.rstrip("/") + "/" if value else None


def _valid_time(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except (TypeError, ValueError):
        return False


def parse(text: str) -> list[dict]:
    """Rows of a history.csv; rows that do not match the schema are dropped."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != C.HISTORY_COLUMNS:
        return []
    rows = []
    for r in reader:
        if not (_valid_time(r.get("generated_at")) and _valid_time(r.get("data_through"))):
            continue
        try:
            if any(int(r[c]) < 0 for c in INT_COLUMNS):
                continue
        except (TypeError, ValueError):
            continue
        rows.append({c: r[c] for c in C.HISTORY_COLUMNS})
    return rows


def _remote() -> list[dict]:
    root = base_url()
    if not root:
        return []
    from .download import SESSION
    url = root + "data/history.csv"
    try:
        r = SESSION.get(url, timeout=30, stream=True)
        if r.status_code == 404:
            print("[history] no published history yet")
            return []
        r.raise_for_status()
        body = r.raw.read(MAX_REMOTE_BYTES + 1, decode_content=True)
        if len(body) > MAX_REMOTE_BYTES:
            print("[history] published history is larger than expected; ignored")
            return []
        rows = parse(body.decode("utf-8-sig"))
        print(f"[history] {len(rows)} rows from {url}")
        return rows
    except Exception as exc:  # the site may be down or not deployed yet
        print(f"[history] could not read {url}: {type(exc).__name__}")
        return []


def _local(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = parse(path.read_text(encoding="utf-8-sig"))
    print(f"[history] {len(rows)} rows from {path.relative_to(C.ROOT) if C.ROOT in path.parents else path}")
    return rows


def load_previous(out_dir: Path) -> list[dict]:
    merged: dict[str, dict] = {}
    for rows in (_remote(), _local(C.LIVE_STATE_DIR / "history.csv"), _local(out_dir / "history.csv")):
        for r in rows:
            merged[r["generated_at"]] = r
    return [merged[k] for k in sorted(merged)]


def row_from_summary(s: dict) -> dict:
    e = s["exposure"]
    return {
        "generated_at": s["generated_at"], "data_through": s["data_through"],
        "hotspots_24h": s["hotspots_24h"], "hotspots_7d": s["hotspots_7d"],
        "fires_active": s["fires_active"], "urban_flagged_fires": s["urban_flagged"]["fires"],
        "people_1km": e["1"]["people"], "people_2km": e["2"]["people"],
        "people_5km": e["5"]["people"], "people_10km": e["10"]["people"],
        "buildings_5km": e["5"]["buildings"],
    }


def append(rows: list[dict], row: dict, max_rows: int = C.LIVE_HISTORY_MAX_ROWS) -> list[dict]:
    merged = {r["generated_at"]: r for r in rows}
    merged[row["generated_at"]] = {c: str(row[c]) for c in C.HISTORY_COLUMNS}
    return [merged[k] for k in sorted(merged)][-max_rows:]


def write(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=C.HISTORY_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def save_state(published: Path) -> None:
    """Keep a copy of the published history for the CI cache fallback."""
    C.LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = C.LIVE_STATE_DIR / "history.csv.part"
    shutil.copyfile(published, tmp)
    tmp.replace(C.LIVE_STATE_DIR / "history.csv")
