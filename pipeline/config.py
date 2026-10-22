"""Project-wide constants: dates, bounding box, radii, CRS, thresholds and paths."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
WEB_DATA = ROOT / "web" / "data"
WEB_ASSETS = ROOT / "web" / "assets"
OUTPUTS = ROOT / "outputs"

for _p in (RAW, INTERIM, WEB_DATA, WEB_ASSETS, OUTPUTS):
    _p.mkdir(parents=True, exist_ok=True)

# Season window
SEASON_START = "2026-07-01"

# Peru bounding box (west, south, east, north)
BBOX = (-81.5, -18.5, -68.5, 0.1)

# CRS
CRS_GEO = "EPSG:4326"
CRS_EQUAL_AREA = "ESRI:102033"  # South America Albers Equal Area Conic
CRS_WEB = "EPSG:3857"

# --- NASA FIRMS ---------------------------------------------------------------
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api"
FIRMS_DAY_RANGE = 5  # API maximum
SATELLITES = ["SNPP", "NOAA20", "NOAA21"]  # index used in hotspots.json "s"
SATELLITE_LABELS = ["S-NPP", "NOAA-20", "NOAA-21"]

# --- Districts (Hugging Face dataset of the course) ------------------------------
HF_REPO = "aquiro1994/ds-python-up"
HF_DISTRICTS_BASE = "_data/Folium/DISTRITOS"
HF_DISTRICTS_SIMPLE = "_data/Folium/peru_distrital_simple.geojson"

# --- WorldPop -----------------------------------------------------------------
WORLDPOP_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/2026/PER/v1/"
    "100m/constrained/per_pop_2026_CN_100m_R2025A_v1.tif"
)

# --- Google Open Buildings v3 ------------------------------------------------------
OB_TILES_URL = "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
OB_POINTS_URL = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip/{tile}_buildings.csv.gz"
OB_POLYGONS_URL = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip/{tile}_buildings.csv.gz"
OB_MIN_CONFIDENCE = 0.75

# --- Relief (AWS Terrain Tiles, Terrarium encoding) ---------------------------------
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_ZOOM = 8  # z7 gives only ~1700 px over the bbox; z8 is downsampled to ~2400 px

# --- Hotspot cleaning -------------------------------------------------------------
VOLCANOES = {  # name: (lat, lon)
    "Sabancaya": (-15.787, -71.857),
    "Ubinas": (-16.355, -70.903),
    "Misti": (-16.294, -71.409),
    "Ticsani": (-16.755, -70.595),
    "Huaynaputina": (-16.61, -70.85),
    "Yucamane": (-17.18, -70.20),
}
VOLCANO_RADIUS_KM = 3.0
# Persistent (static) sources: a ~375 m cell detected on many distinct days spread
# over a long span (gas flares, smelters, industrial heat).
PIXEL_DEG = 375.0 / 111_320.0  # grid cell size in degrees (~375 m at the equator)
PERSISTENT_MIN_DAYS = 10
PERSISTENT_MIN_SPAN_DAYS = 60

# --- Fire clustering --------------------------------------------------------------
CLUSTER_DIST_KM = 1.0
CLUSTER_HOURS = 48.0
ACTIVE_WINDOW_HOURS = 24.0
PIXEL_AREA_HA = 14.06  # 375 m x 375 m VIIRS I-band pixel; footprint, NOT burned area

# --- Exposure ---------------------------------------------------------------------
RADII_KM = [1, 2, 5, 10]
# Reference value: average persons per private household, INEI Censo Nacional 2017
# (about 3.5). To be verified against the official INEI publication.
PERSONS_PER_DWELLING = 3.5

# --- Priority index (0-100) per active fire ------------------------------------------
# priority = 100 * (0.35*n(log1p(people_5km)) + 0.25*n(footprint_ha)
#                   + 0.20*n(frp_sum_24h) + 0.20*n(hotspots_24h))
# n() = min-max normalisation across active fires.
PRIORITY_WEIGHTS = {"people": 0.35, "footprint": 0.25, "frp": 0.20, "activity": 0.20}
PRIORITY_FORMULA = (
    "prioridad = 100 × (0,35·n(log(1 + personas a 5 km)) + 0,25·n(huella en ha) "
    "+ 0,20·n(FRP total en 24 h) + 0,20·n(focos en 24 h)); "
    "n() = normalización mín–máx entre los incendios activos"
)

# --- Web budgets --------------------------------------------------------------------
HOTSPOTS_MAX_BYTES = 2_500_000
CASE_STUDY_MAX_BYTES = 2_000_000
TOPO_MAX_BYTES = 900_000

# Exposure zones are built from the hotspots of ACTIVE fires detected in the last 48 h
# (the current fire front). Many clusters stay "alive" for months by chaining <=48 h
# detections, so buffering their whole season history would overstate exposure.
EXPOSURE_WINDOW_HOURS = 48.0

# Open Buildings v3 via the VIDA GeoParquet mirror (Source Cooperative). The files carry a
# per-row bbox struct with row-group statistics, so DuckDB reads only the row groups that
# intersect the query box instead of whole S2 tiles (100-190 MB each as gzipped CSV).
OB_VIDA_FILES = [
    "https://data.source.coop/vida/google-microsoft-open-buildings/geoparquet/by_country_s2/"
    f"country_iso=PER/{s2}.parquet"
    for s2 in ("10394307939971104768", "10466365534009032704", "10502394331027996672")
]
CASE_STUDY_MAX_BUILDINGS = 8000

# --- Urban / fixed-source flags for active fires --------------------------------------
# A VIIRS hotspot inside a town is more often industry, a landfill or a rooftop/urban heat
# source than a vegetation fire. Flagged fires are excluded from exposure, district ranking
# and priority, and reported apart.
# (a) Urban core: >= 5000 people within 1 km of the fire's hotspots (a 1 km circle is
#     3.14 km2, so this is ~1600 people/km2), OR a hotspot falling in a ~1 km WorldPop cell
#     with >= 1500 people/km2. 1500/km2 is the "urban centre" density of the Degree of
#     Urbanisation (Eurostat / UN Statistical Commission, 2020) for 1 km2 grid cells.
URBAN_PEOPLE_1KM = 5000
URBAN_DENSITY_KM2 = 1500
# (b) Fixed / recurrent point source: a small fire (<= 2 hotspots, no spreading front)
#     at a spot that was already detected (within 500 m) at least 30 days before it
#     started. Vegetation rarely re-burns at the same spot a month later; industrial
#     plants, landfills and kilns do. Caveat: some re-burned farm plots may be removed.
FIXED_MAX_HOTSPOTS = 2
FIXED_RADIUS_KM = 0.5
FIXED_MIN_GAP_DAYS = 30

# Case study: a vegetation fire with a real front
CASE_MIN_HOTSPOTS = 3
CASE_MIN_FOOTPRINT_HA = 50

# --- Snapshot (v1.0.0) ----------------------------------------------------------------
# The delivered analysis is frozen at this cut-off. Snapshot mode always uses it, so a
# fresh FIRMS download is trimmed to the same instant instead of moving it to "now".
SNAPSHOT_CUTOFF_UTC = "2026-10-22T01:52:02Z"
WEB = ROOT / "web"

# --- Live monitor ---------------------------------------------------------------------
# Same methods as the snapshot, but the cut-off is "now" and the season becomes a
# rolling window of LIVE_WINDOW_DAYS full UTC days plus the current day.
LIVE_WINDOW_DAYS = 120
# NRT files are revised after publication: the current UTC day and this many previous
# days are downloaded again on every run; older cached days are reused as they are.
LIVE_REFETCH_DAYS = 2
LIVE_DEFAULT_OUT = ROOT / "build" / "live"
LIVE_FIRMS_DIR = RAW / "firms_live"
LIVE_STATE_DIR = RAW / "live_state"  # history.csv copy, restored from the CI cache
# Open Buildings extracts are cached on a fixed grid: the bbox of every exposure zone is
# rounded outwards to OB_TILE_DEG cells, and each cell is downloaded once.
OB_TILE_DEG = 0.25
OB_TILE_DIR = RAW / "open_buildings" / f"tiles_{OB_TILE_DEG}deg_conf{OB_MIN_CONFIDENCE}"
# Published site, used to read back the history of previous runs. Override with the
# LIVE_BASE_URL environment variable (an empty value disables the remote read).
LIVE_BASE_URL_DEFAULT = "https://rodnm.github.io/mapa-incendios-peru-2026/"
LIVE_HISTORY_MAX_ROWS = 2000
LIVE_MAX_BYTES = 6_000_000  # total size budget of one live output directory
LIVE_FILES_JSON = ["summary.json", "hotspots.json", "fires.json", "districts.topo.json",
                   "series.json", "buffers.json"]
LIVE_FILES_CSV = ["exposure_by_district.csv", "history.csv"]
HISTORY_COLUMNS = ["generated_at", "data_through", "hotspots_24h", "hotspots_7d", "fires_active",
                   "urban_flagged_fires", "people_1km", "people_2km", "people_5km", "people_10km",
                   "buildings_5km"]
# Quality gate: plausible ranges for one live run (inclusive). Peru has ~34 M people
# (WorldPop 2026); the snapshot had 325 hotspots in 24 h and 122 active fires.
LIVE_LIMITS = {
    "rows_last_48h_min": 1,          # FIRMS must have returned detections in the last 48 h
    "data_age_max_hours": 48,        # latest acquisition no older than this
    "hotspots_24h_max": 50_000,
    "hotspots_7d_max": 200_000,
    "hotspots_window_min": 1_000,    # 120 days in Peru never have fewer detections
    "hotspots_window_max": 2_000_000,
    "fires_active_max": 20_000,
    "people_max": 34_500_000,
    "districts_min": 1800,
}
