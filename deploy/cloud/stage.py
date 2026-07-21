"""Assemble the az-acr-build context for the cortex-solo image.

Copies ONLY code from the sibling checkouts into a staging dir:
  core/src, core/plugins  (data dirs, dossier, memory, imports excluded)
  gateway/cortex_gateway
  gateway/hub_static      (built Hub SPA for the cloud web UI, Phase A)
plus the Dockerfile + litestream files from this folder.

The SPA must be BUILT before staging (npm run build in
cortex-desktop/hub/frontend); this script copies dist/ as-is and
prints its build time so a stale bundle is visible before it ships.

Usage:  python stage.py <staging-dir>
Then :  az acr build --registry <acr> --image cortex-solo:<tag> <staging-dir>

Never stage data: overseer.db, imports, gitignored identity files must
not end up in an image layer.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent                      # cortex-core/
GATEWAY = CORE.parent / "cortex-gateway"
HUB_DIST = CORE.parent / "cortex-desktop" / "hub" / "frontend" / "dist"

EXCLUDE_DIRS = {"__pycache__", "data", "imports", "memory", "dossier",
                ".git", "node_modules", ".devdata", "tests"}


def _copy(src: Path, dst: Path) -> None:
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, "*.db", "*.db-*",
                                      "*.jsonl", "*.log", "secrets.toml"),
        dirs_exist_ok=True)


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    stage = Path(sys.argv[1]).resolve()
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    _copy(CORE / "src", stage / "core" / "src")
    _copy(CORE / "plugins", stage / "core" / "plugins")
    _copy(GATEWAY / "cortex_gateway", stage / "gateway" / "cortex_gateway")

    # Built Hub SPA -> /app/gateway/hub_static in the image (the
    # gateway serves it when GATEWAY_STATIC_DIR points there). Print
    # the bundle's build time: shipping a stale dist is the same trap
    # as the stale-APK install, so make freshness visible here.
    index = HUB_DIST / "index.html"
    if not index.is_file():
        # The cloud image runs with GATEWAY_STATIC_DIR set, so web_ui is
        # on and the /api facade activates; shipping without the SPA
        # would serve a facade with no UI. Fail loudly instead. Build it
        # first (cd cortex-desktop/hub/frontend && npm run build), or set
        # CORTEX_STAGE_NO_WEBUI=1 to intentionally stage a UI-less image.
        import os as _os
        if _os.environ.get("CORTEX_STAGE_NO_WEBUI") == "1":
            print("no hub SPA at", HUB_DIST,
                  "(CORTEX_STAGE_NO_WEBUI=1: staging without web UI)")
        else:
            print("ERROR: no hub SPA at", HUB_DIST,
                  "- run npm run build in cortex-desktop/hub/frontend "
                  "first, or set CORTEX_STAGE_NO_WEBUI=1")
            shutil.rmtree(stage)
            sys.exit(2)
    else:
        _copy(HUB_DIST, stage / "gateway" / "hub_static")
        age_min = (time.time() - index.stat().st_mtime) / 60
        print(f"hub SPA staged (dist built {age_min:.0f} min ago)")
        if age_min > 240:
            print("WARNING: hub dist is over 4 hours old; "
                  "rebuild with npm run build if that is not intended")

    for f in ("Dockerfile", "litestream.yml",
              "litestream-restore.sh", "litestream-replicate.sh"):
        shutil.copy2(HERE / f, stage / f)

    # Paranoia sweep: nothing DB- or identity-shaped may be staged.
    leaks = [p for p in stage.rglob("*")
             if p.suffix in (".db", ".jsonl")
             or p.name in ("USER.md", "OVERSEER.md", "APP.md",
                           "secrets.toml")]
    if leaks:
        for p in leaks:
            print("LEAK:", p)
        shutil.rmtree(stage)
        sys.exit(1)
    n = sum(1 for p in stage.rglob("*") if p.is_file())
    print(f"staged {n} files at {stage}")


if __name__ == "__main__":
    main()
