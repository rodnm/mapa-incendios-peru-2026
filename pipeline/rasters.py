"""Web raster layers: hillshaded relief (dark/light) and WorldPop density, in Web Mercator."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from . import config as C
from .download import terrain_tile_range

R_EARTH = 6378137.0
TARGET_HEIGHT = 2400
MAX_PNG_BYTES = 1_200_000


def lon2merc(lon):
    return np.radians(lon) * R_EARTH


def lat2merc(lat):
    return R_EARTH * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def merc2lat(y):
    return np.degrees(2 * np.arctan(np.exp(y / R_EARTH)) - np.pi / 2)


def merc_bounds():
    w, s, e, n = C.BBOX
    return float(lon2merc(w)), float(lat2merc(s)), float(lon2merc(e)), float(lat2merc(n))


def peru_mask(shape: tuple[int, int], bounds_merc) -> np.ndarray:
    from .hotspots import load_districts

    cache = C.INTERIM / "peru_outline.gpkg"
    if cache.exists():
        import geopandas as gpd
        peru = gpd.read_file(cache)
    else:
        d = load_districts()
        peru = d.dissolve()[["geometry"]]
        peru.to_file(cache, driver="GPKG")
    peru = peru.to_crs(C.CRS_WEB)
    h, w = shape
    transform = from_bounds(*bounds_merc, w, h)
    return rasterize(((g, 1) for g in peru.geometry), out_shape=shape, transform=transform,
                     fill=0, dtype="uint8").astype(bool)


def save_png(img: Image.Image, path: Path, max_bytes: int = MAX_PNG_BYTES) -> int:
    """Save an RGBA image, quantizing progressively until it fits the byte budget."""
    img.save(path, optimize=True)
    for colors in (256, 128, 64, 32):
        if path.stat().st_size <= max_bytes:
            break
        q = img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        q.save(path, optimize=True)
    return path.stat().st_size


# ----------------------------------------------------------------------------- relief
def mosaic_elevation(zoom: int = C.TERRAIN_ZOOM):
    xs, ys = terrain_tile_range(zoom)
    tile_dir = C.RAW / "terrain" / str(zoom)
    H, W = len(ys) * 256, len(xs) * 256
    elev = np.zeros((H, W), dtype=np.float32)
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            with Image.open(tile_dir / f"{x}_{y}.png") as im:
                a = np.asarray(im.convert("RGB"), dtype=np.float32)
            elev[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = (a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256) - 32768
    # Crop exactly to the bbox (fractional global pixel coordinates)
    n_px = 256 * 2**zoom
    w, s, e, n = C.BBOX

    def gx(lon):
        return (lon + 180) / 360 * n_px

    def gy(lat):
        r = math.radians(lat)
        return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n_px

    x0, x1 = int(round(gx(w))) - xs[0] * 256, int(round(gx(e))) - xs[0] * 256
    y0, y1 = int(round(gy(n))) - ys[0] * 256, int(round(gy(s))) - ys[0] * 256
    return elev[y0:y1, x0:x1], zoom


def hillshade(elev: np.ndarray, zoom: int, z_factor: float = 1.6) -> np.ndarray:
    """Blend of classic (az 315, alt 45) and multidirectional hillshade, in [0, 1]."""
    h = elev.shape[0]
    # Ground pixel size varies with latitude in Web Mercator
    ys = np.linspace(lat2merc(C.BBOX[3]), lat2merc(C.BBOX[1]), h)
    lat = np.radians(merc2lat(ys))
    px = 2 * math.pi * R_EARTH / (256 * 2**zoom) * np.cos(lat)
    dzdy, dzdx = np.gradient(elev * z_factor)
    dzdx = dzdx / px[:, None]
    dzdy = -dzdy / px[:, None]
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, dzdy)  # radians, aspect measured clockwise from north

    def shade(az_deg, alt_deg=45.0):
        az, alt = math.radians(az_deg), math.radians(alt_deg)
        v = math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect)
        return np.clip(v, 0, 1)

    classic = shade(315)
    multi = (shade(225) + shade(270) + shade(315) * 2 + shade(360)) / 5
    return 0.6 * classic + 0.4 * multi


def build_relief() -> dict:
    elev, zoom = mosaic_elevation()
    hs = hillshade(elev, zoom)
    h0, w0 = elev.shape
    tw = int(round(w0 * TARGET_HEIGHT / h0))
    hs_img = Image.fromarray((hs * 255).astype(np.uint8)).resize((tw, TARGET_HEIGHT), Image.LANCZOS)
    el_img = Image.fromarray(elev).resize((tw, TARGET_HEIGHT), Image.BILINEAR)
    hs = np.asarray(hs_img, dtype=np.float32) / 255.0
    el = np.asarray(el_img, dtype=np.float32)

    bm = merc_bounds()
    inside = peru_mask(hs.shape, bm)
    # Outside Peru: 30 % opacity at the border fading to 0 at ~120 km, so the relief never
    # reaches the image edges. Mercator is conformal: ground km = pixels * pixel size(lat).
    from scipy.ndimage import distance_transform_edt
    rows_lat = merc2lat(np.linspace(bm[3], bm[1], hs.shape[0]))
    px_km = (bm[2] - bm[0]) / hs.shape[1] / 1000 * np.cos(np.radians(rows_lat))
    dist_km = distance_transform_edt(~inside) * px_km[:, None]
    fade = np.clip(1 - dist_km / 120.0, 0, 1) ** 1.3
    alpha = np.where(inside, 255, 77 * fade)
    alpha = np.where(el <= 0, 0, alpha).astype(np.uint8)

    flat = math.sin(math.radians(45))
    # Dark: flat land near #20252f, shadows towards #0e1015, lit slopes up to ~#4a5262
    c_sh, c_flat, c_hi = np.array([14, 16, 21]), np.array([32, 37, 47]), np.array([74, 82, 98])
    lo = np.clip(hs / flat, 0, 1)[..., None]
    hi = np.clip((hs - flat) / (1 - flat), 0, 1)[..., None] ** 0.8
    dark = np.where(hs[..., None] <= flat, c_sh + (c_flat - c_sh) * lo, c_flat + (c_hi - c_flat) * hi)
    # Light: warm paper, shadows in soft grey
    shadow = np.clip((flat - hs) / flat, 0, 1) ** 0.9
    lit = np.clip((hs - flat) / (1 - flat), 0, 1)
    paper, grey = np.array([244, 241, 234]), np.array([150, 147, 142])
    light = paper - (paper - grey) * shadow[..., None] * 0.9 + 6 * lit[..., None]

    sizes = {}
    for name, rgb in (("relief_dark.png", dark), ("relief_light.png", light)):
        rgba = np.dstack([np.clip(rgb, 0, 255).astype(np.uint8), alpha])
        sizes[name] = save_png(Image.fromarray(rgba, "RGBA"), C.WEB_ASSETS / name)
    w, s, e, n = C.BBOX
    (C.WEB_ASSETS / "relief.json").write_text(json.dumps({"bounds": [w, s, e, n]}), encoding="utf-8")
    print("[relief]", {"px": [tw, TARGET_HEIGHT], **sizes})
    return sizes


# ----------------------------------------------------------------------------- population
def worldpop_1km() -> tuple[np.ndarray, rasterio.Affine]:
    """Sum the 100 m WorldPop grid into 10x10 blocks (~1 km), nodata as 0."""
    path = C.RAW / "worldpop" / Path(C.WORLDPOP_URL).name
    f = 10
    with rasterio.open(path) as src:
        H, W = src.height // f, src.width // f
        out = np.zeros((H, W), dtype=np.float64)
        nod = src.nodata
        step = f * 100
        for r0 in range(0, H * f, step):
            nrows = min(step, H * f - r0)
            a = src.read(1, window=((r0, r0 + nrows), (0, W * f))).astype(np.float64)
            a[(a == nod) | (a < 0)] = 0
            out[r0 // f:(r0 + nrows) // f] = a.reshape(nrows // f, f, W, f).sum(axis=(1, 3))
        t = src.transform * rasterio.Affine.scale(f, f)
    return out, t


def build_population() -> dict:
    counts, t = worldpop_1km()
    H, W = counts.shape
    lats = t.f + (np.arange(H) + 0.5) * t.e
    cell_km2 = (abs(t.a) * 111.32) * (abs(t.e) * 111.32) * np.cos(np.radians(lats))
    density = (counts / cell_km2[:, None]).astype(np.float32)

    bm = merc_bounds()
    out_h = 2200
    out_w = int(round(out_h * (bm[2] - bm[0]) / (bm[3] - bm[1])))
    dst = np.zeros((out_h, out_w), dtype=np.float32)
    reproject(density, dst, src_transform=t, src_crs=C.CRS_GEO,
              dst_transform=from_bounds(*bm, out_w, out_h), dst_crs=C.CRS_WEB,
              resampling=Resampling.average, src_nodata=None, dst_nodata=0)

    max_density = float(np.percentile(dst[dst > 0], 99.9))
    v = np.clip(np.log1p(dst) / np.log1p(max_density), 0, 1)
    stops = np.array([0.0, 0.35, 0.7, 1.0])
    colors = np.array([[29, 78, 137], [29, 78, 137], [63, 167, 214], [224, 251, 252]], dtype=np.float32)
    rgb = np.stack([np.interp(v, stops, colors[:, k]) for k in range(3)], axis=-1)
    alpha = np.where(dst <= 0.5, 0, np.clip(40 + 215 * v ** 0.8, 0, 255))
    rgba = np.dstack([rgb, alpha]).astype(np.uint8)
    size = save_png(Image.fromarray(rgba, "RGBA"), C.WEB_ASSETS / "population.png", 1_000_000)
    w, s, e, n = C.BBOX
    (C.WEB_ASSETS / "population.json").write_text(json.dumps(
        {"bounds": [w, s, e, n], "max_density": round(max_density, 1), "scale": "log",
         "units": "personas por km²", "source": "WorldPop R2025A 2026, 100 m constrained, agregado a ~1 km"}),
        encoding="utf-8")
    print("[population]", {"px": [out_w, out_h], "bytes": size, "max_density": round(max_density, 1),
                           "total_1km": round(counts.sum())})
    return {"bytes": size}


def run():
    build_relief()
    build_population()


if __name__ == "__main__":
    run()
