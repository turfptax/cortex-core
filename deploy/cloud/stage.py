"""Assemble the az-acr-build context for the cortex-solo image.

Copies ONLY code from the two sibling checkouts into a staging dir:
  core/src, core/plugins  (data dirs, dossier, memory, imports excluded)
  gateway/cortex_gateway
plus the Dockerfile + litestream files from this folder.

Usage:  python stage.py <staging-dir>
Then :  az acr build --registry <acr> --image cortex-solo:<tag> <staging-dir>

Never stage data: overseer.db, imports, gitignored identity files must
not end up in an image layer.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent                      # cortex-core/
GATEWAY = CORE.parent / "cortex-gateway"

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
