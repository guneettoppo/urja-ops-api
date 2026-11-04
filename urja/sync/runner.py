"""Sync: pull the portal into the local index.

Shape of a run:

1. one signed request to ``/portal/export`` returns all ~400 meters with their
   hierarchy and geo — no pagination needed;
2. two paged requests to ``/portal/dts`` give the transformer list, which is the
   authority for repairing blank feeder codes;
3. one request per meter for energy (there is no bulk readings endpoint), paced
   at ``polite_delay_ms`` and retried on 429.

A run is idempotent: meters are upserted, each meter's readings are replaced in
one transaction, and a failed run leaves the previous index intact.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from urja.config import settings
from urja.db import store
from urja.portal.client import PortalClient, PortalError
from urja.portal.parse import (
    detect_granularity_seconds,
    normalise_meter,
    normalise_readings,
    to_iso,
)

log = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()


def is_running() -> bool:
    return _sync_lock.locked()


def _progress(done: int, total: int, phase: str) -> None:
    with store.connect() as conn:
        store.meta_set(conn, "sync_progress", json.dumps({"phase": phase, "done": done, "total": total}))


async def run_sync(*, include_energy: bool = True) -> dict[str, Any]:
    """Perform one full or metadata-only sync. Returns a summary dict."""
    if _sync_lock.locked():
        return {"state": "running", "skipped": True}

    async with _sync_lock:
        store.init_db()
        run_id = store.start_sync_run()
        meters_synced = 0
        readings_synced = 0
        failures = 0
        notes: list[str] = []

        try:
            async with PortalClient(settings) as portal:
                dts = await portal.list_dts()
                with store.connect() as conn:
                    ds = _dedupe_dts(dts)
                    store.upsert_dts(conn, ds)
                dt_feeder = {d["code"]: d["feederCode"] for d in ds if d.get("code") and d.get("feederCode")}
                dt_names = {d["code"]: d["name"] for d in ds if d.get("code") and d.get("name")}
                log.info("fetched %d transformers", len(ds))

                rows, total = await portal.export_meters()
                log.info("export returned %d meters (total=%s)", len(rows), total)

                records: list[dict[str, Any]] = []
                for row in rows:
                    record, _issues = normalise_meter(row, dt_feeder=dt_feeder, dt_names=dt_names)
                    if record.get("meter_id"):
                        records.append(record)
                with store.connect() as conn:
                    meters_synced = store.upsert_meters(conn, records)
                _progress(0, len(records), "energy" if include_energy else "done")

                if include_energy:
                    for index, record in enumerate(records, start=1):
                        meter_id = record["meter_id"]
                        try:
                            raw = await portal.meter_energy(meter_id)
                        except PortalError as exc:
                            failures += 1
                            notes.append(f"{meter_id}: {exc}")
                            log.warning("energy fetch failed for %s: %s", meter_id, exc)
                            continue
                        if raw is None:
                            failures += 1
                            notes.append(f"{meter_id}: not found by energy endpoint")
                            continue

                        readings, _issues = normalise_readings(raw)
                        with store.connect() as conn:
                            readings_synced += store.replace_readings(conn, meter_id, readings)
                            store.set_granularity(conn, meter_id, detect_granularity_seconds(readings))
                        if index % 50 == 0:
                            _progress(index, len(records), "energy")
                            log.info("energy %d/%d", index, len(records))

            state = "ok" if failures == 0 else "partial"
            store.finish_sync_run(
                run_id,
                state=state,
                meters=meters_synced,
                readings=readings_synced,
                failures=failures,
                detail="; ".join(notes[:20]) or None,
            )
            _progress(meters_synced, meters_synced, "done")
            log.info("sync %s: %d meters, %d readings, %d failures", state, meters_synced, readings_synced, failures)
            return {
                "state": state,
                "meters": meters_synced,
                "readings": readings_synced,
                "failures": failures,
            }

        except Exception as exc:  # noqa: BLE001 - a failed run must be recorded, not hidden
            store.finish_sync_run(
                run_id, state="failed", meters=meters_synced, readings=readings_synced, failures=failures,
                detail=str(exc)[:500],
            )
            log.exception("sync failed")
            return {"state": "failed", "error": str(exc), "meters": meters_synced, "readings": readings_synced}


def _dedupe_dts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row.get("code")
        if code:
            seen[str(code)] = row
    return list(seen.values())


async def sync_loop() -> None:
    """Background refresher; errors are logged, never fatal to the server."""
    while True:
        try:
            await asyncio.sleep(max(1, settings.sync_interval_minutes) * 60)
            await run_sync(include_energy=settings.sync_energy_on_start)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("background sync iteration failed")
            await asyncio.sleep(60)


def bootstrap_if_empty() -> None:
    with store.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM meters").fetchone()
    if row["n"] == 0:
        log.info("index is empty; first sync begins in the background")
        asyncio.create_task(run_sync(include_energy=settings.sync_energy_on_start))
