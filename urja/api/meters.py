"""Meter listing and detail routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from urja.db import store
from urja.errors import Problem, not_found
from urja.models import (
    HierarchyNode,
    MeterDetail,
    MeterHierarchy,
    MeterListResponse,
    MeterSummary,
    PageInfo,
    ReadingSummary,
    ResponseMeta,
)
from urja.portal.parse import to_iso

router = APIRouter(tags=["meters"])

ALLOWED_STATUS = {"installed", "faulty", "decommissioned"}


def _granularity_label(seconds: int | None) -> str:
    if seconds is None:
        return "none"
    if seconds == 1800:
        return "30m"
    if seconds == 86400:
        return "1d"
    return "other"


def _node(code: str | None, name: str | None) -> HierarchyNode | None:
    if not code and not name:
        return None
    return HierarchyNode(code=code, name=name)


def _reading_summary(row: dict) -> ReadingSummary:
    return ReadingSummary(
        firstReadingAt=row.get("first_reading_at"),
        lastReadingAt=row.get("last_reading_at"),
        count=row.get("reading_count") or 0,
        granularitySeconds=row.get("granularity_seconds"),
        granularity=_granularity_label(row.get("granularity_seconds")),
    )


def _summary(row: dict) -> MeterSummary:
    return MeterSummary(
        meterId=row["meter_id"],
        serial=row.get("serial_no"),
        make=row.get("make"),
        phase=row.get("phase_type") if row.get("phase_type") in {"single", "three"} else "unknown",
        status=row.get("install_status") or "unknown",
        installation=row.get("install_type"),
        dtCode=row.get("dt_code"),
        feederCode=row.get("feeder_code"),
        readingSummary=_reading_summary(row),
    )


def _detail(row: dict) -> MeterDetail:
    hierarchy = MeterHierarchy(
        zone=_node(row.get("zone_code"), row.get("zone_name")),
        circle=_node(row.get("circle_code"), row.get("circle_name")),
        division=_node(row.get("division_code"), row.get("division_name")),
        subdivision=_node(row.get("subdivision_code"), row.get("subdivision_name")),
        substation=_node(row.get("substation_code"), row.get("substation_name")),
        feeder=_node(row.get("feeder_code"), row.get("feeder_name")),
        dt=_node(row.get("dt_code"), row.get("dt_name")),
    )
    location = None
    if row.get("lat") is not None and row.get("lng") is not None:
        location = {"latitude": row["lat"], "longitude": row["lng"]}
    return MeterDetail(
        **_summary(row).model_dump(by_alias=True),
        hierarchy=hierarchy,
        location=location,
        dataQuality=row.get("data_quality") or [],
        syncedAt=row.get("synced_at"),
    )


@router.get(
    "/meters",
    response_model=MeterListResponse,
    summary="List and filter meters",
    description=(
        "Filter the indexed meter population. Every filter is applied server-side against "
        "the local index — the portal itself only supports a substring search over meter id "
        "and serial number.\n\n"
        "Pagination is keyset-based: pass `pageInfo.nextCursor` back as `cursor`."
    ),
)
def list_meters(
    q: str | None = Query(None, description="Substring match on meter id or serial number."),
    status: str | None = Query(None, description="`installed`, `faulty` or `decommissioned`."),
    make: str | None = Query(None, description="Meter manufacturer, case-insensitive (e.g. `Genus`)."),
    phase: Literal["single", "three"] | None = Query(None, description="Supply phase."),
    feeder: str | None = Query(None, description="Feeder code, e.g. `F-012`."),
    dt: str | None = Query(None, description="Distribution transformer code, e.g. `DT-005`."),
    limit: int = Query(50, ge=1, le=200, description="Page size."),
    cursor: str | None = Query(None, description="`pageInfo.nextCursor` from the previous page."),
) -> MeterListResponse:
    if status and status.lower() not in ALLOWED_STATUS:
        raise Problem(
            422,
            "Invalid request",
            f"Unknown status '{status}'.",
            type_="urn:urja:validation",
            extra={"allowed": sorted(ALLOWED_STATUS)},
        )

    rows, next_cursor, total = store.list_meters(
        q=q,
        status=status,
        make=make,
        phase=phase,
        feeder=feeder,
        dt=dt,
        limit=limit,
        cursor=cursor,
    )
    return MeterListResponse(
        data=[_summary(row) for row in rows],
        pageInfo=PageInfo(nextCursor=next_cursor, limit=limit),
        meta=ResponseMeta(generatedAt=to_iso(), dataAgeSeconds=store.data_age_seconds(), total=total),
    )


@router.get(
    "/meters/{meter_id}",
    response_model=MeterDetail,
    summary="Meter detail",
    description=(
        "Full attributes, network position and data-quality notes for one meter. "
        "Returns `404` when the meter is not in the index — distinct from `503`, "
        "which means the portal itself is unreachable."
    ),
    responses={404: {"description": "No such meter"}, 503: {"description": "Portal unavailable"}},
)
def get_meter(meter_id: str) -> MeterDetail:
    row = store.get_meter(meter_id)
    if row is None:
        raise not_found("meter", meter_id)
    return _detail(row)
