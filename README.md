# Urja Ops API

A clean, documented REST API over the legacy **Urja Meter Ops** portal — an
ageing internal web app that distribution-utility staff use to look up smart
meters, their position in the network, and their consumption.

The portal has no API of its own. This service logs into it, indexes its data
into SQLite, and serves a typed, filterable, documented surface on top. Nothing
downstream ever has to hold a portal session, sign an HMAC request, or parse a
`dd/MM/yyyy` timestamp.

```
                    ┌──────────────────────────────┐
   consumers ──────▶│  Urja Ops API  (FastAPI)     │
   curl / SDK /     │                              │
   dashboard        │  /meters  /consumption       │
                    │  /network /sync/status       │
                    └───────────┬──────────────────┘
                                │ reads
                    ┌───────────▼──────────────────┐
                    │  SQLite index (17k rows)     │
                    └───────────▲──────────────────┘
                                │ writes
                    ┌───────────┴──────────────────┐
                    │  sync job (paced)            │
                    │  export + 403 energy calls   │
                    └───────────┬──────────────────┘
                                │ logs in, signs HMAC
                    ┌───────────▼──────────────────┐
                    │  Urja Meter Ops portal       │
                    │  (SvelteKit, no public API)  │
                    └──────────────────────────────┘
```

---

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). No Docker needed.

```bash
git clone <this-repo> && cd urja-ops-api

cp .env.example .env          # then set PORTAL_EMAIL / PORTAL_PASSWORD
uv sync                       # install dependencies

uv run python -m urja.sync    # build the index (~4 min cold, 1 export + 403 calls)
uv run uvicorn urja.main:app --port 8081
```

Open **http://localhost:8081/docs** for interactive docs, or
**http://localhost:8081/openapi.json** for the machine-readable spec.

> Port 8080 is the container's port; use 8081 locally if 8080 is taken.
> The service also syncs in the background on boot if the index is empty, so
> `uvicorn` alone works — the first sync just runs while you browse `/docs`.

### Containerised

```bash
docker build -t urja-ops-api .
docker run -p 8081:8080 --env-file .env -v urja-data:/data urja-ops-api
```

---

## Sample requests

List faulty meters on a feeder — a query the portal cannot answer at all:

```bash
curl "localhost:8081/meters?status=faulty&feeder=F-012"
```

```json
{
  "data": [
    {
      "meterId": "J100004",
      "serial": "SE65293",
      "make": "Genus",
      "phase": "single",
      "status": "faulty",
      "installation": "ct_operated",
      "dtCode": "DT-005",
      "feederCode": "F-005",
      "readingSummary": {
        "firstReadingAt": "2026-06-23T23:30+05:30",
        "lastReadingAt": "2026-06-30T23:30+05:30",
        "count": 337,
        "granularitySeconds": 1800,
        "granularity": "30m"
      }
    }
  ],
  "pageInfo": { "nextCursor": null, "limit": 50 },
  "meta": { "generatedAt": "2026-11-04T17:41:02+00:00", "dataAgeSeconds": 12, "total": 1 }
}
```

Consumption as **deltas**, not registers:

```bash
curl "localhost:8081/meters/J100000/consumption?interval=1d"
```

```json
{
  "meterId": "J100000",
  "interval": "1d",
  "data": [
    { "start": "2026-06-24T00:00+05:30", "end": "2026-06-25T00:00+05:30",
      "kwh": 20.24, "kvah": 21.87, "derived": true, "anomaly": null }
  ],
  "meta": {
    "sourceGranularity": "30m",
    "sourceTimezone": "Asia/Kolkata",
    "unit": "kWh",
    "dataEndsAt": "2026-06-30T23:30+05:30",
    "suppressedPoints": 0
  }
}
```

The network tree, reconstructed from per-meter paths:

```bash
curl "localhost:8081/network/hierarchy?to=feeder"
```

```json
{
  "depth": "feeder",
  "roots": [
    { "code": "Z-01", "name": "Jaipur Zone 1", "meters": 141,
      "children": [
        { "code": "C-01", "name": "Circle 1", "meters": 58,
          "children": [ { "code": "F-001", "name": "Feeder 1", "meters": 12, "children": [] } ] }
      ] }
  ]
}
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | service liveness (does not call the portal) |
| GET | `/meters` | list/filter: `q`, `status`, `make`, `phase`, `feeder`, `dt`, `limit`, `cursor` |
| GET | `/meters/{id}` | full detail, hierarchy, data-quality notes |
| GET | `/meters/{id}/consumption` | derived consumption: `interval=auto\|30m\|1d`, `from`, `to` |
| GET | `/network/hierarchy` | zone → … → DT tree with meter counts, `to=` trims depth |
| GET | `/network/feeders/{code}/meters` | meters on a feeder |
| GET | `/sync/status` | index freshness, last run, progress |
| POST | `/sync` | trigger a sync now (`202`, `409` if running) |
| GET | `/docs`, `/openapi.json` | rendered and machine-readable spec |

Errors are RFC 9457 `application/problem+json` throughout:

```json
{ "type": "urn:urja:granularity-mismatch", "title": "Interval finer than the source data",
  "status": 422, "detail": "Meter J100050 only has 1d readings in the portal; a 30m series would be fabricated.",
  "instance": "/meters/J100050/consumption", "sourceGranularity": "1d" }
