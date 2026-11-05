"""Build ``seed/urja.db`` — a compact, committed copy of the index.

The seed exists because free hosting tiers give you an ephemeral filesystem: a
restart wipes the index, and rebuilding it costs ~4 minutes of portal requests
on every cold start. Because this dataset is frozen, a committed snapshot is
both accurate and much cheaper.

    uv run python -m scripts.build_seed
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from urja.config import settings
from urja.db import store

OUT = Path(__file__).resolve().parent.parent / "seed" / "urja.db"


def main() -> int:
    source = store.get_db_path()
    if not source.exists():
        print(f"no index at {source}; run `python -m urja.sync` first")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    # VACUUM INTO produces a single-file copy with no WAL/journal sidecars,
    # which is exactly what you want checked into a repo.
    with sqlite3.connect(source) as conn:
        conn.execute("VACUUM INTO ?", (str(OUT),))

    with sqlite3.connect(OUT) as conn:
        meters = conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]
        readings = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        last = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]

    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} — {meters} meters, {readings} readings, newest {last}")
    print(f"size {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
