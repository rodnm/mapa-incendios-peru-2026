"""Exposure of people (WorldPop) and buildings (Open Buildings) around active fires."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from rasterstats import zonal_stats
from scipy.spatial import cKDTree

from . import config as C
from .hotspots import load_districts, load_meta
from .rasters import worldpop_1km

WORLDPOP_TIF = C.RAW / "worldpop" / Path(C.WORLDPOP_URL).name


# ----------------------------------------------------------------------------- inputs
def load_inputs():
    h = pd.read_pickle(C.INTERIM / "hotspots_clean.pkl")
    fires = pd.read_pickle(C.INTERIM / "fires.pkl")
    cutoff = pd.Timestamp(load_meta()["cutoff_utc"])
    return h, fires, cutoff


def front_points(h: pd.DataFrame, fires: pd.DataFrame, cutoff: pd.Timestamp) -> gpd.GeoDataFrame:
    """Hotspots of active fires within EXPOSURE_WINDOW_HOURS, in the equal-area CRS."""
    act = set(fires.loc[fires["active"], "cluster"])
    since = cutoff - pd.Timedelta(hours=C.EXPOSURE_WINDOW_HOURS)
    p = h[h["cluster"].isin(act) & (h["time"] >= since)].copy()
    g = gpd.GeoDataFrame(p, geometry=gpd.points_from_xy(p.lon, p.lat), crs=C.CRS_GEO)
    return g.to_crs(C.CRS_EQUAL_AREA)


def people_sum(geoms_ea: list) -> np.ndarray:
    """WorldPop 2026 (100 m) sum inside each equal-area geometry (cell-centre rule)."""
    if not geoms_ea:
        return np.array([])
    g = gpd.GeoSeries(geoms_ea, crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO)
    zs = zonal_stats(list(g), str(WORLDPOP_TIF), stats=["sum"], nodata=-99999, all_touched=False)
    return np.array([z["sum"] or 0.0 for z in zs], dtype=float)


# ----------------------------------------------------------------------------- buildings
def _merge_boxes(boxes: list[tuple], pad: float = 0.0) -> list[tuple]:
    """Merge overlapping (w, s, e, n) boxes until none overlap."""
    boxes = [tuple(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out: list[list[float]] = []
        for b in boxes:
            for o in out:
                if not (b[0] > o[2] + pad or b[2] < o[0] - pad or b[1] > o[3] + pad or b[3] < o[1] - pad):
                    o[0], o[1], o[2], o[3] = min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])
                    changed = True
                    break
            else:
                out.append(list(b))
        boxes = [tuple(o) for o in out]
    return boxes


def _vida_query(box: tuple, with_geometry: bool = False) -> pd.DataFrame:
    w, s, e, n = box
    files = "[" + ",".join(f"'{f}'" for f in C.OB_VIDA_FILES) + "]"
    geom = ", geometry" if with_geometry else ""
    q = f"""
        SELECT (bbox.xmin + bbox.xmax) / 2 AS lon, (bbox.ymin + bbox.ymax) / 2 AS lat,
               confidence, area_in_meters {geom}
        FROM read_parquet({files})
        WHERE bbox.xmin >= {w} AND bbox.xmax <= {e} AND bbox.ymin >= {s} AND bbox.ymax <= {n}
          AND bf_source = 'google' AND confidence >= {C.OB_MIN_CONFIDENCE}
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false;")
    try:
        return con.execute(q).df()
    finally:
        con.close()


def fetch_buildings(zone_ea) -> gpd.GeoDataFrame:
    """Open Buildings (Google, confidence >= 0.75) inside the 10 km exposure zone."""
    cache = C.INTERIM / "buildings_10km.pkl"
    if cache.exists():
        df = pd.read_pickle(cache)
    else:
        parts = gpd.GeoSeries(list(shapely.get_parts(zone_ea)), crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO)
        boxes = _merge_boxes([tuple(b) for b in parts.bounds.values], pad=0.02)
        print(f"[buildings] querying {len(boxes)} boxes from the VIDA GeoParquet mirror")
        with ThreadPoolExecutor(6) as ex:
            frames = list(ex.map(_vida_query, boxes))
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["lon", "lat", "area_in_meters"])
        df.to_pickle(cache)
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=C.CRS_GEO).to_crs(C.CRS_EQUAL_AREA)
    zone = gpd.GeoDataFrame(geometry=[zone_ea], crs=C.CRS_EQUAL_AREA)
    inside = gpd.sjoin(g, zone, how="inner", predicate="within")
    return g.loc[inside.index.unique()].reset_index(drop=True)


