"""Unit tests for the portal-side parsers and the request signer.

These encode the specific quirks discovered while reverse-engineering the
portal, so a future change to the portal surfaces here as a failing test rather
than as wrong numbers in the API.
"""

from __future__ import annotations

import hmac
import hashlib

import pytest

from urja.portal.parse import (
    detect_granularity_seconds,
    normalise_meter,
    normalise_readings,
    parse_portal_ts,
    slug,
    to_float,
)
from urja.portal.sign import export_headers, sign_request


# --------------------------------------------------------------------------- #
# timestamps and numbers
# --------------------------------------------------------------------------- #


def test_day_first_dates_are_not_parsed_as_us():
    # 03/07/2026 is 3 July. A "%m/%d/%Y" parse would silently produce March 7th.
    parsed = parse_portal_ts("03/07/2026 00:00")
    assert moment_date(parsed) == (2026, 7, 3)
    assert parse_portal_ts("30/06/2026 23:30").month == 6


def parse_portal_ts_or_raise(value: str):
    parsed = parse_portal_ts(value)
    assert parsed is not None, f"failed to parse {value!r}"
    return parsed


def moment_date(moment):
    assert moment is not None
    return (moment.year, moment.month, moment.day)


def test_timestamp_carries_ist_offset():
    parsed = parse_portal_ts_or_raise("24/06/2026 00:30")
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600


def test_blank_timestamp_is_none_not_an_exception():
    assert parse_portal_ts("") is None
    assert parse_portal_ts(None) is None
    assert parse_portal_ts("not a date") is None


@pytest.mark.parametrize("raw,expected", [("12.5", 12.5), ("", None), (None, None), ("junk", None), ("0", 0.0)])
def test_tolerant_numeric_cast(raw, expected):
    assert to_float(raw) == expected


def test_slug_normalises_installation_types():
    assert slug("CT Operated") == "ct_operated"
    assert slug("Whole Current") == "whole_current"
    assert slug(None) == "unknown"


# --------------------------------------------------------------------------- #
# readings
# --------------------------------------------------------------------------- #


def test_trailing_duplicate_null_row_is_discarded():
    """Five meters return a 9th row repeating the last timestamp with empty values."""
    rows = [
        {"timestamp": "29/06/2026 00:00", "kwh": "12678.83", "kvah": "13693.14", "voltR": "225"},
        {"timestamp": "30/06/2026 00:00", "kwh": "12686.48", "kvah": "13701.39", "voltR": "225"},
        {"timestamp": "30/06/2026 00:00", "kwh": "", "kvah": "13701.39", "voltR": ""},
    ]
    readings, issues = normalise_readings(rows)
    assert len(readings) == 2
    assert readings[-1]["kwh"] == 12686.48
    assert any(issue.startswith("DUPLICATE_TIMESTAMP") for issue in issues)


def test_duplicate_row_with_data_wins_over_null_row():
    rows = [
        {"timestamp": "30/06/2026 00:00", "kwh": "", "kvah": "", "voltR": ""},
        {"timestamp": "30/06/2026 00:00", "kwh": "42.0", "kvah": "45.0", "voltR": "230"},
    ]
    readings, _ = normalise_readings(rows)
    assert len(readings) == 1
    assert readings[0]["kwh"] == 42.0


def test_granularity_detection():
    half_hourly = [
        {"ts": "2026-06-24T00:00+05:30", "kwh": 1.0, "kvah": None, "volt_r": None},
        {"ts": "2026-06-24T00:30+05:30", "kwh": 2.0, "kvah": None, "volt_r": None},
        {"ts": "2026-06-24T01:00+05:30", "kwh": 3.0, "kvah": None, "volt_r": None},
    ]
    daily = [
        {"ts": "2026-06-29T00:00+05:30", "kwh": 1.0, "kvah": None, "volt_r": None},
        {"ts": "2026-06-30T00:00+05:30", "kwh": 2.0, "kvah": None, "volt_r": None},
    ]
    assert detect_granularity_seconds(half_hourly) == 1800
    assert detect_granularity_seconds(daily) == 86400
    assert detect_granularity_seconds(daily[:1]) is None


# --------------------------------------------------------------------------- #
# meters
# --------------------------------------------------------------------------- #


def _row(**overrides):
    row = {
        "meterId": "J100044",
        "serialNo": "GE12345",
        "make": "Genus",
        "phaseType": "single",
        "installStatus": "Installed",
        "installType": "CT Operated",
        "build": "v2",
        "dtCode": "DT-005",
        "hierarchy": {
            "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
            "circle": {"name": "", "code": ""},
            "division": {"name": "Division 5", "code": "D-05"},
            "subdivision": {"name": "Subdivision 5", "code": "SD-05"},
            "substation": {"name": "Substation 5", "code": "SS-05"},
            "feeder": {"name": "", "code": ""},
            "dt": {"name": "Bani Park DT 5", "code": "DT-005"},
        },
        "geo": {"lat": "26.9", "lng": "75.8"},
    }
    row.update(overrides)
    return row


def test_blank_feeder_is_repaired_from_the_transformer_list():
    record, issues = normalise_meter(_row(), dt_feeder={"DT-005": "F-005"})
    assert record["feeder_code"] == "F-005"
    assert "FEEDER_REPAIRED_FROM_DT" in issues


def test_blank_hierarchy_fields_are_reported_not_invented():
    record, issues = normalise_meter(_row(), dt_feeder={"DT-005": "F-005"})
    assert record["circle_code"] is None
    assert any(issue.startswith("EMPTY_HIERARCHY_CODE") for issue in issues)


def test_conflicting_dt_names_are_flagged():
    record, issues = normalise_meter(
        _row(
            dtCode="DT-007",
            hierarchy={**_row()["hierarchy"], "dt": {"name": "Old Malviya Nagar Xfmr", "code": "DT-007"}},
        ),
        dt_names={"DT-007": "Sanganer DT 7"},
    )
    assert "DT_NAME_CONFLICT" in issues
    assert record["dt_name"] == "Old Malviya Nagar Xfmr"


def test_status_and_phase_are_slugged():
    record, _ = normalise_meter(_row(installStatus="Decommissioned", phaseType="three"))
    assert record["install_status"] == "decommissioned"
    assert record["phase_type"] == "three"


# --------------------------------------------------------------------------- #
# request signing
# --------------------------------------------------------------------------- #


def test_signature_matches_known_vector():
    """Known-answer test: METHOD\\nPATH\\nQUERY\\nTIMESTAMP, HMAC-SHA256, lowercase hex."""
    stamp, signature = sign_request("secret", "GET", "/portal/export", "page=1", timestamp=1700000000)
    assert stamp == "1700000000"
    assert signature == "81340cd1e31e597cb066514f51f103416827265923e80f700aefb1b64dc6a3b9"


def test_signature_covers_the_query_string():
    """Changing a parameter must change the signature; the portal binds both."""
    _, first = sign_request("secret", "GET", "/portal/export", "page=1", timestamp=1700000000)
    _, second = sign_request("secret", "GET", "/portal/export", "page=2", timestamp=1700000000)
    assert first != second


def test_export_headers_contain_both_required_fields():
    headers = export_headers("secret")
    assert set(headers) == {"x-timestamp", "x-signature"}
    assert len(headers["x-signature"]) == 64


def test_signature_is_lowercase_hex():
    _, signature = sign_request("secret", "GET", "/portal/export", "", timestamp=1)
    assert signature == signature.lower()
    int(signature, 16)  # raises if not hex
