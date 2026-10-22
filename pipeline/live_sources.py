"""Live-mode inputs: incremental FIRMS day cache, Open Buildings tile cache, static files.

* FIRMS: one CSV per (product, UTC day) under data/raw/firms_live/days/. A run downloads
  only the days that are not cached, plus the current day and the LIVE_REFETCH_DAYS
  previous days, because NRT files are revised after publication.
* Open Buildings: one .npz per OB_TILE_DEG grid cell. A zone's bbox is rounded outwards
  to the grid and every cell is queried once; buildings do not change between runs.
* WorldPop and the district shapefile are downloaded once and reused.

Downloaded files are parsed as data, never executed. The FIRMS key only travels inside
request URLs: URLs are never printed and every error message is redacted.
"""

from __future__ import annotations

import io
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely

from . import config as C
from .download import SESSION, _plan_chunks, download_districts, download_worldpop

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_DELAY_S = 180.0
_SECRETS: set[str] = set()


class SourceError(RuntimeError):
    """A remote source failed after its retries (message already redacted)."""


# ----------------------------------------------------------------------------- secrets
def map_key() -> str | None:
    """FIRMS key from the environment (CI secret) or, locally, from .env. Never printed."""
    key = (os.environ.get("FIRMS_MAP_KEY") or "").strip()
    if not key:
        env = C.ROOT / ".env"
        if env.exists():
            from dotenv import dotenv_values
            key = (dotenv_values(env).get("FIRMS_MAP_KEY") or "").strip()
    if key:
        _SECRETS.add(key)
    return key or None


def redact(text: object) -> str:
    text = str(text)
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


# ----------------------------------------------------------------------------- HTTP
def http_get(url: str, label: str, *, timeout: int = 120, attempts: int = 6, base_delay: float = 4.0,
             valid=None, fatal=None) -> requests.Response:
    """GET with exponential backoff and jitter on network errors, 429/5xx and bad bodies.

    `valid(resp)` accepts a 200 body; `fatal(resp)` marks a body that retrying cannot fix
    (for example an invalid key). Retry-After is honoured. `label` is what gets logged.
    """
    last = "no attempt"
    for attempt in range(1, attempts + 1):
        wait = None
        try:
            r = SESSION.get(url, timeout=timeout)
        except requests.RequestException as exc:  # the message may contain the URL
            last = type(exc).__name__
        else:
            if r.status_code == 200 and (valid is None or valid(r)):
                return r
            if fatal is not None and fatal(r):
                raise SourceError(f"{label}: rejected by the server (HTTP {r.status_code}): "
                                  f"{redact(r.text[:120].strip())!r}")
            if r.status_code != 200 and r.status_code not in RETRY_STATUS:
                raise SourceError(f"{label}: HTTP {r.status_code}")
            last = f"HTTP {r.status_code}" if r.status_code != 200 else f"unexpected body {redact(r.text[:60].strip())!r}"
            ra = r.headers.get("Retry-After", "")
            wait = float(ra) if ra.isdigit() else None
        if attempt == attempts:
            break
        delay = min(MAX_DELAY_S, wait if wait is not None else base_delay * 2 ** (attempt - 1) * random.uniform(0.75, 1.25))
        print(f"[http] {label}: {last}; retry {attempt}/{attempts - 1} in {delay:.0f} s")
        time.sleep(delay)
    raise SourceError(f"{label}: {last} after {attempts} attempts")


def with_retries(fn, label: str, attempts: int = 4, base_delay: float = 5.0):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts:
                raise SourceError(f"{label}: {redact(exc)}") from None
            delay = base_delay * 2 ** (attempt - 1) * random.uniform(0.75, 1.25)
            print(f"[retry] {label}: {type(exc).__name__}; retry {attempt}/{attempts - 1} in {delay:.0f} s")
            time.sleep(delay)


# ----------------------------------------------------------------------------- static inputs
def ensure_static() -> None:
    """District shapefile and WorldPop raster: downloaded on the first run only."""
    d = C.RAW / "districts"
    shp = next(d.rglob("DISTRITOS.shp"), None) if d.exists() else None
    if shp is None or not all(shp.with_suffix(ext).exists() for ext in (".dbf", ".shx", ".prj")):
        with_retries(download_districts, "districts (Hugging Face)")
    with_retries(download_worldpop, "WorldPop")  # skips an existing file


