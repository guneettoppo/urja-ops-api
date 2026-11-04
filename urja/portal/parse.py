"""Normalisation of the portal's raw payloads into our own shapes.

Everything the portal does oddly is handled *here*, once, so that no route and
no consumer ever sees it:

* timestamps are ``dd/MM/yyyy HH:MM`` in local (IST) time, not ISO, not UTC
* numbers arrive as strings, and a missing value is the empty string ``""``
* energy values are *cumulative register* readings, not interval consumption
* the hierarchy is a set of display strings with codes embedded in them
* some hierarchy codes are empty where a sibling endpoint knows the real value
* the same DT can carry two different names across endpoints
* the energy endpoint occasionally repeats its last timestamp with a null row
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# The portal displays naive local time. Jaipur discom (JVVNL) is IST; the data
# (Jaipur DT names) corroborates it. Recorded as an assumption in PROTOCOL.md.
IST = timezone(timedelta(hours=5, minutes=30))

# Root -> leaf, matching the order the portal itself displays.
EXPORT_LEVELS = ("zone", "circle", "division", "subdivision", "substation", "feeder", "dt")


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def to_iso(moment: datetime | None = None) -> str:
    """ISO-8601 (second precision) for the given moment, defaulting to now."""
    return (moment or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_portal_ts(value: str | None) -> datetime | None:
    """Parse the portal's ``dd/MM/yyyy HH:MM`` timestamp as IST.

    Returns ``None`` rather than raising so a single corrupt row cannot abort a
    whole sweep. ``strptime`` with an explicit format matters: parsing
    ``03/07/2026`` as US style would succeed and be silently wrong by months.
    """
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def clean(value: Any) -> str | None:
    """Collapse the portal's "missing" encodings (``""``, ``null``) to ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_float(value: Any) -> float | None:
    """Tolerant numeric cast; ``""`` and junk become ``None``, never raise."""
    text = clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def slug(value: str | None, *, default: str = "unknown") -> str:
    """``"CT Operated"`` -> ``"ct_operated"``; ``"Whole Current"`` -> ``"whole_current"``."""
    text = clean(value)
    if text is None:
        return default
    return text.strip().lower().replace("-", "_").replace(" ", "_")


# --------------------------------------------------------------------------- #
# meters
# --------------------------------------------------------------------------- #


