"""Hotspot cleaning and space-time clustering of VIIRS detections into fires."""

from __future__ import annotations

import json
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sklearn.neighbors import BallTree

from . import config as C

EARTH_RADIUS_KM = 6371.0088
_SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "el", "en"}


def nice_name(s: str) -> str:
    """'CODO DEL POZUZO' -> 'Codo del Pozuzo'."""
    words = re.split(r"(\s+|-)", str(s).strip().lower())
    out = []
    for i, w in enumerate(words):
        if w.strip() and w not in ("-",) and (i == 0 or w not in _SMALL_WORDS):
            w = w[:1].upper() + w[1:]
        out.append(w)
    return "".join(out)


def load_districts() -> gpd.GeoDataFrame:
    shp = next((C.RAW / "districts").rglob("DISTRITOS.shp"))
    g = gpd.read_file(shp)
    g = g.rename(columns={"IDDIST": "id", "DISTRITO": "district_raw",
                          "PROVINCIA": "province_raw", "DEPARTAMEN": "department_raw"})
    g["name"] = g["district_raw"].map(nice_name)
    g["province"] = g["province_raw"].map(nice_name)
    g["department"] = g["department_raw"].map(nice_name)
    g["geometry"] = g.geometry.make_valid()
    g = g[["id", "name", "province", "department", "geometry"]].set_crs(C.CRS_GEO, allow_override=True)
    return g


def load_meta() -> dict:
    return json.loads((C.RAW / "firms" / "meta.json").read_text(encoding="utf-8"))


def load_raw_hotspots() -> pd.DataFrame:
    df = pd.read_csv(C.RAW / "firms" / "viirs_season.csv", dtype={"acq_time": str})
    return prepare_raw(df)