def count_in(points: gpd.GeoDataFrame, geoms_ea: list) -> np.ndarray:
    if not geoms_ea or points.empty:
        return np.zeros(len(geoms_ea), dtype=int)
    polys = gpd.GeoDataFrame({"k": range(len(geoms_ea))}, geometry=geoms_ea, crs=C.CRS_EQUAL_AREA)
    j = gpd.sjoin(points[["geometry"]], polys, how="inner", predicate="within")
    counts = j.groupby("k").size()
    return counts.reindex(range(len(geoms_ea)), fill_value=0).values


# ----------------------------------------------------------------------------- main
def flag_fires(active: pd.DataFrame, h: pd.DataFrame) -> pd.DataFrame:
    """Add urban / fixed-source flags to active fires (see config for the rules)."""
    from .hotspots import haversine_km

    pop1k, t1k = worldpop_1km()
    lats = t1k.f + (np.arange(pop1k.shape[0]) + 0.5) * t1k.e
    dens = pop1k / ((abs(t1k.a) * 111.32) * (abs(t1k.e) * 111.32) * np.cos(np.radians(lats)))[:, None]
    inv = ~t1k
    max_d, recurrent = [], []
    for r in active.itertuples():
        mine = h[h["cluster"] == r.cluster]
        cols, rows = inv * (mine["lon"].values, mine["lat"].values)
        rr = np.clip(rows.astype(int), 0, dens.shape[0] - 1)
        cc = np.clip(cols.astype(int), 0, dens.shape[1] - 1)
        max_d.append(float(dens[rr, cc].max()))
        old = h[(h["cluster"] != r.cluster) & (h["time"] <= r.first_seen - pd.Timedelta(days=C.FIXED_MIN_GAP_DAYS))]
        near = np.zeros(len(old), dtype=bool)
        for la, lo in zip(mine["lat"].values, mine["lon"].values):
            near |= haversine_km(old["lat"].values, old["lon"].values, la, lo) <= C.FIXED_RADIUS_KM
        recurrent.append(int(old.loc[near, "day"].nunique()))
    active = active.assign(hotspot_density=max_d, recurrent_days=recurrent)
    urban = (active["people_1km"] >= C.URBAN_PEOPLE_1KM) | (active["hotspot_density"] >= C.URBAN_DENSITY_KM2)
    fixed = (active["n"] <= C.FIXED_MAX_HOTSPOTS) & (active["recurrent_days"] >= 1)
    active["flag_reason"] = np.where(urban, "urban", np.where(fixed, "fixed", ""))
    active["urban"] = active["flag_reason"] != ""
    return active


def export_buffers(zones: dict, tol_m: float = 75.0, max_bytes: int = 600_000,
                   out_dir: Path | None = None, write_meta: bool = True) -> dict:
    """Write the dissolved exposure zones (after the urban filter) as GeoJSON geometries."""
    out, dropped = {}, []
    for r in C.RADII_KM:
        g = gpd.GeoSeries([zones[r].simplify(tol_m)], crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO).iloc[0]
        g = shapely.set_precision(g, 0.0001)
        if g.geom_type == "Polygon":
            g = shapely.MultiPolygon([g])
        elif g.is_empty:  # no active vegetation fire: keep the geometry type stable
            g = shapely.MultiPolygon()
        out[str(r)] = json.loads(shapely.to_geojson(g))
    path = (out_dir or C.WEB_DATA) / "buffers.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    if path.stat().st_size > max_bytes:
        dropped.append("10")
        out.pop("10")
        path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    info = {"bytes": path.stat().st_size, "dropped": dropped, "tolerance_m": tol_m}
    if write_meta:
        (C.INTERIM / "buffers_meta.json").write_text(json.dumps(info), encoding="utf-8")
    print("[buffers]", info)
    return info


def run() -> dict:
    """Snapshot step: read the interim hotspots/fires, write the interim exposure files."""
    h, fires, cutoff = load_inputs()
    res = compute(h, fires, cutoff, load_districts())
    export_buffers(res["zones"])
    res["pts_all"].to_pickle(C.INTERIM / "front_points.pkl")
    res["active"].to_pickle(C.INTERIM / "active_fires.pkl")
    res["by_district"].to_pickle(C.INTERIM / "exposure_by_district.pkl")
    (C.INTERIM / "exposure_totals.json").write_text(json.dumps(res["totals"], indent=1), encoding="utf-8")
    (C.INTERIM / "urban_flagged.json").write_text(json.dumps(res["flagged"], indent=1), encoding="utf-8")
    print("[exposure]", json.dumps(res["totals"]))
    print("[flagged]", json.dumps(res["flagged"]))
    return res["totals"]


