"""`python -m urja.sync` — run a single full sync and print the outcome."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from urja.config import settings
from urja.sync.runner import run_sync


def main() -> int:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not settings.portal_email or not settings.portal_password:
        print(
            "Portal credentials are missing. Copy .env.example to .env and fill in "
            "PORTAL_EMAIL / PORTAL_PASSWORD.",
            file=sys.stderr,
        )
        return 2
    result = asyncio.run(run_sync(include_energy=settings.sync_energy_on_start))
    print(json.dumps(result, indent=2))
    return 0 if result.get("state") in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