def prepare_raw(df: pd.DataFrame) -> pd.DataFrame:
    """FIRMS CSV rows (plus a sat_key column) -> the columns used by clean()."""
    df = df.copy()
    df["acq_time"] = df["acq_time"].astype(str).str.zfill(4)
    df["time"] = pd.to_datetime(df["acq_date"] + " " + df["acq_time"], format="%Y-%m-%d %H%M", utc=True)
    df["sat"] = df["sat_key"].map({k: i for i, k in enumerate(C.SATELLITES)}).astype(int)
    df = df.rename(columns={"latitude": "lat", "longitude": "lon"})
    df["confidence"] = df["confidence"].astype(str).str.lower().str[0]
    return df[["lon", "lat", "time", "frp", "sat", "confidence", "daynight", "acq_date"]].reset_index(drop=True)


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def clean(df: pd.DataFrame, districts: gpd.GeoDataFrame) -> tuple[pd.DataFrame, dict]:
    """Clip to Peru, then drop low confidence, volcanoes and persistent static sources."""
    removed = {}
    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=C.CRS_GEO)
    joined = gpd.sjoin(pts, districts[["id", "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    df = df.assign(district_id=joined["id"].values)
    removed["outside_peru"] = int(df["district_id"].isna().sum())
    df = df[df["district_id"].notna()].copy()

    # 1) Low confidence
    m = df["confidence"] == "l"
    removed["low_conf"] = int(m.sum())
    df = df[~m]

    # 2) Volcanoes (thermal anomalies that are not vegetation fires)
    near = np.zeros(len(df), dtype=bool)
    for lat, lon in C.VOLCANOES.values():
        near |= haversine_km(df["lat"].values, df["lon"].values, lat, lon) <= C.VOLCANO_RADIUS_KM
    removed["volcano"] = int(near.sum())
    df = df[~near]

    # 3) Persistent pixels: same ~375 m cell seen on >= N distinct days over >= M days
    df = df.assign(cx=np.floor(df["lon"] / C.PIXEL_DEG).astype(int),
                   cy=np.floor(df["lat"] / C.PIXEL_DEG).astype(int),
                   day=df["time"].dt.floor("D"))
    g = df.groupby(["cx", "cy"])["day"].agg(["nunique", "min", "max"])
    g["span"] = (g["max"] - g["min"]).dt.days
    bad = g[(g["nunique"] >= C.PERSISTENT_MIN_DAYS) & (g["span"] >= C.PERSISTENT_MIN_SPAN_DAYS)].index
    m = pd.MultiIndex.from_frame(df[["cx", "cy"]]).isin(bad)
    removed["persistent"] = int(m.sum())
    removed["persistent_cells"] = int(len(bad))
    df = df[~m].reset_index(drop=True)
    return df, removed


def cluster_fires(df: pd.DataFrame) -> np.ndarray:
    """Link hotspots within CLUSTER_DIST_KM and CLUSTER_HOURS; return component labels."""
    coords = np.radians(df[["lat", "lon"]].values)
    tree = BallTree(coords, metric="haversine")
    neigh = tree.query_radius(coords, r=C.CLUSTER_DIST_KM / EARTH_RADIUS_KM)
    # Hours since epoch; computed from timedeltas so it is independent of the
    # datetime64 resolution (pandas 3 may store "s"/"us" instead of "ns").
    hours = ((df["time"] - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds() / 3600.0).values
    rows, cols = [], []
    for i, nb in enumerate(neigh):
        nb = nb[nb > i]
        if nb.size:
            nb = nb[np.abs(hours[nb] - hours[i]) <= C.CLUSTER_HOURS]
            rows.append(np.full(nb.size, i))
            cols.append(nb)
    n = len(df)
    if rows:
        r, c = np.concatenate(rows), np.concatenate(cols)
    else:
        r = c = np.array([], dtype=int)
    graph = coo_matrix((np.ones(r.size, dtype=np.int8), (r, c)), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    return labels


def build_fires(df: pd.DataFrame, cutoff: pd.Timestamp, districts: gpd.GeoDataFrame) -> pd.DataFrame:
    since = cutoff - pd.Timedelta(hours=C.ACTIVE_WINDOW_HOURS)
    df = df.assign(recent=df["time"] >= since)
    grp = df.groupby("cluster")
    fires = pd.DataFrame({
        "n": grp.size(),
        "n24": grp["recent"].sum(),
        "frp24": df[df["recent"]].groupby("cluster")["frp"].sum(),
        "frp_total": grp["frp"].sum(),
        "lon": grp["lon"].mean(),
        "lat": grp["lat"].mean(),
        "first_seen": grp["time"].min(),
        "last_seen": grp["time"].max(),
        "cells": df.groupby("cluster").apply(lambda x: len(set(zip(x["cx"], x["cy"]))), include_groups=False),
        "district_id": grp["district_id"].agg(lambda s: s.value_counts().index[0]),
    })
    fires["frp24"] = fires["frp24"].fillna(0.0)
    fires["footprint_ha"] = fires["cells"] * C.PIXEL_AREA_HA
    fires["active"] = fires["n24"] > 0
    info = districts.set_index("id")[["name", "province", "department"]]
    fires = fires.join(info, on="district_id")
    fires.index.name = "cluster"
    return fires.reset_index()


def run() -> dict:
    out_h = C.INTERIM / "hotspots_clean.pkl"
    out_f = C.INTERIM / "fires.pkl"
    out_m = C.INTERIM / "hotspots_meta.json"
    meta = load_meta()
    cutoff = pd.Timestamp(meta["cutoff_utc"])
    districts = load_districts()
    raw = load_raw_hotspots()
    df, removed = clean(raw, districts)
    df["cluster"] = cluster_fires(df)
    fires = build_fires(df, cutoff, districts)

    # Stable public fire ids: active fires ranked later by priority; here use cluster ids
    df.to_pickle(out_h)
    fires.to_pickle(out_f)
    stats = {
        "cutoff_utc": meta["cutoff_utc"],
        "cutoff_lima": meta["cutoff_lima"],
        "rows_downloaded": int(len(raw)),
        "removed": removed,
        **counts(df, fires, cutoff),
        "districts": int(len(districts)),
    }
    out_m.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print("[hotspots]", json.dumps(stats))
    return stats


def counts(df: pd.DataFrame, fires: pd.DataFrame, cutoff: pd.Timestamp) -> dict:
    """Hotspot and fire counts at the cut-off (cleaned hotspots, all fires)."""
    since24 = cutoff - pd.Timedelta(hours=24)
    since7 = cutoff - pd.Timedelta(days=7)
    return {
        "hotspots_season": int(len(df)),
        "hotspots_7d": int((df["time"] >= since7).sum()),
        "hotspots_24h": int((df["time"] >= since24).sum()),
        "fires_total": int(len(fires)),
        "fires_active": int(fires["active"].sum()),
        "fires_active_gt100ha": int((fires["active"] & (fires["footprint_ha"] > 100)).sum()),
    }


if __name__ == "__main__":
    run()
