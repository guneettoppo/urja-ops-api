"""API behaviour tests against a seeded temp database (no network)."""

from __future__ import annotations

import pytest


def test_lists_all_seeded_meters(client):
    response = client.get("/meters")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 3
    assert [row["meterId"] for row in body["data"]] == ["J100000", "J100001", "J100002"]


def test_status_filter(client):
    body = client.get("/meters", params={"status": "faulty"}).json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["meterId"] == "J100001"


def test_unknown_status_is_a_problem_json_422(client):
    response = client.get("/meters", params={"status": "exploded"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:urja:validation"


def test_feeder_filter_includes_repaired_meter(client):
    """J100002 arrived with a blank feeder code and was repaired from its DT."""
    body = client.get("/meters", params={"feeder": "F-012"}).json()
    assert {row["meterId"] for row in body["data"]} == {"J100000", "J100002"}


def test_substring_search_on_serial(client):
    body = client.get("/meters", params={"q": "GE84"}).json()
    assert [row["meterId"] for row in body["data"]] == ["J100001"]


def test_keyset_pagination_walks_the_whole_set_without_duplicates(client):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/meters", params=params).json()
        pages += 1
        seen.extend(row["meterId"] for row in body["data"])
        cursor = body["pageInfo"]["nextCursor"]
        if not cursor:
            break
    assert seen == ["J100000", "J100001", "J100002"]
    assert pages == 3


def test_meter_detail_includes_hierarchy_and_quality_notes(client):
    body = client.get("/meters/J100002").json()
    assert body["hierarchy"]["feeder"]["code"] == "F-012"
    assert body["hierarchy"]["zone"]["name"] == "Jaipur Zone 1"
    assert body["location"]["latitude"] is not None
    assert "FEEDER_REPAIRED_FROM_DT" in body["dataQuality"]


def test_missing_meter_is_404_problem_json(client):
    response = client.get("/meters/J999999")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404


# --------------------------------------------------------------------------- #
# consumption
# --------------------------------------------------------------------------- #


def test_consumption_returns_deltas_not_registers(client):
    body = client.get("/meters/J100000/consumption").json()
    values = [point["kwh"] for point in body["data"]]
    assert values == [10.0, 15.5]  # 100 -> 110 -> 125.5
    assert all(point["derived"] is True for point in body["data"])
    assert body["meta"]["sourceGranularity"] == "30m"


def test_consumption_marks_register_resets_instead_of_hiding_them(client):
    # A decreasing register is a meter reset; the delta must be visible and flagged.
    from urja.db import store
    from urja.portal.parse import normalise_readings

    readings, _ = normalise_readings(
        [
            {"timestamp": "24/06/2026 00:00", "kwh": "100", "kvah": "1", "voltR": "230"},
            {"timestamp": "24/06/2026 00:30", "kwh": "5", "kvah": "1", "voltR": "230"},
        ]
    )
    with store.connect() as conn:
        store.replace_readings(conn, "J100000", readings)

    body = client.get("/meters/J100000/consumption").json()
    assert body["data"][0]["kwh"] == -95.0
    assert body["data"][0]["anomaly"] == "register_reset"


def test_daily_meter_reports_its_own_granularity(client):
    body = client.get("/meters/J100001/consumption").json()
    assert body["meta"]["sourceGranularity"] == "1d"
    assert [p["kwh"] for p in body["data"]] == [12.0, 9.0]


def test_asking_30m_of_a_daily_meter_is_rejected_not_faked(client):
    response = client.get("/meters/J100001/consumption", params={"interval": "30m"})
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"] == "urn:urja:granularity-mismatch"
    assert problem["sourceGranularity"] == "1d"


def test_daily_aggregation_of_a_half_hourly_meter(client):
    body = client.get("/meters/J100000/consumption", params={"interval": "1d"}).json()
    assert len(body["data"]) == 1
    assert body["data"][0]["kwh"] == 25.5
    assert body["data"][0]["start"].startswith("2026-06-24T00:00")
    assert body["data"][0]["end"].startswith("2026-06-25T00:00")


def test_consumption_of_missing_meter_is_404(client):
    assert client.get("/meters/nope/consumption").status_code == 404


# --------------------------------------------------------------------------- #
# network + system
# --------------------------------------------------------------------------- #


def _walk(nodes, collected=None):
    collected = collected if collected is not None else []
    for node in nodes:
        collected.append(node)
        _walk(node["children"], collected)
    return collected


def test_hierarchy_counts_cover_every_meter(client):
    body = client.get("/network/hierarchy").json()
    assert body["depth"] == "dt"
    zone = body["roots"][0]
    assert zone["code"] == "Z-01"
    assert zone["meters"] == 3
    # counts must be conserved all the way down to the leaves
    leaves = [node for node in _walk(body["roots"]) if not node["children"]]
    assert sum(node["meters"] for node in leaves) == 3


def test_hierarchy_can_be_trimmed_to_feeders(client):
    body = client.get("/network/hierarchy", params={"to": "feeder"}).json()
    codes = {node["code"] for node in _walk(body["roots"])}
    assert "F-012" in codes
    assert not any(code and code.startswith("DT-") for code in codes)
    assert all(node["children"] == [] for node in _walk(body["roots"]) if node["code"] == "F-012")


def test_health_reports_index_size(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["indexedMeters"] == 3


def test_sync_status_reports_freshness(client):
    body = client.get("/sync/status").json()
    assert body["dataAgeSeconds"] is not None
    assert body["counts"]["meters"] == 3
    assert body["counts"]["lastReadingAt"].startswith("2026-06-30")


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/health"])
def test_openapi_and_docs_are_served(client, path):
    assert client.get(path).status_code == 200
