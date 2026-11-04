"""Application entrypoint.

Run locally:

    uv run python -m urja.sync          # populate the index (~4 min cold)
    uv run uvicorn urja.main:app --port 8081
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from urja import errors
from urja.api import consumption, meters, network, system
from urja.config import settings
from urja.db import store
from urja.sync import runner as sync_tasks

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

DESCRIPTION = """
A clean REST API over the legacy **Urja Meter Ops** portal.

The portal has no API of its own: this service logs into it, indexes its data
locally, and serves a documented, typed, queryable surface on top.

**Design notes that matter when consuming this API**

* Energy values from the portal are *cumulative register readings*. Everything
  returned by `/meters/{id}/consumption` is a derived delta and is flagged
  `derived: true`.
* Sampling granularity varies **per meter** in the source data (30-minute or
  daily). Responses state the source granularity rather than hiding it.
* The portal's readings stop at a fixed date. `/sync/status` reports both how
  fresh our copy is (`dataAgeSeconds`) and how recent the underlying data is
  (`counts.lastReadingAt`).
* Errors are RFC 9457 `application/problem+json`. `404` means "no such meter";
  `503` means the portal is unreachable — retry those.
""".strip()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    refresher: asyncio.Task | None = None
    if settings.auto_sync_on_start:
        sync_tasks.bootstrap_if_empty()
        refresher = asyncio.create_task(sync_tasks.sync_loop())
    try:
        yield
    finally:
        if refresher is not None:
            refresher.cancel()


app = FastAPI(
    title="Urja Ops API",
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url=None,
    openapi_tags=[
        {"name": "meters", "description": "Meter population: filtering, detail, data quality."},
        {"name": "consumption", "description": "Derived consumption series."},
        {"name": "network", "description": "Reconstructed distribution hierarchy."},
        {"name": "system", "description": "Health and index freshness."},
    ],
    contact={"name": "Urja Ops API"},
    license_info={"name": "MIT"},
)

errors.register(app)

app.include_router(meters.router)
app.include_router(consumption.router)
app.include_router(network.router)
app.include_router(system.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")
