"""Static exposure map for the README and social posts, drawn in the dashboard's visual style.

It reads only the exported web files (web/data and web/assets), so it can be re-rendered on its own
without re-running the pipeline:

    uv run python -m pipeline.static_map

Colours and type follow web/index.html: the night map surface, the relief, people in the validated
blue ramp, fire in the thermal ramp, Newsreader for display text and Schibsted Grotesk for labels.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, LogNorm  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402

from . import config as C  # noqa: E402

# Tokens from web/index.html.
NIGHT = "#070b14"
INK = "#e9edf3"
MUTED = "#8b96a8"
PEOPLE_INK = "#6dbbe6"
FIRE_INK = "#f08a4b"
PEOPLE_RAMP = ["#1d4e89", "#3fa7d6", "#e0fbfc"]
THERMAL = [(0.0, (120, 20, 14)), (0.3, (196, 44, 22)), (0.55, (229, 102, 28)), (0.78, (255, 176, 59)), (1.0, (255, 240, 205))]

# Google Fonts (SIL Open Font License), cached under data/raw so they are never committed.
FONT_URLS = {
    "serif": "https://raw.githubusercontent.com/google/fonts/main/ofl/newsreader/Newsreader%5Bopsz%2Cwght%5D.ttf",
    "sans": "https://raw.githubusercontent.com/google/fonts/main/ofl/schibstedgrotesk/SchibstedGrotesk%5Bwght%5D.ttf",
}
FONT_FALLBACKS = {"serif": ["Georgia", "DejaVu Serif"], "sans": ["Segoe UI", "DejaVu Sans"]}
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "setiembre", "octubre", "noviembre", "diciembre"]

FIG_W, FIG_H, DPI = 8.0, 10.0, 150  # 1200 x 1500 px, the 4:5 portrait ratio social feeds favour
TO_MERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def _fonts() -> dict[str, FontProperties]:
    out = {}
    for role, url in FONT_URLS.items():
        path = C.RAW / "fonts" / f"{role}.ttf"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                path.write_bytes(r.content)
            out[role] = FontProperties(fname=str(path))
        except (requests.RequestException, OSError):
            out[role] = FontProperties(family=FONT_FALLBACKS[role])
    return out


def _thermal(frp: np.ndarray) -> np.ndarray:
    pos = np.clip(np.log10(np.maximum(frp, 0) + 1) / np.log10(301), 0, 1)
    stops = np.array([s for s, _ in THERMAL])
    cols = np.array([c for _, c in THERMAL], dtype=float) / 255
    return np.stack([np.interp(pos, stops, cols[:, i]) for i in range(3)], axis=1)


def _num(v: float) -> str:
    return f"{round(v):,}".replace(",", " ")


def _lima(utc_iso: str) -> str:
    t = datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=-5)))
    return f"{t.day} de {MONTHS[t.month - 1]} de {t.year}, {t:%H:%M}"


def render(web_data: Path | None = None, web_assets: Path | None = None, out_path: Path | None = None) -> Path:
    # Resolve paths at call time: run_all redirects C.WEB_DATA / C.OUTPUTS for --out runs.
    web_data = web_data or C.WEB_DATA
    web_assets = web_assets or C.WEB_ASSETS
    out_path = out_path or C.OUTPUTS / "mapa_exposicion.png"
    f = _fonts()
    summary = json.loads((web_data / "summary.json").read_text(encoding="utf-8"))
    hot = json.loads((web_data / "hotspots.json").read_text(encoding="utf-8"))
    relief_bounds = json.loads((web_assets / "relief.json").read_text(encoding="utf-8"))["bounds"]

    districts = gpd.read_file(web_data / "districts.topo.json", layer="districts")
    if districts.crs is None:
        districts = districts.set_crs("EPSG:4326")
    districts = districts.to_crs("EPSG:3857")
    # The simplified web geometry has a few self-touching rings; repair them before dissolving.
    districts["geometry"] = districts.geometry.make_valid()
    exposed = districts[districts["p5"].fillna(0) > 0]
    deps = districts.dissolve(by="dep")
    country = districts.dissolve()

    # Hotspots of the last 7 days before the cut-off; those belonging to active vegetation fires glow.
    t0 = datetime.fromisoformat(hot["t0"].replace("Z", "+00:00"))
    cut = datetime.fromisoformat(summary["cutoff_utc"].replace("Z", "+00:00"))
    cut_h = (cut - t0).total_seconds() / 3600
    t = np.asarray(hot["t"], dtype=float)
    week = (t >= cut_h - 168) & (t <= cut_h + 1)
    active = week & (np.asarray(hot["f"]) >= 0)
    hx, hy = TO_MERC.transform(np.asarray(hot["x"], dtype=float), np.asarray(hot["y"], dtype=float))
    frp = np.asarray(hot["frp"], dtype=float)

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=NIGHT)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(NIGHT)
    ax.set_axis_off()

    # Frame Peru on the right so the Pacific on the left holds the title and the key figures.
    (x0, x1), (y0, y1) = TO_MERC.transform([-89.6, -67.9], [-19.4, 0.9])
    target = FIG_H / FIG_W
    if (y1 - y0) / (x1 - x0) < target:
        pad = ((x1 - x0) * target - (y1 - y0)) / 2
        y0, y1 = y0 - pad, y1 + pad
    else:
        pad = ((y1 - y0) / target - (x1 - x0)) / 2
        x0, x1 = x0 - pad, x1 + pad
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)

    (rx0, rx1), (ry0, ry1) = TO_MERC.transform([relief_bounds[0], relief_bounds[2]], [relief_bounds[1], relief_bounds[3]])
    ax.imshow(Image.open(web_assets / "relief_dark.png"), extent=(rx0, rx1, ry0, ry1), interpolation="bilinear", zorder=1)

    people = LinearSegmentedColormap.from_list("people", PEOPLE_RAMP)
    vmax = float(exposed["p5"].max()) if len(exposed) else 10.0
    norm = LogNorm(vmin=1, vmax=max(vmax, 10))
    districts.boundary.plot(ax=ax, color=(201 / 255, 210 / 255, 223 / 255, 0.07), linewidth=0.25, zorder=2)
    if len(exposed):
        exposed.plot(ax=ax, column="p5", cmap=people, norm=norm, alpha=0.9, edgecolor=(224 / 255, 251 / 255, 252 / 255, 0.35), linewidth=0.3, zorder=3)
    deps.boundary.plot(ax=ax, color=(201 / 255, 210 / 255, 223 / 255, 0.22), linewidth=0.45, zorder=4)
    country.boundary.plot(ax=ax, color=(201 / 255, 210 / 255, 223 / 255, 0.55), linewidth=0.8, zorder=4)

    embers = week & ~active
    ax.scatter(hx[embers], hy[embers], s=1.2, c=[(122 / 255, 42 / 255, 20 / 255)], alpha=0.55, linewidths=0, zorder=5)
    cols = _thermal(frp[active])
    ax.scatter(hx[active], hy[active], s=22, c=cols, alpha=0.10, linewidths=0, zorder=6)  # glow
    ax.scatter(hx[active], hy[active], s=3.2, c=cols, alpha=0.95, linewidths=0, zorder=7)

    # Name the three districts with the most people near a fire.
    stroke = [pe.withStroke(linewidth=3, foreground=NIGHT)]
    for _, row in exposed.nlargest(3, "p5").iterrows():
        p = row.geometry.representative_point()
        name = str(row["name"]).title().replace(" De ", " de ").replace(" Del ", " del ")
        ax.annotate(f"{name}\n{_num(row['p5'])}", xy=(p.x, p.y), xytext=(10, 0), textcoords="offset points", va="center",
                    color=INK, fontproperties=f["sans"], fontsize=8.5, linespacing=1.15, path_effects=stroke, zorder=9)
        ax.scatter([p.x], [p.y], s=10, facecolors="none", edgecolors=INK, linewidths=0.8, zorder=9)

    e5, e1, e10 = (summary["exposure"][k] for k in ("5", "1", "10"))
    left = 0.06
    fig.text(left, 0.955, "¿A quién alcanza\nel fuego?", va="top", color=INK, fontproperties=f["serif"], fontsize=34, linespacing=1.0)
    fig.text(left, 0.815, f"Personas a menos de 5 km de un incendio activo,\npor distrito. Perú, {_lima(summary['cutoff_utc'])} (hora de Lima).",
             va="top", color=MUTED, fontproperties=f["sans"], fontsize=9.5, linespacing=1.4)

    fig.text(left, 0.43, _num(e5["people"]), va="top", color=PEOPLE_INK, fontproperties=f["serif"], fontsize=40)
    fig.text(left, 0.37, f"personas viven a menos de 5 km\nde uno de los {summary['fires_active']} incendios\nactivos en vegetación.",
             va="top", color=INK, fontproperties=f["sans"], fontsize=10.5, linespacing=1.4)
    fig.text(left, 0.285, f"A 1 km: {_num(e1['people'])}\nA 10 km: {_num(e10['people'])}",
             va="top", color=MUTED, fontproperties=f["sans"], fontsize=9.5, linespacing=1.5)

    cax = fig.add_axes([left, 0.172, 0.30, 0.012])
    grad = np.linspace(0, 1, 256)[None, :]
    cax.imshow(grad, aspect="auto", cmap=people, extent=(0, 1, 0, 1))
    cax.set_axis_off()
    lo, hi = np.log10(norm.vmin), np.log10(norm.vmax)
    for v in (10, 100, 1000, 10000):
        if v <= norm.vmax:
            x = (np.log10(v) - lo) / (hi - lo)
            fig.text(left + 0.30 * x, 0.165, _num(v), ha="center", va="top", color=MUTED, fontproperties=f["sans"], fontsize=7.5)
    fig.text(left, 0.192, "Personas a menos de 5 km (escala logarítmica)", va="bottom", color=INK, fontproperties=f["sans"], fontsize=8)
    fig.text(left + 0.014, 0.126, "Focos de incendios activos, últimos 7 días", va="center", color=INK, fontproperties=f["sans"], fontsize=8)
    fig.patches.append(matplotlib.patches.Ellipse((left + 0.004, 0.126), 0.0084, 0.0067, transform=fig.transFigure, color=FIRE_INK, figure=fig))

    fig.text(left, 0.032, "Fuentes: NASA FIRMS (VIIRS), WorldPop R2025A 2026, Google Open Buildings v3, límites INEI.\n"
             "Un foco de calor no es un incendio confirmado; las nubes pueden ocultar el fuego.\n"
             "Rodrigo Norabuena, PUCP. github.com/rodnm/mapa-incendios-peru-2026",
             va="bottom", color=MUTED, fontproperties=f["sans"], fontsize=6.8, linespacing=1.5)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=DPI, facecolor=NIGHT)
    plt.close(fig)
    return out_path


if __name__ == "__main__":
    print(render())
