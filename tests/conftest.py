"""Test fixtures: a tiny synthetic index, no network, no real database.

The API tests run against a temp SQLite file seeded with hand-built records, so
they stay fast and deterministic. Portal-response parsing is covered separately
with captured payloads in ``test_parse.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from urja.config import settings
from urja.db import store
from urja.portal.parse import detect_granularity_seconds, normalise_meter, normalise_readings, to_iso


def _export_row(meter_id: str, *, serial: str, dt: str, feeder: str, status: str = "Installed",
                phase: str = "single", make: str = "Genus", build: str = "legacy"):
    return {
        "meterId": meter_id,
        "serialNo": serial,
        "make": make,
        "phaseType": phase,
        "installStatus": status,
        "installType": "Whole Current",
        "build": build,
        "dtCode": dt,
        "hierarchy": {
            "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
            "circle": {"name": "Circle 1", "code": "C-01"},
            "division": {"name": "Division 1", "code": "D-01"},
            "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
            "substation": {"name": "Substation 1", "code": "SS-01"},
            "feeder": {"name": "Feeder 12", "code": feeder},
            "dt": {"name": "Malviya Nagar DT 5", "code": dt},
        },
        "geo": {"lat": 26.9, "lng": 75.8},
    }


def _half_hourly(start_kwh: float = 100.0):
    """Three readings, two computable intervals (+10.0, +15.5)."""
    return [
        {"timestamp": "24/06/2026 00:00", "kwh": str(start_kwh), "kvah": "110", "voltR": "230"},
        {"timestamp": "24/06/2026 00:30", "kwh": str(start_kwh + 10), "kvah": "121", "voltR": "231"},
        {"timestamp": "24/06/2026 01:00", "kwh": str(start_kwh + 25.5), "kvah": "140", "voltR": "229"},
    ]


def _daily():
    return [
        {"timestamp": "28/06/2026 00:00", "kwh": "500", "kvah": "550", "voltR": "230"},
        {"timestamp": "29/06/2026 00:00", "kwh": "512", "kvah": "563", "voltR": "232"},
        {"timestamp": "30/06/2026 00:00", "kwh": "521", "kvah": "573", "voltR": "228"},
    ]


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")
    store.init_db()

    dt_feeder = {"DT-005": "F-012", "DT-007": "F-013"}
    dt_names = {"DT-007": "Sanganer DT 7"}
    rows = [
        _export_row("J100000", serial="SE33962", dt="DT-005", feeder="F-012"),
        _export_row("J100001", serial="GE84132", dt="DT-007", feeder="F-013", status="Faulty", make="HPL"),
        # blank feeder code on the meter row: must be repaired from the DT list
        _export_row("J100002", serial="AL28136", dt="DT-005", feeder="", status="Decommissioned"),
    ]
    records = []
    for row in rows:
        record, _ = normalise_meter(row, dt_feeder=dt_feeder, dt_names=dt_names)
        records.append(record)

    with store.connect() as conn:
        store.upsert_meters(conn, records)
        store.upsert_dts(
            conn,
            [
                {"code": "DT-005", "name": "Malviya Nagar DT 5", "feederCode": "F-012", "capacityKva": 100},
                {"code": "DT-007", "name": "Sanganer DT 7", "feederCode": "F-013", "capacityKva": 63},
            ],
        )
        for meter_id, payload in (
            ("J100000", _half_hourly()),
            ("J100001", _daily()),
            ("J100002", _half_hourly(200.0)),
        ):
            readings, _ = normalise_readings(payload)
            store.replace_readings(conn, meter_id, readings)
            store.set_granularity(conn, meter_id, detect_granularity_seconds(readings))
        store.meta_set(conn, "last_successful_sync", to_iso())

    return tmp_path


@pytest.fixture()
def client(seeded_db):
    from urja.main import app

    # No `with` block: skipping the lifespan avoids starting a real background
    # sync against the portal during tests.
    return TestClient(app)
