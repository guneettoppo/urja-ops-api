"""Operational routes: health, sync status, manual sync trigger."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter

from urja.config import settings
from urja.db import store
from urja.errors import Problem
from urja.models import HealthResponse, PortalState, SyncCounts, SyncStatus
from urja.sync import runner

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness of this service (not of the portal)",
    description=(
        "Reports whether the local index is queryable. Deliberately does **not** call "
        "the portal: the service keeps serving the index when the upstream is down, "
        "which is the point of caching it."
    ),
)
def health() -> HealthResponse:
    try:
        stats = store.get_counts()
    except Exception:  # pragma: no cover - only on a missing/corrupt database
        return HealthResponse(status="degraded", indexedMeters=0, indexedPoints=0)
    return HealthResponse(
        status="ok", indexedMeters=stats["count"], indexedPoints=stats["readings"]
    )


@router.get(
    "/sync/status",
    response_model=SyncStatus,
    summary="Index freshness and last sync outcome",
    description=(
        "How stale the index is, whether the last sync failed partially, and whether a "
        "sync is running right now.\n\n"
        "`dataAgeSeconds` is the freshness of *our copy*. `counts.lastReadingAt` is the "
        "newest reading the portal actually holds — a different question, and in this "
        "dataset the answer is months old."
    ),
)
def sync_status() -> SyncStatus:
    stats = store.get_counts()
    last = store.latest_sync_run()

    progress = None
    with store.connect() as conn:
        raw = store.meta_get(conn, "sync_progress")
    if raw:
        try:
            progress = json.loads(raw)
        except ValueError:
            progress = None

    return SyncStatus(
        lastSuccessAt=store.meta_last_success(),
        dataAgeSeconds=store.data_age_seconds(),
        running=runner.is_running(),
        lastRun={**last, "progress": progress} if last else None,
        counts=SyncCounts(
            meters=stats["count"],
            readings=stats["readings"],
            firstReadingAt=stats["first_reading_at"],
            lastReadingAt=stats["last_reading_at"],
        ),
        portal=PortalState(configured=bool(settings.portal_email and settings.portal_password)),
    )


@router.post(
    "/sync",
    status_code=202,
    summary="Trigger a sync now",
    description=(
        "Starts a sync in the background and returns immediately. `409` if one is "
        "already running; poll `/sync/status` for progress."
    ),
)
async def trigger_sync() -> dict:
    if runner.is_running():
        raise Problem(
            409,
            "Sync already running",
            "Another sync is in progress; poll /sync/status for progress.",
            type_="urn:urja:sync-in-flight",
        )
    asyncio.create_task(runner.run_sync(include_energy=settings.sync_energy_on_start))
    return {"state": "scheduled"}
