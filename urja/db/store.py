"""SQLite access layer.

One connection per operation (sqlite3 is cheap to open and this dataset is
~17k rows); WAL so a background sync never blocks readers.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from urja.config import settings
from urja.portal.parse import to_iso

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> Path:
    """Absolute path of the index file, creating its parent directory."""
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with sqlite3.connect(get_db_path()) as conn:
        conn.executescript(SCHEMA_SQL)


def seed_if_empty(seed_path: str | Path | None = None) -> bool:
    """Copy a prebuilt database into place when the index is empty.

    Only appropriate because the source data is frozen: on a host with an
    ephemeral filesystem this turns a four-minute portal sweep into a file copy.
    """
    seed = Path(seed_path) if seed_path is not None else Path(settings.seed_db_path)
    target = get_db_path()
    if target.exists() and target.stat().st_size > 0:
        with sqlite3.connect(target) as conn:
            try:
                count = conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0]
            except sqlite3.Error:
                count = 0
        if count:
            return False
    if not seed.exists() or seed.resolve() == target.resolve():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(target) + suffix)
        if leftover.exists():
            leftover.unlink()
    shutil.copyfile(seed, target)
    return True


def mark_interrupted_runs() -> int:
    """Close out runs left 'running' by a process that no longer exists.

    This service assumes a single container, so a run marked in-flight at
    startup must belong to a previous process that was killed mid-sync. Without
    this, a redeploy during a sync leaves a phantom lock that blocks new syncs
    for the full stale-run window.
    """
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE sync_runs SET state='interrupted', finished_at=?, "
            "detail=COALESCE(detail, 'process exited before the run completed') "
            "WHERE state='running'",
            (_now_iso(),),
        )
        return cursor.rowcount


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --------------------------------------------------------------------------- #
# writes (sync path)
# --------------------------------------------------------------------------- #

METER_COLUMNS: Sequence[str] = (
    "meter_id", "serial_no", "make", "phase_type", "install_status", "install_type",
    "build", "dt_code", "dt_name", "feeder_code", "feeder_name", "substation_code",
    "substation_name", "subdivision_code", "subdivision_name", "division_code",
    "division_name", "circle_code", "circle_name", "zone_code", "zone_name",
    "lat", "lng", "data_quality", "first_reading_at", "last_reading_at",
    "reading_count", "granularity_seconds", "synced_at",
)


def upsert_meters(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        payload = dict(record)
        issues = payload.pop("data_quality", [])
        payload["data_quality"] = json.dumps(issues)
        payload.setdefault("synced_at", to_iso())
        # NOT NULL columns: a freshly exported row has no readings yet.
        if payload.get("reading_count") is None:
            payload["reading_count"] = 0
        row = {column: payload.get(column) for column in METER_COLUMNS}
        placeholders = ", ".join("?" for _ in METER_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in METER_COLUMNS if c != "meter_id")
        conn.execute(
            f"INSERT INTO meters({', '.join(METER_COLUMNS)}) VALUES({placeholders}) "
            f"ON CONFLICT(meter_id) DO UPDATE SET {updates}",
            tuple(row[c] for c in METER_COLUMNS),
        )
        count += 1
    return count


def upsert_dts(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        conn.execute(
            "INSERT INTO dts(code, name, feeder_code, capacity_kva) VALUES(?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
            "feeder_code=excluded.feeder_code, capacity_kva=excluded.capacity_kva",
            (row.get("code"), row.get("name"), row.get("feederCode"), row.get("capacityKva")),
        )
        count += 1
    return count


def replace_readings(conn: sqlite3.Connection, meter_id: str, readings: Sequence[dict[str, Any]]) -> int:
    """Replace one meter's series atomically, then refresh its summary columns."""
    conn.execute("DELETE FROM readings WHERE meter_id=?", (meter_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO readings(meter_id, ts, kwh, kvah, volt_r) VALUES(?,?,?,?,?)",
        [(meter_id, r["ts"], r["kwh"], r["kvah"], r["volt_r"]) for r in readings],
    )
    if readings:
        conn.execute(
            "UPDATE meters SET first_reading_at=?, last_reading_at=?, reading_count=? "
            "WHERE meter_id=?",
            (readings[0]["ts"], readings[-1]["ts"], len(readings), meter_id),
        )
    else:
        conn.execute(
            "UPDATE meters SET first_reading_at=NULL, last_reading_at=NULL, reading_count=0 "
            "WHERE meter_id=?",
            (meter_id,),
        )
    return len(readings)


def set_granularity(conn: sqlite3.Connection, meter_id: str, seconds: int | None) -> None:
    conn.execute("UPDATE meters SET granularity_seconds=? WHERE meter_id=?", (seconds, meter_id))


# --------------------------------------------------------------------------- #
# reads (API path)
# --------------------------------------------------------------------------- #


def _row_to_meter(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["data_quality"] = json.loads(record.get("data_quality") or "[]")
    return record


def list_meters(
    *,
    q: str | None = None,
    status: str | None = None,
    make: str | None = None,
    phase: str | None = None,
    feeder: str | None = None,
    dt: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, int]:
    where: list[str] = []
    params: list[Any] = []

    if q:
        where.append("(meter_id LIKE ? OR serial_no LIKE ?)")
        needle = f"%{q.strip()}%"
        params.extend([needle, needle])
    if status:
        where.append("install_status = ?")
        params.append(status.lower())
    if make:
        where.append("make = ? COLLATE NOCASE")
        params.append(make)
    if phase:
        where.append("phase_type = ?")
        params.append(phase)
    if feeder:
        where.append("feeder_code = ?")
        params.append(feeder)
    if dt:
        where.append("dt_code = ?")
        params.append(dt)

    base = f"WHERE {' AND '.join(where)}" if where else ""

    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM meters {base}", params).fetchone()["n"]

        page_where = list(where)
        page_params = list(params)
        if cursor:
            page_where.append("meter_id > ?")
            page_params.append(cursor)
        clause = f"WHERE {' AND '.join(page_where)}" if page_where else ""
        rows = conn.execute(
            f"SELECT * FROM meters {clause} ORDER BY meter_id LIMIT ?",
            (*page_params, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1]["meter_id"] if has_more and rows else None
    return [_row_to_meter(r) for r in rows], next_cursor, total


def get_meter(meter_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM meters WHERE meter_id=?", (meter_id,)).fetchone()
    return _row_to_meter(row) if row else None


def get_readings(meter_id: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    where = ["meter_id = ?"]
    params: list[Any] = [meter_id]
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT ts, kwh, kvah, volt_r FROM readings WHERE {' AND '.join(where)} ORDER BY ts",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_dts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM dts ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def hierarchy_counts() -> dict[str, Any]:
    """Aggregate meters by hierarchy path; the portal gives paths, not a tree."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT zone_code, zone_name, circle_code, circle_name, division_code, division_name, "
            "subdivision_code, subdivision_name, substation_code, substation_name, "
            "feeder_code, feeder_name, dt_code, dt_name, COUNT(*) AS meters "
            "FROM meters GROUP BY zone_code, circle_code, division_code, subdivision_code, "
            "substation_code, feeder_code, dt_code ORDER BY zone_code, circle_code, division_code, "
            "subdivision_code, substation_code, feeder_code, dt_code"
        ).fetchall()
    return {"paths": [dict(r) for r in rows]}


def counts() -> dict[str, Any]:
    with connect() as conn:
        meters = conn.execute("SELECT COUNT(*) AS n FROM meters").fetchone()["n"]
        readings = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        bounds = conn.execute(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM readings"
        ).fetchone()
        with_quality = conn.execute(
            "SELECT COUNT(*) AS n FROM meters WHERE data_quality != '[]'"
        ).fetchone()["n"]
    return {
        "meters": meters,
        "readings": readings,
        "firstReadingAt": bounds["first"],
        "lastReadingAt": bounds["last"],
        "metersWithQualityNotes": with_quality,
    }


def get_counts() -> dict[str, Any]:
    """Flat counter dict used by /health and /sync/status."""
    data = counts()
    return {
        "count": data["meters"],
        "readings": data["readings"],
        "first_reading_at": data["firstReadingAt"],
        "last_reading_at": data["lastReadingAt"],
        "with_quality_notes": data["metersWithQualityNotes"],
    }


def meta_last_success() -> str | None:
    with connect() as conn:
        return meta_get(conn, "last_successful_sync")


# --------------------------------------------------------------------------- #
# sync bookkeeping
# --------------------------------------------------------------------------- #


def start_sync_run() -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs(started_at, state) VALUES(?, 'running')", (to_iso(),)
        )
        return int(cur.lastrowid)


def finish_sync_run(
    run_id: int, *, state: str, meters: int, readings: int, failures: int, detail: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sync_runs SET finished_at=?, state=?, meters_synced=?, readings_synced=?, "
            "failures=?, detail=? WHERE id=?",
            (to_iso(), state, meters, readings, failures, detail, run_id),
        )
        if state in {"ok", "partial"}:
            meta_set(conn, "last_successful_sync", to_iso())


def latest_sync_run() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def data_age_seconds() -> int | None:
    """Seconds since our index last finished a successful sync."""
    with connect() as conn:
        stamp = meta_get(conn, "last_successful_sync")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int((datetime.now(tz=timezone.utc) - moment).total_seconds())