```

Deliberately distinct: **`404`** means the meter does not exist; **`503`** means
the portal is unreachable and the request is worth retrying (`Retry-After` set).

---

## Assumptions

1. **Portal timestamps are IST (UTC+05:30).** The payload carries no timezone.
   Inferred from day-first formatting, Jaipur transformer names, and an Indian
   discom deployment. Recorded in `PROTOCOL.md` §8 as the biggest open risk.
2. **The signed export is an intended integration path**, not a workaround: the
   portal hands its own frontend the signing secret from `/portal/keys`.
3. **The index may be stale; it must never be wrong about being stale.** Every
   response carries `meta.dataAgeSeconds`, and `/sync/status` additionally
   reports how old the *underlying data* is — which is months.
4. **A meter's `dtCode` is authoritative** for its transformer. It matched
   `hierarchy.dt.code` on all 403 rows.
5. **No multi-tenancy, no PII.** Meter IDs and geography only; no consumer names.

---

## Design decisions & trade-offs

| Decision | Alternative | Why |
|---|---|---|
| **Local SQLite index** | Thin pass-through proxy | The portal supports no filtering beyond a substring on id/serial, so most useful questions would have to be answered by fetching everything. The export is one call, so indexing is cheap. |
| **One signed bulk export** | Paginate `/portal/meters/search` 21 times | Same data, one request, plus `hierarchy` and `geo` that search omits. |
| **Consumption computed as deltas** | Expose raw registers | Registers are what the meter stores, not what anyone wants. Deltas are flagged `derived: true` so the provenance is explicit. |
| **`422` for 30m on a daily meter** | Return whatever points exist | A silently sparse series looks like "the meter stopped reporting", which is a different and alarming conclusion. Better to refuse than mislead. |
| **Negative deltas surfaced with `anomaly`** | Clamp to zero | A register going backwards is a meter reset — operationally important, and hiding it fabricates energy. |
| **Keyset pagination** | `OFFSET` | Stable under concurrent syncs and cheap on an indexed primary key. |
| **Background sync, serve stale** | Fail requests when portal is down | The dataset changes rarely; availability of the *service* matters more than 60-second freshness. |
| **Schema-typed responses (Pydantic)** | Hand-written OpenAPI | The spec is generated from the same objects that serialise responses, so it cannot drift. |
| **No ORM** | SQLAlchemy | Three tables, ten queries. An ORM would be more code than the schema. |
| **Single in-process sync loop** | Celery/Redis queue | At 17k rows a broker is infrastructure with no payoff. |

---

## Intentionally not built

* **Authentication on this API.** It is an internal service with no tenants and
  no PII. A gateway or mTLS is the right control; a half-built API-key check
  would be security theatre. Called out again in `REFLECTION.md`.
* **A web client.** The most enjoyable optional extra and the least informative
  for a reviewer judging systems thinking. `openapi.json` is there for anyone
  who wants to build one.
* **Incremental sync.** Every run refetches all 403 energy series (~4 min).
  Correct and simple; wasteful. See "with more time".
* **`/portal/meters/{id}/geo` and the `__data.json` detail endpoint.** The bulk
  export already contains geo, hierarchy and all attributes — using the detail
  page would add a second, inconsistent schema for no gain. Both are documented
  in `PROTOCOL.md` because knowing when *not* to use an endpoint is part of the
  map.
* **Writes of any kind.** The portal is treated as read-only.

---

## With more time

1. **Incremental sync** — diff meter metadata, refetch only meters whose
   `lastReadingAt` moved. Minutes → seconds.
2. **Explicit hierarchy table** with parent/child rows, plus transformer
   `capacityKva` (already fetched, currently unused) for load analysis.
3. **Conditional responses** — `ETag`/`If-None-Match`. The dataset is frozen, so
   most repeat requests could be `304`.
4. **Per-meter timeout budget** and a real circuit breaker in the client, so one
   hung meter cannot delay a whole run.
5. **A stub-portal integration test** (`respx`) covering re-login, 429 backoff
   and 503 mapping — currently verified by hand only.
6. **Split `suppressedPoints`** into missing-data vs truncation counts.

---

## Testing

```bash
uv run pytest -q      # 41 tests, ~1s, no network
```

* `tests/test_parse.py` — the portal's quirks as executable assertions:
  day-first dates, empty-string nulls, the corrupt trailing row, blank-feeder
  repair, granularity detection, and a known-answer HMAC signature vector.
* `tests/test_api.py` — behaviour against a seeded temp database: filters,
  keyset pagination, delta maths, the 30m-on-daily `422`, hierarchy count
  conservation, `problem+json` shapes.

No test touches the network. The API tests skip the FastAPI lifespan so no
background sync starts during a run.

---

## Layout

```
urja/
  main.py            FastAPI app, lifespan, OpenAPI metadata
  config.py          env-driven settings
  models.py          Pydantic schemas — these generate openapi.json
  errors.py          RFC 9457 problem+json handlers
  portal/            the ONLY code that knows the portal exists
    auth.py          session, single-flight re-login
    sign.py          HMAC export signing
    client.py        timeouts, pacing, 429 backoff, circuit breaker
    parse.py         day-first dates, "" → null, registers → readings
  db/
    schema.sql       meters, dts, readings, sync_runs, meta
    store.py         queries + upserts
  sync/runner.py     export + paced energy sweep, idempotent
  api/               meters, consumption, network, system
tests/               parser unit tests + API behaviour tests
scripts/dump_openapi.py
```

`portal/` is the seam: replace the legacy system and only that directory changes.

---

## Deliverables

| What | Where |
|---|---|
| How the portal actually works | [`PROTOCOL.md`](./PROTOCOL.md) |
| OpenAPI 3.1 spec of *this* API | [`openapi.json`](./openapi.json) (regenerate: `uv run python -m scripts.dump_openapi`) |
| Interactive docs | `/docs` when running |
| Design notes | this README |
| Reflection | [`REFLECTION.md`](./REFLECTION.md) |

Credentials live in `.env` (git-ignored). `.env.example` has placeholders only.
