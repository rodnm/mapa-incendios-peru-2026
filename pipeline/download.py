"""Download raw data (cached): FIRMS VIIRS, districts, WorldPop, terrain tiles, Open Buildings.

Every function skips files that already exist, so the pipeline is resumable.
Downloaded files are treated as untrusted data: they are only parsed, never executed.
"""

from __future__ import annotations

import io
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from . import config as C

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "mapa-incendios-peru-2026 (class project)"})


# ----------------------------------------------------------------------------- helpers
def download_file(url: str, dest: Path, timeout: int = 120, retries: int = 3) -> Path:
    """Stream a URL to dest (atomic rename). Skip if dest already exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries):
        try:
            with SESSION.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
            return dest
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    return dest


def get_map_key(wait_minutes: int = 0) -> str | None:
    """Read FIRMS_MAP_KEY from .env, optionally polling for the file to appear."""
    env_path = C.ROOT / ".env"
    deadline = time.time() + wait_minutes * 60
    while True:
        if env_path.exists():
            load_dotenv(env_path, override=True)
            key = (os.getenv("FIRMS_MAP_KEY") or "").strip()
            if key:
                return key
        if time.time() >= deadline:
            return None
        time.sleep(30)


# ----------------------------------------------------------------------------- districts
def download_districts() -> Path:
    """Fetch the course district shapefile (and the simplified GeoJSON) from Hugging Face."""
    from huggingface_hub import hf_hub_download

    local = C.RAW / "districts"
    local.mkdir(parents=True, exist_ok=True)

    def fetch(path: str) -> str:
        return hf_hub_download(C.HF_REPO, path, repo_type="dataset", local_dir=local)

    def fetch_shapefile(base: str) -> str:
        main = fetch(f"{base}.shp")
        for ext in ("dbf", "shx", "prj", "cpg"):
            try:
                fetch(f"{base}.{ext}")
            except Exception:
                pass
        return main

    shp = Path(fetch_shapefile(C.HF_DISTRICTS_BASE))
    try:
        fetch(C.HF_DISTRICTS_SIMPLE)
    except Exception:
        pass
    return shp


# ----------------------------------------------------------------------------- WorldPop
def download_worldpop() -> Path:
    return download_file(C.WORLDPOP_URL, C.RAW / "worldpop" / Path(C.WORLDPOP_URL).name, timeout=600)


# ----------------------------------------------------------------------------- terrain
def terrain_tile_range(zoom: int) -> tuple[range, range]:
    w, s, e, n = C.BBOX

    def lon2x(lon):
        return (lon + 180.0) / 360.0 * 2**zoom

    def lat2y(lat):
        r = math.radians(lat)
        return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * 2**zoom

    xs = range(int(math.floor(lon2x(w))), int(math.floor(lon2x(e))) + 1)
    ys = range(int(math.floor(lat2y(n))), int(math.floor(lat2y(s))) + 1)
    return xs, ys


def download_terrain(zoom: int = C.TERRAIN_ZOOM) -> Path:
    out = C.RAW / "terrain" / str(zoom)
    xs, ys = terrain_tile_range(zoom)
    jobs = [(x, y) for x in xs for y in ys]

    def job(xy):
        x, y = xy
        url = C.TERRAIN_URL.format(z=zoom, x=x, y=y)
        download_file(url, out / f"{x}_{y}.png")

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(job, jobs))
    return out


# ----------------------------------------------------------------------------- FIRMS
def firms_availability(key: str) -> pd.DataFrame:
    dest = C.RAW / "firms" / "data_availability.csv"
    if not dest.exists():
        r = SESSION.get(f"{C.FIRMS_BASE}/data_availability/csv/{key}/ALL", timeout=60)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(r.text, encoding="utf-8")
    return pd.read_csv(dest)


def _plan_chunks(sat: str, avail: pd.DataFrame, start: date, end: date) -> list[tuple[str, date, int]]:
    """Split [start, end] into (source, first_day, day_range<=5) chunks.

    Each day uses the standard-processing (SP) product when available, otherwise NRT.
    Consecutive days with the same source are grouped in blocks of FIRMS_DAY_RANGE.
    """
    rows = {r.data_id: r for r in avail.itertuples()}

    def rng(src):
        r = rows.get(src)
        if r is None:
            return None
        return (date.fromisoformat(str(r.min_date)[:10]), date.fromisoformat(str(r.max_date)[:10]))

    sp, nrt = rng(f"VIIRS_{sat}_SP"), rng(f"VIIRS_{sat}_NRT")
    days = []
    d = start
    while d <= end:
        if sp and sp[0] <= d <= sp[1]:
            days.append((f"VIIRS_{sat}_SP", d))
        elif nrt and nrt[0] <= d <= nrt[1]:
            days.append((f"VIIRS_{sat}_NRT", d))
        elif nrt and d > nrt[1]:
            # Today's data may not be in the availability table yet; NRT covers it.
            days.append((f"VIIRS_{sat}_NRT", d))
        d += timedelta(days=1)

    chunks: list[tuple[str, date, int]] = []
    for src, d in days:
        if chunks and chunks[-1][0] == src and chunks[-1][2] < C.FIRMS_DAY_RANGE and \
                chunks[-1][1] + timedelta(days=chunks[-1][2]) == d:
            chunks[-1] = (src, chunks[-1][1], chunks[-1][2] + 1)
        else:
            chunks.append((src, d, 1))
    return chunks


def download_firms(key: str, today: date | None = None) -> Path:
    """Download VIIRS hotspots for the season, per satellite, in <=5-day chunks.

    FIRMS area API semantics: /{DAY_RANGE}/{DATE} returns acquisitions from DATE to
    DATE + DAY_RANGE - 1 (verified empirically by checking acq_date in the response).
    Chunks are contiguous and non-overlapping; rows are deduplicated afterwards anyway.
    """
    out_dir = C.RAW / "firms"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "meta.json"
    merged = out_dir / "viirs_season.csv"
    if merged.exists() and meta_path.exists():
        return merged

    now = datetime.now(timezone.utc)
    today = today or now.date()
    start = date.fromisoformat(C.SEASON_START)
    avail = firms_availability(key)
    area = ",".join(str(v) for v in C.BBOX)

    tasks = []
    for sat in C.SATELLITES:
        for src, d0, n in _plan_chunks(sat, avail, start, today):
            tasks.append((sat, src, d0, n))

    def fetch(task):
        sat, src, d0, n = task
        dest = out_dir / "chunks" / f"{src}_{d0.isoformat()}_{n}.csv"
        is_recent = d0 + timedelta(days=n - 1) >= today - timedelta(days=1)
        if dest.exists() and not is_recent:
            return dest
        url = f"{C.FIRMS_BASE}/area/csv/{key}/{src}/{area}/{n}/{d0.isoformat()}"
        for attempt in range(4):
            try:
                r = SESSION.get(url, timeout=180)
                r.raise_for_status()
                text = r.text
                if not text.lstrip().lower().startswith("latitude"):
                    raise RuntimeError(f"Unexpected FIRMS response for {src} {d0}: {text[:80]!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
                return dest
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))

    with ThreadPoolExecutor(4) as ex:
        paths = list(ex.map(fetch, tasks))
    cutoff = datetime.now(timezone.utc)

    frames = []
    for task, p in zip(tasks, paths):
        df = pd.read_csv(p, dtype={"acq_time": str, "version": str})
        if df.empty:
            continue
        df["sat_key"] = task[0]
        df["source"] = task[1]
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["acq_time"] = df["acq_time"].astype(str).str.zfill(4)
    before = len(df)
    df = df.drop_duplicates(subset=["latitude", "longitude", "acq_date", "acq_time", "sat_key"])
    df = df[df["acq_date"] >= C.SEASON_START]
    df.to_csv(merged, index=False)

    lima = cutoff - timedelta(hours=5)
    meta = {
        "cutoff_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cutoff_lima": lima.strftime("%Y-%m-%dT%H:%M:%S-05:00"),
        "n_calls": len(tasks),
        "rows_raw": before,
        "rows_dedup": len(df),
        "sources": sorted({t[1] for t in tasks}),
    }
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return merged


# ----------------------------------------------------------------------------- Open Buildings
def open_buildings_tiles() -> "gpd.GeoDataFrame":
    import geopandas as gpd

    dest = download_file(C.OB_TILES_URL, C.RAW / "open_buildings" / "tiles.geojson")
    tiles = gpd.read_file(dest)
    w, s, e, n = C.BBOX
    return tiles.cx[w:e, s:n].copy()


def remote_size(url: str) -> int | None:
    try:
        r = SESSION.head(url, timeout=30, allow_redirects=True)
        if r.ok and "Content-Length" in r.headers:
            return int(r.headers["Content-Length"])
    except Exception:
        pass
    return None


def download_open_buildings_tile(tile_id: str, kind: str = "points") -> Path:
    tmpl = C.OB_POINTS_URL if kind == "points" else C.OB_POLYGONS_URL
    url = tmpl.format(tile=tile_id)
    return download_file(url, C.RAW / "open_buildings" / kind / f"{tile_id}_buildings.csv.gz", timeout=900)


if __name__ == "__main__":
    download_districts()
    download_worldpop()
    download_terrain()
