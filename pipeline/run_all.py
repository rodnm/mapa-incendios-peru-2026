"""Run the whole pipeline: downloads -> hotspots -> exposure -> rasters -> exports."""

import json
import sys

from . import config as C
from . import download, exposure, export, hotspots, rasters


def main() -> int:
    download.download_districts()
    download.download_worldpop()
    download.download_terrain()
    key = download.get_map_key(wait_minutes=10)
    if not key:
        print("No se encontró FIRMS_MAP_KEY en .env; se generan solo los rasters.")
        rasters.run()
        return 1
    download.download_firms(key)
    hotspots.run()
    exposure.run()
    rasters.run()
    export.run()
    sizes = {p.relative_to(C.ROOT).as_posix(): p.stat().st_size
             for d in (C.WEB_DATA, C.WEB_ASSETS) for p in sorted(d.iterdir()) if p.is_file()}
    print(json.dumps(sizes, indent=1))
    print("total web bytes:", sum(sizes.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
