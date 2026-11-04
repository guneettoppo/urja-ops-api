"""Consumption route.

The portal exposes **cumulative register readings**, not consumption. Every
number this endpoint returns is therefore a derived delta, and says so
(`derived: true`) so that no consumer mistakes it for a raw meter value.

Two of the portal's data behaviours make this non-trivial:

* granularity varies per meter (1800s for some, 86400s for others) with no way
  to request an interval, so asking for 30m on a daily meter is a 422 rather
  than a silently half-empty series;
* gaps and empty-string readings exist, so intervals that cannot be computed
  are dropped and counted in `meta.suppressedPoints`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Path, Query

from urja.db import store
from urja.errors import Problem, not_found
from urja.models import ConsumptionMeta, ConsumptionPoint, ConsumptionResponse
from urja.portal.parse import IST

router = APIRouter(tags=["consumption"])

MAX_POINTS = 5000


def _parse_bound(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Problem(
            422,
            "Invalid request",
            f"'{value}' is not an ISO-8601 date or timestamp.",
            type_="urn:urja:validation",
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment


def _parse_ts(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=IST)


@router.get(
    "/meters/{meter_id}/consumption",
    response_model=ConsumptionResponse,
    summary="Derived consumption series",
    description=(
        "Consumption computed as the difference between consecutive cumulative register "
        "readings.\n\n"
        "* `interval=auto` — one point per native reading interval (30m or 1d, per meter).\n"
        "* `interval=30m` — only valid for meters whose source is half-hourly; `422` otherwise.\n"
        "* `interval=1d` — points aggregated onto IST calendar days.\n\n"
        "Intervals where either register value is missing are omitted and counted in "
        "`meta.suppressedPoints`. A negative difference is emitted with "
        "`anomaly: \"register_reset\"` rather than silently clamped."
    ),
    responses={404: {"description": "No such meter"}, 422: {"description": "Interval finer than the source data"}},
)
def meter_consumption(
    meter_id: str = Path(..., description="Meter id, e.g. `J100000`."),
    interval: Literal["auto", "30m", "1d"] = Query(
        "auto", description="Output bucket size. `auto` follows the meter's native granularity."
    ),
    start: str | None = Query(None, alias="from", description="ISO-8601 lower bound (inclusive)."),
    end: str | None = Query(None, alias="to", description="ISO-8601 upper bound (inclusive)."),
) -> ConsumptionResponse:
    meter = store.get_meter(meter_id)
    if meter is None:
        raise not_found("meter", meter_id)

    native = meter.get("granularity_seconds")
    native_label = "30m" if native == 1800 else "1d" if native == 86400 else "other"

    if interval == "30m" and native is not None and native != 1800:
        raise Problem(
            422,
            "Interval finer than the source data",
            (
                f"Meter {meter_id} only has {native_label} readings in the portal; a 30m series "
                "would be fabricated. Use interval=auto or interval=1d."
            ),
            type_="urn:urja:granularity-mismatch",
            extra={"sourceGranularity": native_label, "requestedInterval": interval},
        )

    readings = store.get_readings(meter_id)
    lower, upper = _parse_bound(start), _parse_bound(end)
    if lower and upper and lower > upper:
        raise Problem(422, "Invalid request", "'from' must be earlier than 'to'.", type_="urn:urja:validation")

    points: list[dict] = []
    suppressed = 0
    previous: dict | None = None
    for reading in readings:
        moment = _parse_ts(reading["ts"])
        if previous is not None:
            delta_kwh = (
                None
                if reading["kwh"] is None or previous["kwh"] is None
                else round(reading["kwh"] - previous["kwh"], 4)
            )
            delta_kvah = (
                None
                if reading["kvah"] is None or previous["kvah"] is None
                else round(reading["kvah"] - previous["kvah"], 4)
            )
            if delta_kwh is None:
                suppressed += 1
            else:
                points.append(
                    {
                        "start": _parse_ts(previous["ts"]),
                        "end": moment,
                        "kwh": delta_kwh,
                        "kvah": delta_kvah,
                        "anomaly": "register_reset" if delta_kwh < 0 else None,
                    }
                )
        previous = reading

    if interval == "1d" and native_label != "1d":
        points = _aggregate_daily(points)

    if lower is not None:
        points = [p for p in points if p["end"] >= lower]
    if upper is not None:
        points = [p for p in points if p["end"] <= upper]

    truncated = len(points) > MAX_POINTS
    points = points[-MAX_POINTS:]

    return ConsumptionResponse(
        meterId=meter_id,
        interval=interval,
        data=[
            ConsumptionPoint(
                start=p["start"].isoformat(timespec="minutes"),
                end=p["end"].isoformat(timespec="minutes"),
                kwh=p["kwh"],
                kvah=p["kvah"],
                derived=True,
                anomaly=p["anomaly"],
            )
            for p in points
        ],
        meta=ConsumptionMeta(
            sourceGranularity=native_label,
            sourceTimezone="Asia/Kolkata",
            unit="kWh",
            dataEndsAt=meter.get("last_reading_at"),
            suppressedPoints=suppressed + (len(points) if truncated else 0),
        ),
    )


def _aggregate_daily(points: list[dict]) -> list[dict]:
    """Sum intervals onto IST calendar days.

    Day buckets are half-open: ``start`` is 00:00 IST and ``end`` is 00:00 IST
    the following day, which is unambiguous for consumers doing their own
    bucketing downstream.
    """
    buckets: dict[str, dict] = {}
    for point in points:
        key = point["end"].astimezone(IST).date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "kwh": point["kwh"],
                "kvah": point["kvah"],
                "anomaly": point["anomaly"],
            }
            continue
        bucket["kwh"] = round((bucket["kwh"] or 0) + (point["kwh"] or 0), 4)
        if point["kvah"] is not None:
            bucket["kvah"] = round((bucket["kvah"] or 0) + point["kvah"], 4)
        if point["anomaly"]:
            bucket["anomaly"] = point["anomaly"]

    ordered: list[dict] = []
    for key, bucket in sorted(buckets.items()):
        day_start = datetime.fromisoformat(key).replace(tzinfo=IST)
        ordered.append(
            {
                "start": day_start,
                "end": day_start + timedelta(days=1),
                "kwh": bucket["kwh"],
                "kvah": bucket["kvah"],
                "anomaly": bucket["anomaly"],
            }
        )
    return ordered