# ----------------------------------------------------------------------------- FIRMS
def _is_csv(header: str):
    return lambda r: r.text.lstrip().lower().startswith(header)


def _key_rejected(r: requests.Response) -> bool:
    body = r.text[:200].lower()
    return r.status_code in (400, 401, 403) or ("invalid" in body and "key" in body)


def firms_availability(key: str) -> pd.DataFrame:
    """Date coverage of each FIRMS product. Refreshed every run, cached as a fallback."""
    dest = C.LIVE_FIRMS_DIR / "data_availability.csv"
    try:
        r = http_get(f"{C.FIRMS_BASE}/data_availability/csv/{key}/ALL", "FIRMS availability",
                     timeout=60, valid=_is_csv("data_id"), fatal=_key_rejected)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(r.text, encoding="utf-8")
    except SourceError as exc:
        if not dest.exists() or "rejected" in str(exc):
            raise
        print(f"[firms] {exc}; using the cached availability table")
    return pd.read_csv(dest)


def _day_path(src: str, d: date) -> Path:
    return C.LIVE_FIRMS_DIR / "days" / src / f"{d.isoformat()}.csv"


def _group(days: list[tuple[str, date]]) -> list[tuple[str, date, int]]:
    """Consecutive (source, day) pairs -> (source, first_day, n<=FIRMS_DAY_RANGE) requests."""
    chunks: list[tuple[str, date, int]] = []
    for src, d in sorted(days):
        if chunks and chunks[-1][0] == src and chunks[-1][2] < C.FIRMS_DAY_RANGE and \
                chunks[-1][1] + timedelta(days=chunks[-1][2]) == d:
            chunks[-1] = (src, chunks[-1][1], chunks[-1][2] + 1)
        else:
            chunks.append((src, d, 1))
    return chunks


def _fetch_chunk(key: str, src: str, d0: date, n: int) -> None:
    """Download one area request and store it split into one file per acquisition day."""
    area = ",".join(str(v) for v in C.BBOX)
    url = f"{C.FIRMS_BASE}/area/csv/{key}/{src}/{area}/{n}/{d0.isoformat()}"
    r = http_get(url, f"FIRMS {src} {d0.isoformat()} +{n}d", timeout=180,
                 valid=_is_csv("latitude"), fatal=_key_rejected)
    df = pd.read_csv(io.StringIO(r.text), dtype={"acq_time": str, "version": str})
    for i in range(n):
        d = d0 + timedelta(days=i)
        path = _day_path(src, d)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        df[df["acq_date"] == d.isoformat()].to_csv(tmp, index=False)
        tmp.replace(path)


def firms_window(key: str, start: date, end: date) -> tuple[pd.DataFrame, dict]:
    """VIIRS rows (all satellites) acquired from `start` to `end` (UTC days, inclusive).

    Each day uses the product the snapshot would use (standard processing when available,
    otherwise NRT), so a day moves from NRT to SP automatically once SP covers it.
    """
    avail = firms_availability(key)
    refetch_from = end - timedelta(days=C.LIVE_REFETCH_DAYS)
    plan = {sat: [(src, d0 + timedelta(days=i)) for src, d0, n in _plan_chunks(sat, avail, start, end)
                  for i in range(n)] for sat in C.SATELLITES}
    need = [(src, d) for days in plan.values() for src, d in days
            if d >= refetch_from or not _day_path(src, d).exists()]
    chunks = _group(need)
    print(f"[firms] {sum(len(v) for v in plan.values())} satellite-days in the window, "
          f"{len(need)} to download in {len(chunks)} requests")
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda c: _fetch_chunk(key, *c), chunks))

    frames = []
    for sat, days in plan.items():
        for src, d in days:
            df = pd.read_csv(_day_path(src, d), dtype={"acq_time": str, "version": str})
            if df.empty:
                continue
            frames.append(df.assign(sat_key=sat, source=src))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["latitude", "longitude", "acq_date", "acq_time", "frp", "confidence", "daynight", "sat_key"])
    df["acq_time"] = df["acq_time"].astype(str).str.zfill(4)
    df = df.drop_duplicates(subset=["latitude", "longitude", "acq_date", "acq_time", "sat_key"])
    df = df[df["acq_date"] >= start.isoformat()].reset_index(drop=True)

    # Keep the cache bounded: drop days that left the window more than a week ago.
    oldest = (start - timedelta(days=7)).isoformat()
    for p in (C.LIVE_FIRMS_DIR / "days").glob("*/*.csv"):
        if p.stem < oldest:
            p.unlink(missing_ok=True)

    info = {"requests": len(chunks), "days_downloaded": len(need),
            "days_cached": sum(len(v) for v in plan.values()) - len(need),
            "sources": sorted({src for days in plan.values() for src, _ in days})}
    return df, info


