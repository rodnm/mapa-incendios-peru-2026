"""Assemble the static site for GitHub Pages (used by CI, runnable locally).

    uv run python scripts/build_site.py [--live build/live] [--out site]

site/index.html     live monitor (live/index.html)
site/data/          live outputs, after passing the quality gate again
site/assets/        relief and population layers copied from web/assets
site/historia/      the frozen v1.0.0 story: web/index.html wrapped in a complete HTML
                    document (that file is written for a host that adds the skeleton),
                    plus web/data and web/assets

The site is built in a temporary directory and swapped into --out at the end.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import checks, config as C  # noqa: E402

LIVE_ASSETS = ["relief_dark.png", "relief.json", "population.png", "population.json"]
STORY_HEAD = ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>\n')
STORY_TAIL = "</body></html>\n"


def build(live: Path, out: Path) -> Path:
    problems = checks.validate(live)
    if problems:
        raise SystemExit(f"{live} did not pass the quality gate; run the live pipeline first:\n  - "
                         + "\n  - ".join(problems))
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        (tmp / "assets").mkdir(parents=True)
        shutil.copyfile(ROOT / "live" / "index.html", tmp / "index.html")
        shutil.copytree(live, tmp / "data")
        for name in LIVE_ASSETS:
            shutil.copyfile(C.WEB_ASSETS / name, tmp / "assets" / name)

        story = tmp / "historia"
        story.mkdir()
        body = (C.WEB / "index.html").read_bytes()  # embedded byte for byte
        (story / "index.html").write_bytes(STORY_HEAD.encode() + body + STORY_TAIL.encode())
        shutil.copytree(C.WEB_DATA, story / "data")
        shutil.copytree(C.WEB_ASSETS, story / "assets")

        old = out.with_name(f".{out.name}.old-{os.getpid()}")
        if out.exists():
            os.replace(out, old)
        os.replace(tmp, out)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--live", type=Path, default=C.LIVE_DEFAULT_OUT, help="live pipeline output (default build/live)")
    p.add_argument("--out", type=Path, default=ROOT / "site", help="site directory (default site/)")
    args = p.parse_args()
    out = build(args.live.resolve(), args.out.resolve())
    files = sorted(f for f in out.rglob("*") if f.is_file())
    for f in files:
        print(f"{f.stat().st_size:>10}  {f.relative_to(out).as_posix()}")
    print(f"{sum(f.stat().st_size for f in files):>10}  total ({len(files)} files) in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
