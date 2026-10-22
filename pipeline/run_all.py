"""Run the whole pipeline.

Two modes:

* snapshot (default): the delivered analysis, frozen at config.SNAPSHOT_CUTOFF_UTC.
  downloads -> hotspots -> exposure -> rasters -> exports. It writes into web/ and
  outputs/ only with --force; with --out DIR it writes DIR/data, DIR/assets and
  DIR/outputs instead, which lets you check that the snapshot still reproduces.
* live: the monitor. Cut-off "now", rolling window, quality gate, atomic swap of --out
  (default build/live). See pipeline/live.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config as C


def _inside(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def snapshot(out: Path | None, force: bool) -> int:
    out = out.resolve() if out is not None else None
    target = out if out is not None else C.WEB
    if not force and (_inside(target, C.WEB) or _inside(target, C.OUTPUTS)):
        print(
            "Snapshot mode would overwrite the frozen v1.0.0 outputs in web/ and outputs/.\n"
            "  - To check that the snapshot reproduces, write elsewhere:\n"
            "      uv run python -m pipeline.run_all --mode snapshot --out build/snapshot\n"
            "  - To regenerate web/ and outputs/ in place, add --force.\n"
            "  - For current data, use the live monitor:\n"
            "      uv run python -m pipeline.run_all --mode live --out build/live",
            file=sys.stderr,
        )
        return 2
    if out is not None and not _inside(out, C.WEB):
        # Redirect every snapshot writer; they all read these paths at call time.
        C.WEB_DATA, C.WEB_ASSETS, C.OUTPUTS = out / "data", out / "assets", out / "outputs"
        for p in (C.WEB_DATA, C.WEB_ASSETS, C.OUTPUTS):
            p.mkdir(parents=True, exist_ok=True)

    from . import download, exposure, export, hotspots, rasters

    meta_path = C.RAW / "firms" / "meta.json"
    if meta_path.exists():
        cached = json.loads(meta_path.read_text(encoding="utf-8")).get("cutoff_utc")
        if cached != C.SNAPSHOT_CUTOFF_UTC:
            print(f"data/raw/firms/ holds a download with cut-off {cached}, not the snapshot cut-off "
                  f"{C.SNAPSHOT_CUTOFF_UTC}. Remove data/raw/firms/ and data/interim/ and run again.",
                  file=sys.stderr)
            return 2

    download.download_districts()
    download.download_worldpop()
    download.download_terrain()
    key = download.get_map_key(wait_minutes=10)
    if not key:
        print("No se encontró FIRMS_MAP_KEY en .env; se generan solo los rasters.")
        rasters.run()
        return 1
    cutoff = datetime.fromisoformat(C.SNAPSHOT_CUTOFF_UTC.replace("Z", "+00:00"))
    download.download_firms(key, cutoff=cutoff)
    hotspots.run()
    exposure.run()
    rasters.run()
    export.run()
    sizes = {p.relative_to(C.ROOT).as_posix() if _inside(p, C.ROOT) else str(p): p.stat().st_size
             for d in (C.WEB_DATA, C.WEB_ASSETS) for p in sorted(d.iterdir()) if p.is_file()}
    print(json.dumps(sizes, indent=1))
    print("total web bytes:", sum(sizes.values()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run_all", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["snapshot", "live"], default="snapshot",
                        help="snapshot: frozen v1.0.0 cut-off (default); live: cut-off now")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (live default: build/live; snapshot default: web/)")
    parser.add_argument("--force", action="store_true",
                        help="snapshot only: allow writing into web/ and outputs/")
    args = parser.parse_args(argv)
    if args.mode == "live":
        if args.force:
            parser.error("--force only applies to snapshot mode")
        from . import live
        return live.main(args.out or C.LIVE_DEFAULT_OUT)
    return snapshot(args.out, args.force)


if __name__ == "__main__":
    sys.exit(main())