def compute(h: pd.DataFrame, fires: pd.DataFrame, cutoff: pd.Timestamp, districts: gpd.GeoDataFrame,
            fetch=fetch_buildings) -> dict:
    """Exposure, urban/fixed flags and priority of the active fires, without writing files.

    `fetch(zone_ea)` returns the Open Buildings points inside the 10 km zone; the snapshot
    uses one cached extract, the live monitor a tile cache.
    """
    d_ea = districts.to_crs(C.CRS_EQUAL_AREA)
    pts_all = front_points(h, fires, cutoff)
    active = fires[fires["active"]].copy()

    # Per-fire zones (each fire with its own, undissolved buffer), for all active fires
    fire_zones = {}
    for r in C.RADII_KM:
        bv = pts_all.buffer(r * 1000, quad_segs=16).values
        fire_zones[r] = pd.Series({k: shapely.union_all(bv[idx])
                                   for k, idx in pts_all.groupby("cluster").indices.items()})
    zone_all_10 = shapely.union_all(list(fire_zones[max(C.RADII_KM)].values))
    buildings = fetch(zone_all_10)
    print(f"[buildings] {len(buildings)} buildings (conf >= {C.OB_MIN_CONFIDENCE}) inside the 10 km zone")

    for r in C.RADII_KM:
        fz = fire_zones[r].reindex(active["cluster"].values)
        active[f"people_{r}km"] = people_sum(list(fz.values))
        active[f"buildings_{r}km"] = count_in(buildings, list(fz.values))

    # Urban / fixed-source flags; flagged fires are left out of exposure and priority
    active = flag_fires(active, h)
    keep_ids = set(active.loc[~active["urban"], "cluster"])
    pts = pts_all[pts_all["cluster"].isin(keep_ids)]

    zones = {}
    for r in C.RADII_KM:
        zones[r] = shapely.union_all(pts.buffer(r * 1000, quad_segs=16).values)

    # Per-district exposure (dissolved zones: no double counting)
    by_district = pd.DataFrame(index=districts["id"])
    totals = {}
    for r in C.RADII_KM:
        Z = zones[r]
        cand = d_ea[d_ea.intersects(Z)]
        pieces = list(cand.geometry.intersection(Z).values)
        ppl = people_sum(pieces)
        bld = count_in(buildings, pieces)
        if len(cand):
            by_district.loc[cand["id"].values, f"people_{r}km"] = ppl
            by_district.loc[cand["id"].values, f"buildings_{r}km"] = bld
        else:  # no active vegetation fire
            by_district[f"people_{r}km"] = 0.0
            by_district[f"buildings_{r}km"] = 0.0
        totals[str(r)] = {
            "people": int(round(ppl.sum())),
            "buildings": int(bld.sum()),
            "people_from_buildings": int(round(bld.sum() * C.PERSONS_PER_DWELLING)),
            "districts": int(len(cand)),
            "area_km2": round(Z.area / 1e6, 1),
        }
    by_district = by_district.fillna(0)

    # District context: area, population, hotspot counts
    pop1k, t1k = worldpop_1km()
    zs = zonal_stats(list(districts.geometry), pop1k, affine=t1k, stats=["sum"], nodata=-1)
    by_district["pop"] = [z["sum"] or 0.0 for z in zs]
    by_district["area_km2"] = d_ea.set_index("id").geometry.area.values / 1e6
    since7 = cutoff - pd.Timedelta(days=7)
    h7 = h[h["time"] >= since7]
    by_district["hot7"] = h7.groupby("district_id").size().reindex(by_district.index, fill_value=0)
    by_district["frp7"] = h7.groupby("district_id")["frp"].sum().reindex(by_district.index, fill_value=0)
    by_district["hot_season"] = h.groupby("district_id").size().reindex(by_district.index, fill_value=0)

    # Priority index, only among non-flagged active fires
    def n(x):
        x = np.asarray(x, dtype=float)
        if x.size == 0:
            return x
        span = x.max() - x.min()
        return (x - x.min()) / span if span > 0 else np.zeros_like(x)

    W = C.PRIORITY_WEIGHTS
    ok = ~active["urban"]
    a = active[ok]
    if a.empty:  # no active vegetation fire: no priority to compute
        for col in ("part_people", "part_footprint", "part_frp", "part_activity"):
            active[col] = np.nan
    for col, val in (("part_people", W["people"] * n(np.log1p(a["people_5km"]))),
                     ("part_footprint", W["footprint"] * n(a["footprint_ha"])),
                     ("part_frp", W["frp"] * n(a["frp24"])),
                     ("part_activity", W["activity"] * n(a["n24"]))):
        if not a.empty:
            active.loc[ok, col] = val
    active["priority"] = 100 * (active["part_people"] + active["part_footprint"]
                                + active["part_frp"] + active["part_activity"])
    active = active.sort_values(["urban", "priority"], ascending=[True, False])

    flagged = active[active["urban"]]
    zf = shapely.union_all(pts_all[pts_all["cluster"].isin(set(flagged["cluster"]))].buffer(5000, quad_segs=16).values)
    flagged_people_5km = float(people_sum([zf])[0]) if len(flagged) else 0.0
    totals_flag = {
        "fires": int(len(flagged)),
        "fires_urban": int((flagged["flag_reason"] == "urban").sum()),
        "fires_fixed": int((flagged["flag_reason"] == "fixed").sum()),
        "hotspots_24h": int(flagged["n24"].sum()),
        "people_5km": int(round(flagged_people_5km)),
    }
    return {"pts_all": pts_all, "active": active, "by_district": by_district, "totals": totals,
            "flagged": totals_flag, "zones": zones}


