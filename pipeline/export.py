"""Compact web outputs (web/data), static map and findings (outputs/)."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import config as C
from .exposure import case_study
from .hotspots import load_districts, load_meta

T0 = pd.Timestamp(C.SEASON_START, tz="UTC")


def dump(obj, path: Path) -> int:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path.stat().st_size


def iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_since_t0(t: pd.Series, t0: pd.Timestamp = T0) -> np.ndarray:
    return ((t - t0).dt.total_seconds() // 3600).astype(int).values


# ----------------------------------------------------------------------------- pieces
# Every writer takes an optional out_dir; the default (C.WEB_DATA) is the snapshot target.
def export_hotspots(h: pd.DataFrame, active_ids: set, out_dir: Path | None = None,
                    t0: pd.Timestamp = T0) -> bool:
    h = h.assign(f=np.where(h["cluster"].isin(active_ids), h["cluster"], -1))
    path = (out_dir or C.WEB_DATA) / "hotspots.json"

    def build(df):
        return {
            "t0": iso(t0),
            "x": df["lon"].round(4).tolist(),
            "y": df["lat"].round(4).tolist(),
            "t": hours_since_t0(df["time"], t0).tolist(),
            "frp": df["frp"].round().astype(int).tolist(),
            "s": df["sat"].astype(int).tolist(),
            "f": df["f"].astype(int).tolist(),
        }

    df = h.sort_values("time")
    size = dump(build(df), path)
    dedup = False
    factor = 1
    while size > C.HOTSPOTS_MAX_BYTES and factor <= 4:
        # One point per (grid cell, day), keeping the max FRP; the grid grows if needed
        dedup = True
        key = df.assign(gx=df["cx"] // factor, gy=df["cy"] // factor)
        dd = (key.sort_values("frp", ascending=False)
                 .drop_duplicates(subset=["gx", "gy", "day"])
                 .sort_values("time"))
        size = dump(build(dd), path)
        factor += 1
    n_out = len(dd) if dedup else len(df)
    return dedup, n_out, (factor - 1) if dedup else 0


def export_series(h: pd.DataFrame, cutoff: pd.Timestamp, out_dir: Path | None = None,
                  start: str = C.SEASON_START) -> None:
    days = pd.date_range(start, cutoff.tz_convert(None).normalize(), freq="D")
    d = h.assign(date=h["time"].dt.tz_convert(None).dt.normalize())
    tab = d.groupby(["date", "sat"]).size().unstack(fill_value=0).reindex(days, fill_value=0)
    for k in range(len(C.SATELLITES)):
        if k not in tab.columns:
            tab[k] = 0
    dump({
        "dates": [x.strftime("%Y-%m-%d") for x in days],
        "total": tab.sum(axis=1).astype(int).tolist(),
        "snpp": tab[0].astype(int).tolist(),
        "noaa20": tab[1].astype(int).tolist(),
        "noaa21": tab[2].astype(int).tolist(),
    }, (out_dir or C.WEB_DATA) / "series.json")


def export_fires(active: pd.DataFrame, out_dir: Path | None = None) -> list:
    out = []
    for r in active.itertuples():
        out.append({
            "id": int(r.cluster), "lon": round(float(r.lon), 5), "lat": round(float(r.lat), 5),
            "n": int(r.n), "n24": int(r.n24), "footprint_ha": round(float(r.footprint_ha), 1),
            "frp24": round(float(r.frp24), 1), "first_seen": iso(r.first_seen), "last_seen": iso(r.last_seen),
            "district": r.name, "province": r.province, "department": r.department,
            "people": {str(k): int(round(getattr(r, f"people_{k}km"))) for k in C.RADII_KM},
            "buildings": {str(k): int(getattr(r, f"buildings_{k}km")) for k in C.RADII_KM},
            "priority": None if r.urban else round(float(r.priority), 1),
            "parts": None if r.urban else {
                "people": round(100 * r.part_people, 1), "footprint": round(100 * r.part_footprint, 1),
                "frp": round(100 * r.part_frp, 1), "activity": round(100 * r.part_activity, 1)},
            "urban": bool(r.urban),
            "flag_reason": r.flag_reason or None,
        })
    dump(out, (out_dir or C.WEB_DATA) / "fires.json")
    return out


def export_districts(districts: gpd.GeoDataFrame, exp: pd.DataFrame, out_dir: Path | None = None) -> str:
    props = exp.reindex(districts["id"].values)
    g = districts[["id", "name", "province", "department", "geometry"]].rename(
        columns={"province": "prov", "department": "dep"}).reset_index(drop=True)
    g["area_km2"] = props["area_km2"].round(1).values
    g["pop"] = props["pop"].round().astype(int).values
    g["hot7"] = props["hot7"].astype(int).values
    g["hot_season"] = props["hot_season"].astype(int).values
    g["frp7"] = props["frp7"].round(1).values
    for r in C.RADII_KM:
        g[f"p{r}"] = props[f"people_{r}km"].round().astype(int).values
    g["b5"] = props["buildings_5km"].astype(int).values

    out_dir = out_dir or C.WEB_DATA
    out = out_dir / "districts.topo.json"
    try:
        import topojson as tp
        for eps in (0.004, 0.006, 0.009, 0.013, 0.02):
            topo = tp.Topology(g, prequantize=1e5, toposimplify=eps, object_name="districts")
            out.write_text(topo.to_json(), encoding="utf-8")
            if out.stat().st_size <= C.TOPO_MAX_BYTES:
                break
        # Compact JSON (topojson's writer may include spaces)
        dump(json.loads(out.read_text(encoding="utf-8")), out)
        return "topojson"
    except Exception as exc:  # pragma: no cover - fallback path
        print("[districts] TopoJSON failed, falling back to GeoJSON:", exc)
        g2 = g.copy()
        g2["geometry"] = g2.geometry.simplify(0.01, preserve_topology=True)
        g2.to_file(out_dir / "districts.geojson", driver="GeoJSON", COORDINATE_PRECISION=4)
        return "geojson"


def export_district_csv(districts: gpd.GeoDataFrame, exp: pd.DataFrame, out_dir: Path | None = None) -> pd.DataFrame:
    info = districts.set_index("id")[["department", "province", "name"]].rename(columns={"name": "district"})
    t = info.join(exp)
    cols = [f"people_{r}km" for r in C.RADII_KM] + [f"buildings_{r}km" for r in C.RADII_KM]
    t = t[(t[cols].sum(axis=1) > 0)].copy()
    for c in cols:
        t[c] = t[c].round().astype(int)
    t["hotspots_7d"] = t["hot7"].astype(int)
    t = t.reset_index().rename(columns={"index": "id"})
    t = t[["id", "department", "province", "district"] + cols + ["hotspots_7d"]].sort_values(
        "people_5km", ascending=False)
    t.to_csv((out_dir or C.WEB_DATA) / "exposure_by_district.csv", index=False, encoding="utf-8")
    return t


def pick_case_study(active: pd.DataFrame) -> tuple[pd.Series, list[str]]:
    """Vegetation fire (>= 3 hotspots, >= 50 ha, not flagged) with most people within 5 km."""
    ok = active[(~active["urban"]) & (active["n"] >= C.CASE_MIN_HOTSPOTS)
                & (active["footprint_ha"] >= C.CASE_MIN_FOOTPRINT_HA)]
    notes = [f"Regla: incendio activo con al menos {C.CASE_MIN_HOTSPOTS} focos y {C.CASE_MIN_FOOTPRINT_HA} ha "
             f"de huella, sin marca urbana ni de fuente fija, con más personas a 5 km "
             f"({len(ok)} candidatos)."]
    if ok.empty:
        ok = active[~active["urban"]]
    return ok.sort_values("people_5km", ascending=False).iloc[0], notes


# ----------------------------------------------------------------------------- static map
def static_map() -> None:
    """Draw outputs/mapa_exposicion.png from the files just exported, in the dashboard's style."""
    from .static_map import render
    render()