def hierarchy_from_export(row: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Pull the structured hierarchy out of a bulk-export row."""
    raw = row.get("hierarchy") or {}
    result: dict[str, dict[str, str | None]] = {}
    for level in EXPORT_LEVELS:
        node = raw.get(level) or {}
        if isinstance(node, dict):
            result[level] = {"name": clean(node.get("name")), "code": clean(node.get("code"))}
        else:  # defensive: a string "Name (CODE)" occasionally shows up
            name, _, code = str(node).partition(" (")
            result[level] = {"name": clean(name), "code": clean(code.rstrip(")"))}
    return result


def normalise_meter(
    row: dict[str, Any],
    *,
    dt_feeder: dict[str, str] | None = None,
    dt_names: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Flatten one export row into a database-shaped record plus quality issues."""
    dt_feeder = dt_feeder or {}
    dt_names = dt_names or {}

    hierarchy = hierarchy_from_export(row)
    issues: list[str] = []

    dt_code = clean(row.get("dtCode")) or hierarchy.get("dt", {}).get("code")

    missing = sorted({lvl for lvl, node in hierarchy.items() if not node.get("code")})
    if missing:
        issues.append("EMPTY_HIERARCHY_CODE:" + ",".join(missing))

    # A blank feeder code is recoverable: the DT list knows which feeder the DT
    # hangs off. Repair it and say so, rather than passing the hole downstream.
    feeder = hierarchy.get("feeder") or {}
    if not feeder.get("code") and dt_code and dt_feeder.get(dt_code):
        feeder["code"] = dt_feeder[dt_code]
        issues.append("FEEDER_REPAIRED_FROM_DT")
    if not feeder.get("name") and feeder.get("code"):
        issues.append("FEEDER_NAME_MISSING")

    # The same DT can be named differently by /portal/dts and by the meter rows.
    dt_node = hierarchy.get("dt") or {}
    if dt_code and dt_names.get(dt_code) and dt_node.get("name") and dt_node["name"] != dt_names[dt_code]:
        issues.append("DT_NAME_CONFLICT")

    geo = row.get("geo") or {}
    record = {
        "meter_id": clean(row.get("meterId")),
        "serial_no": clean(row.get("serialNo")),
        "make": clean(row.get("make")),
        "phase_type": slug(row.get("phaseType")),
        "install_status": slug(row.get("installStatus")),
        "install_type": slug(row.get("installType")),
        "build": clean(row.get("build")),
        "dt_code": dt_code,
        "lat": to_float(geo.get("lat")),
        "lng": to_float(geo.get("lng")),
        "zone_code": hierarchy.get("zone", {}).get("code"),
        "zone_name": hierarchy.get("zone", {}).get("name"),
        "circle_code": hierarchy.get("circle", {}).get("code"),
        "circle_name": hierarchy.get("circle", {}).get("name"),
        "division_code": hierarchy.get("division", {}).get("code"),
        "division_name": hierarchy.get("division", {}).get("name"),
        "subdivision_code": hierarchy.get("subdivision", {}).get("code"),
        "subdivision_name": hierarchy.get("subdivision", {}).get("name"),
        "substation_code": hierarchy.get("substation", {}).get("code"),
        "substation_name": hierarchy.get("substation", {}).get("name"),
        "feeder_code": feeder.get("code"),
        "feeder_name": feeder.get("name"),
        "dt_name": dt_node.get("name"),
        "data_quality": issues,
    }
    return record, issues


# --------------------------------------------------------------------------- #
# readings
# --------------------------------------------------------------------------- #


def normalise_readings(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Clean one meter's energy series.

    Returns ``(readings, issues)`` where readings are sorted, de-duplicated and
    carry ISO timestamps. Null values are preserved as ``None`` so that gaps are
    visible rather than silently closed.
    """
    issues: list[str] = []
    by_ts: dict[datetime, dict[str, Any]] = {}
    nulls = 0
    duplicates = 0
    unparseable = 0

    for row in rows:
        ts = parse_portal_ts(row.get("timestamp"))
        if ts is None:
            unparseable += 1
            continue
        kwh, kvah, volt = to_float(row.get("kwh")), to_float(row.get("kvah")), to_float(row.get("voltR"))
        if kwh is None:
            nulls += 1
        candidate = {"ts": ts, "kwh": kwh, "kvah": kvah, "volt_r": volt}
        if ts in by_ts:
            duplicates += 1
            # The portal sometimes appends a copy of the last timestamp with an
            # empty reading. Keep whichever row actually has data.
            prev = by_ts[ts]
            if prev["kwh"] is None and kwh is not None:
                by_ts[ts] = candidate
            continue
        by_ts[ts] = candidate

    if duplicates:
        issues.append(f"DUPLICATE_TIMESTAMP:{duplicates}")
    if nulls:
        issues.append(f"NULL_READING:{nulls}")
    if unparseable:
        issues.append(f"UNPARSEABLE_TIMESTAMP:{unparseable}")

    ordered = [by_ts[k] for k in sorted(by_ts)]
    return [
        {
            "ts": item["ts"].isoformat(timespec="minutes"),
            "kwh": item["kwh"],
            "kvah": item["kvah"],
            "volt_r": item["volt_r"],
        }
        for item in ordered
    ], issues


def detect_granularity_seconds(readings: list[dict[str, Any]]) -> int | None:
    """Infer the native sampling interval (seconds) from the modal gap.

    The portal changes granularity *per meter* and offers no parameter to
    choose it: 40 meters are half-hourly, 363 are daily.
    """
    stamps = [datetime.fromisoformat(r["ts"]) for r in readings]
    if len(stamps) < 2:
        return None
    deltas = [
        int((stamps[i + 1] - stamps[i]).total_seconds())
        for i in range(len(stamps) - 1)
        if (stamps[i + 1] - stamps[i]).total_seconds() > 0
    ]
    if not deltas:
        return None
    return int(statistics.median(deltas))


def has_negative_delta(readings: list[dict[str, Any]]) -> bool:
    """A decreasing register means a meter reset/replacement, not production."""
    values = [r["kwh"] for r in readings if r["kwh"] is not None]
    return any(values[i + 1] < values[i] for i in range(len(values) - 1))