# ----------------------------------------------------------------------------- case study
def case_study(fire_cluster: int) -> dict:
    """Buildings (polygons) within 5 km of one fire, with distance to its nearest hotspot."""
    h, fires, cutoff = load_inputs()
    pts = pd.read_pickle(C.INTERIM / "front_points.pkl")
    fp = pts[pts["cluster"] == fire_cluster]
    rings, zone5 = {}, None
    for r in (1, 2, 5):
        u = shapely.union_all(fp.buffer(r * 1000, quad_segs=24).values)
        if r == 5:
            zone5 = u
        biggest = max(shapely.get_parts(u), key=lambda g: g.area)
        ring = gpd.GeoSeries([shapely.Polygon(biggest.exterior)], crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO)
        rings[str(r)] = [[round(x, 5), round(y, 5)] for x, y in ring.iloc[0].exterior.coords]

    box = gpd.GeoSeries([zone5], crs=C.CRS_EQUAL_AREA).to_crs(C.CRS_GEO).total_bounds
    cache = C.INTERIM / f"case_buildings_{fire_cluster}.pkl"
    if cache.exists():
        b = pd.read_pickle(cache)
    else:
        b = _vida_query(tuple(box), with_geometry=True)
        b.to_pickle(cache)
    geoms = shapely.from_wkb(b["geometry"].apply(bytes).values)
    bg = gpd.GeoDataFrame(b.drop(columns="geometry"), geometry=geoms, crs=C.CRS_GEO)
    cent = bg.geometry.representative_point().to_crs(C.CRS_EQUAL_AREA)
    keep = cent.within(zone5).values
    bg, cent = bg[keep], cent[keep]
    tree = cKDTree(np.c_[fp.geometry.x.values, fp.geometry.y.values])
    dist, _ = tree.query(np.c_[cent.x.values, cent.y.values])
    bg = bg.assign(dist_m=dist).sort_values("dist_m")
    total_in_5km = len(bg)
    bg = bg.head(C.CASE_STUDY_MAX_BUILDINGS)
    coords = []
    for g in bg.geometry:
        poly = max(shapely.get_parts(g), key=lambda p: p.area) if g.geom_type != "Polygon" else g
        coords.append([[round(x, 5), round(y, 5)] for x, y in poly.exterior.coords])
    hs = h[h["cluster"] == fire_cluster]
    t0 = pd.Timestamp(C.SEASON_START, tz="UTC")
    hot = [[round(float(r.lon), 5), round(float(r.lat), 5), int(round(r.frp)),
            int((r.time - t0).total_seconds() // 3600)] for r in hs.itertuples()]
    return {
        "fire_id": int(fire_cluster),
        "buildings": {"type": "polygon", "coords": coords, "dist_m": [int(round(d)) for d in bg["dist_m"]]},
        "hotspots": hot,
        "rings": rings,
        "_n_buildings_5km": total_in_5km,
        "_n_buildings_kept": len(bg),
        "_nearest_m": float(bg["dist_m"].min()) if len(bg) else None,
    }


if __name__ == "__main__":
    run()