# ----------------------------------------------------------------------------- main
def run() -> dict:
    meta = load_meta()
    hstats = json.loads((C.INTERIM / "hotspots_meta.json").read_text(encoding="utf-8"))
    totals = json.loads((C.INTERIM / "exposure_totals.json").read_text(encoding="utf-8"))
    flagged = json.loads((C.INTERIM / "urban_flagged.json").read_text(encoding="utf-8"))
    cutoff = pd.Timestamp(meta["cutoff_utc"])
    h = pd.read_pickle(C.INTERIM / "hotspots_clean.pkl")
    active = pd.read_pickle(C.INTERIM / "active_fires.pkl")
    exp = pd.read_pickle(C.INTERIM / "exposure_by_district.pkl")
    districts = load_districts()

    dedup, n_points, dedup_cells = export_hotspots(h, set(active.loc[~active["urban"], "cluster"]))
    export_series(h, cutoff)
    export_fires(active)
    topo_kind = export_districts(districts, exp)
    table = export_district_csv(districts, exp)

    # Case study
    cs_fire, cs_notes = pick_case_study(active)
    cs = case_study(int(cs_fire["cluster"]))
    n5, kept, nearest = cs.pop("_n_buildings_5km"), cs.pop("_n_buildings_kept"), cs.pop("_nearest_m")
    while dump(cs, C.WEB_DATA / "case_study.json") > C.CASE_STUDY_MAX_BYTES * 0.3 and len(cs["buildings"]["coords"]) > 500:
        k = int(len(cs["buildings"]["coords"]) * 0.8)
        cs["buildings"]["coords"] = cs["buildings"]["coords"][:k]
        cs["buildings"]["dist_m"] = cs["buildings"]["dist_m"][:k]
    kept = len(cs["buildings"]["coords"])
    pts = np.array([p[:2] for p in cs["hotspots"]] + [xy for ring in cs["rings"].values() for xy in ring])
    bbox = [round(float(pts[:, 0].min()), 5), round(float(pts[:, 1].min()), 5),
            round(float(pts[:, 0].max()), 5), round(float(pts[:, 1].max()), 5)]

    case = {
        "fire_id": int(cs_fire["cluster"]), "name": f"Incendio de {cs_fire['name']}", "district": cs_fire["name"],
        "province": cs_fire["province"], "department": cs_fire["department"],
        "lon": round(float(cs_fire["lon"]), 5), "lat": round(float(cs_fire["lat"]), 5), "bbox": bbox,
        "people_1km": int(round(cs_fire["people_1km"])), "people_5km": int(round(cs_fire["people_5km"])),
        "buildings_1km": int(cs_fire["buildings_1km"]), "buildings_5km": int(cs_fire["buildings_5km"]),
        "nearest_building_m": int(round(nearest)) if nearest is not None else None,
        "footprint_ha": round(float(cs_fire["footprint_ha"]), 1),
        "first_seen": iso(cs_fire["first_seen"]), "last_seen": iso(cs_fire["last_seen"]),
        "buildings_shown": kept, "selection_notes": cs_notes,
    }
    summary = build_summary(meta, hstats, totals, flagged, active, exp, table,
                            (dedup, n_points, dedup_cells), topo_kind, case_study=case)
    dump(summary, C.WEB_DATA / "summary.json")

    # Static map + findings
    static_map()
    findings = build_findings(summary, table)
    (C.OUTPUTS / "hallazgos.json").write_text(json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[export] case study:", summary["case_study"])
    return summary


def build_summary(meta: dict, hstats: dict, totals: dict, flagged: dict, active: pd.DataFrame,
                  exp: pd.DataFrame, table: pd.DataFrame, points: tuple, topo_kind: str,
                  season_start: str = C.SEASON_START, case_study: dict | None = None) -> dict:
    """summary.json: headline figures, exposure by radius, top districts and thresholds.

    `points` is the (deduplicated, n_points, dedup_cells) tuple returned by export_hotspots.
    """
    dedup, n_points, dedup_cells = points
    worldpop_total = float(exp["pop"].sum())
    top = table.head(10)
    return {
        "cutoff_utc": meta["cutoff_utc"],
        "cutoff_lima": meta["cutoff_lima"],
        "season_start": season_start,
        "satellites": C.SATELLITE_LABELS,
        "hotspots_season": hstats["hotspots_season"],
        "hotspots_7d": hstats["hotspots_7d"],
        "hotspots_24h": hstats["hotspots_24h"],
        "fires_active": int((~active["urban"]).sum()),
        "fires_active_gt100ha": int(((~active["urban"]) & (active["footprint_ha"] > 100)).sum()),
        "fires_active_all": hstats["fires_active"],
        "urban_flagged": flagged,
        "worldpop_total": int(round(worldpop_total)),
        "persons_per_dwelling": C.PERSONS_PER_DWELLING,
        "removed": {k: hstats["removed"][k] for k in ("low_conf", "volcano", "persistent")},
        "removed_outside_peru": hstats["removed"]["outside_peru"],
        "exposure": {k: {kk: v[kk] for kk in ("people", "buildings", "people_from_buildings", "districts")}
                     for k, v in totals.items()},
        "top_districts": [
            {"id": r.id, "name": r.district, "province": r.province, "department": r.department,
             "people_5km": int(r.people_5km), "buildings_5km": int(r.buildings_5km), "hotspots_7d": int(r.hotspots_7d)}
            for r in top.itertuples()],
        "case_study": case_study,
        "priority_formula": C.PRIORITY_FORMULA,
        "season_points_deduplicated": dedup,
        "season_points": n_points,
        "season_points_dedup_cell_m": int(375 * dedup_cells) if dedup else None,
        "districts_format": topo_kind,
        "exposure_window_hours": C.EXPOSURE_WINDOW_HOURS,
        "thresholds": {
            "volcano_radius_km": C.VOLCANO_RADIUS_KM, "persistent_min_days": C.PERSISTENT_MIN_DAYS,
            "persistent_min_span_days": C.PERSISTENT_MIN_SPAN_DAYS, "cluster_km": C.CLUSTER_DIST_KM,
            "cluster_hours": C.CLUSTER_HOURS, "active_hours": C.ACTIVE_WINDOW_HOURS,
            "pixel_ha": C.PIXEL_AREA_HA, "buildings_min_confidence": C.OB_MIN_CONFIDENCE,
            "urban_people_1km": C.URBAN_PEOPLE_1KM, "urban_density_km2": C.URBAN_DENSITY_KM2,
            "fixed_max_hotspots": C.FIXED_MAX_HOTSPOTS, "fixed_radius_km": C.FIXED_RADIUS_KM,
            "fixed_min_gap_days": C.FIXED_MIN_GAP_DAYS,
            "case_min_hotspots": C.CASE_MIN_HOTSPOTS, "case_min_footprint_ha": C.CASE_MIN_FOOTPRINT_HA,
        },
    }


def fmt(n: float) -> str:
    """Spanish thousands separator (space-free, dot-free: use thin grouping with a dot)."""
    return f"{int(round(n)):,}".replace(",", ".")


def build_findings(s: dict, table: pd.DataFrame) -> list:
    e = s["exposure"]
    p1, p5, p10 = e["1"]["people"], e["5"]["people"], e["10"]["people"]
    top = s["top_districts"][0]
    share = 100 * top["people_5km"] / p5 if p5 else 0
    top10 = 100 * sum(d["people_5km"] for d in s["top_districts"]) / max(p5, 1)
    b5, pb5 = e["5"]["buildings"], e["5"]["people_from_buildings"]
    ratio = p5 / b5 if b5 else 0
    cs, uf = s["case_study"], s["urban_flagged"]
    cut = s["cutoff_lima"][:16].replace("T", " ")
    return [
        {
            "titulo": "El radio cambia la respuesta",
            "texto": (f"Al corte ({cut}, hora de Lima) había {s['fires_active']} incendios activos de vegetación. "
                      f"A 5 km de sus focos de las últimas 48 h viven unas {fmt(p5)} personas (WorldPop 2026). "
                      f"Con 1 km la cifra baja a {fmt(p1)} y con 10 km sube a {fmt(p10)}: el radio elegido "
                      f"multiplica el resultado por {p10 / max(p1, 1):.0f}. Aparte quedan {uf['fires']} incendios "
                      f"marcados como urbanos o de fuente fija, que habrían sumado {fmt(uf['people_5km'])} personas más."),
            "cifra": f"{fmt(p5)} personas a 5 km (rango {fmt(p1)}–{fmt(p10)})",
        },
        {
            "titulo": f"{top['name']} concentra la exposición",
            "texto": (f"El distrito de {top['name']} ({top['province']}, {top['department']}) reúne "
                      f"{fmt(top['people_5km'])} personas a 5 km de un incendio activo, el {share:.0f} % del total "
                      f"nacional. Los 10 primeros distritos suman el {top10:.0f} %."),
            "cifra": f"{share:.0f} % del total en un distrito",
        },
        {
            "titulo": "Dos fuentes que no coinciden del todo",
            "texto": (f"Open Buildings cuenta {fmt(b5)} edificaciones a 5 km. Con {str(s['persons_per_dwelling']).replace('.', ',')} "
                      f"personas por vivienda (Censo 2017, cifra de referencia) serían {fmt(pb5)} personas, frente a "
                      f"{fmt(p5)} de WorldPop ({f'{ratio:.1f}'.replace('.', ',')} personas por edificación): no toda edificación es una "
                      f"vivienda. En el caso de estudio ({cs['district']}, {cs['department']}) la edificación más "
                      f"cercana está a {fmt(cs['nearest_building_m'] or 0)} m de un foco."),
            "cifra": f"{fmt(pb5)} frente a {fmt(p5)}",
        },
    ]


if __name__ == "__main__":
    run()