# ----------------------------------------------------------------------------- Open Buildings
def _tile_path(ix: int, iy: int) -> Path:
    return C.OB_TILE_DIR / f"{ix}_{iy}.npz"


def _tile_bounds(ix: int, iy: int) -> tuple[float, float, float, float]:
    t = C.OB_TILE_DEG
    return ix * t, iy * t, (ix + 1) * t, (iy + 1) * t


def tiles_for(zone_ea, pad_deg: float = 0.02) -> list[tuple[int, int]]:
    """Grid cells that intersect the zone (in EPSG:4326, padded like the snapshot boxes)."""
    if zone_ea is None or zone_ea.is_empty:
        return []
    zone = gpd.GeoSeries([zone_ea], crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO).iloc[0].buffer(pad_deg)
    shapely.prepare(zone)
    t = C.OB_TILE_DEG
    out = set()
    for part in shapely.get_parts(zone):
        w, s, e, n = part.bounds
        for ix in range(math.floor(w / t), math.floor(e / t) + 1):
            for iy in range(math.floor(s / t), math.floor(n / t) + 1):
                if (ix, iy) not in out and zone.intersects(shapely.box(*_tile_bounds(ix, iy))):
                    out.add((ix, iy))
    return sorted(out)


def _fetch_tile(ix: int, iy: int, margin: float = 0.01) -> None:
    """Query one grid cell from the VIDA GeoParquet mirror and cache it.

    The query box is the cell plus a margin, and rows are assigned to the cell by their
    bbox centre, so buildings that straddle a cell edge are kept exactly once.
    """
    from .exposure import _vida_query

    w, s, e, n = _tile_bounds(ix, iy)
    df = with_retries(lambda: _vida_query((w - margin, s - margin, e + margin, n + margin)),
                      f"Open Buildings tile {ix},{iy}")
    m = (df["lon"] >= w) & (df["lon"] < e) & (df["lat"] >= s) & (df["lat"] < n)
    df = df[m]
    path = _tile_path(ix, iy)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez_compressed(tmp, lon=df["lon"].to_numpy(np.float64), lat=df["lat"].to_numpy(np.float64),
                        area=df["area_in_meters"].to_numpy(np.float64))
    tmp.replace(path)


def _load_tile(ix: int, iy: int) -> pd.DataFrame:
    with np.load(_tile_path(ix, iy), allow_pickle=False) as z:
        return pd.DataFrame({"lon": z["lon"], "lat": z["lat"], "area_in_meters": z["area"]})


def fetch_buildings_tiled(zone_ea) -> gpd.GeoDataFrame:
    """Drop-in for exposure.fetch_buildings: Open Buildings points inside the zone (EA CRS)."""
    tiles = tiles_for(zone_ea)
    missing = [t for t in tiles if not _tile_path(*t).exists()]
    print(f"[buildings] {len(tiles)} grid cells of {C.OB_TILE_DEG} deg, {len(missing)} to download")
    if missing:
        import duckdb
        con = duckdb.connect()  # install httpfs once before the parallel queries
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
        finally:
            con.close()
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda t: _fetch_tile(*t), missing))
    empty = gpd.GeoDataFrame({"lon": [], "lat": [], "area_in_meters": []},
                             geometry=gpd.points_from_xy([], []), crs=C.CRS_EQUAL_AREA)
    if not tiles:
        return empty
    df = pd.concat([_load_tile(*t) for t in tiles], ignore_index=True)
    df = df.drop_duplicates(subset=["lon", "lat", "area_in_meters"])
    if df.empty:
        return empty
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=C.CRS_GEO).to_crs(C.CRS_EQUAL_AREA)
    zone = gpd.GeoDataFrame(geometry=[zone_ea], crs=C.CRS_EQUAL_AREA)
    inside = gpd.sjoin(g, zone, how="inner", predicate="within")
    return g.loc[inside.index.unique()].reset_index(drop=True)
