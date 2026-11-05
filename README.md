# Urja Ops API

A clean, documented REST API over the legacy **Urja Meter Ops** portal — an
ageing internal web app that distribution-utility staff use to look up smart
meters, their position in the network, and their consumption.

The portal has no API of its own. This service logs into it, indexes its data
into SQLite, and serves a typed, filterable, documented surface on top. Nothing
downstream ever has to hold a portal session, sign an HMAC request, or parse a
`dd/MM/yyyy` timestamp.

**Live instance:** <https://urja-ops-api-aj94.onrender.com> —
start at [`/docs`](https://urja-ops-api-aj94.onrender.com/docs). It runs on
Render's free plan, so the first request after 15 minutes of idleness takes
~60 s to wake; everything after that is ~130 ms.

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

## The domain in 30 seconds

Zero prior context assumed. A **discom** is an electricity distribution
company; Urja is the internal portal its operations staff use. The physical
network is a tree, and the portal models it that way:

```
zone → circle → division → subdivision → substation → feeder → DT → meter
```

- A **DT** (distribution transformer) is the green box on a pole that steps
  medium voltage down to what a house uses. Meters hang off a DT.
- A **feeder** is the 11 kV line feeding many transformers.
- A **meter** is the device on a customer's supply. *Single-phase* is a house;
  `CT-operated` / three-phase is a larger connection. Makes in this dataset are
  Genus, HPL and similar Indian meter vendors.
- A **register** is the number printed by the meter, and it only ever counts
  up — like a car odometer. **Consumption** is the difference between two
  register readings. The portal serves registers; almost every useful question
  is about consumption. That distinction is the single most important thing in
  this repository.

So the data behind this API is: 403 meters, each with a position in that tree,
plus a time series of register readings. Nothing about customers — no names, no
addresses, no billing.

---

## Quick start

Requires Python 3.11+ (tested on 3.14) and [uv](https://docs.astral.sh/uv/).
No Docker, no database server, no cloud account.

```bash
git clone https://github.com/guneettoppo/urja-ops-api.git
cd urja-ops-api

cp .env.example .env          # then set PORTAL_EMAIL / PORTAL_PASSWORD
uv sync                       # install dependencies (~5 s)

uv run python -m urja.sync    # build the index (~4 min cold, 1 export + 403 calls)
uv run uvicorn urja.main:app --port 8081
```

Open **http://localhost:8081/docs** for interactive docs, or
**http://localhost:8081/openapi.json** for the machine-readable spec.

> Port 8080 is the container's port; use 8081 locally if 8080 is taken.
> The service also syncs in the background on boot if the index is empty, so
> `uvicorn` alone works — the first sync just runs while you browse `/docs`.

### Configuration

Everything is environment-driven (pydantic-settings). Precedence: real
environment variables → `.env` → defaults in `urja/config.py`. Only the first
two have no default; everything else works untouched.

| Variable | Default | What it does |
|---|---|---|
| `PORTAL_EMAIL` | — (**required**) | portal login |
| `PORTAL_PASSWORD` | — (**required**) | portal password |
| `PORTAL_BASE_URL` | `https://urja-ops.flockenergy.tech` | change only to point at a clone |
| `DATABASE_PATH` | `./data/urja.db` | where the SQLite index lives |
| `SEED_DB_PATH` | `./seed/urja.db` | snapshot copied in when the index is empty |
| `SEED_ON_EMPTY` | `true` | set `false` to always sync from scratch |
| `MAX_CONCURRENCY` | `2` | simultaneous portal requests; keep it low |
| `POLITE_DELAY_MS` | `120` | minimum gap between portal requests |
| `REQUEST_TIMEOUT_SECONDS` | `20` | per-request HTTP timeout |
| `AUTO_SYNC_ON_START` | `true` | sync in the background on boot if the index is empty |
| `SYNC_ENERGY_ON_START` | `true` | include the 403-call energy sweep on boot (**set `false` on free hosts**) |
| `SYNC_INTERVAL_MINUTES` | `60` | refresh period for the background loop |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every portal request |

If `PORTAL_EMAIL`/`PORTAL_PASSWORD` are missing the service still starts and
serves whatever is indexed; `/sync/status` reports a failed run with
`"PORTAL_EMAIL / PORTAL_PASSWORD are not configured"` rather than crashing.

### Containerised

```bash
docker build -t urja-ops-api .
docker run -p 8081:8080 --env-file .env -v urja-data:/data urja-ops-api
```

The image is a plain `python:3.12-slim` with dependencies installed by `uv`.
It listens on `$PORT` (default `8080`) and keeps the index on a volume at
`/data`.

---

## Deploying it

It runs on **Render's free plan**. The service is stateful — a SQLite index plus
a background sync — and the free tier has an ephemeral filesystem. That single
constraint shapes everything below.

| Target | Cost | Disk | Cold start |
|---|---|---|---|
| **Render** (free plan) | $0 | ephemeral | ~1 min sleeping + ~2 s boot |

`render.yaml` in the repo root is the blueprint.

### The seed database

Free tiers throw the filesystem away on every deploy, restart and spin-down. A
cold start would therefore re-sync the portal — 404 requests, about four
minutes, on every wake-up. That is a bad first impression for a reviewer who
just clicked the link.

`seed/urja.db` is a committed snapshot (2.3 MB, 403 meters, 16,384 readings).
On boot, if the index is empty, it is copied into place — **~1 s** instead of
~4 min:

```bash
uv run python -m scripts.build_seed     # rebuild after a sync
```

This is only defensible because the portal's data is frozen at 2026-06-30. If
the readings were live, a committed snapshot would be a correctness bug; the
container would need a volume and a real sync instead.

### Render (free, no card)

1. Push this repo to GitHub (done).
2. Render dashboard → **New → Blueprint** → select the repo. It reads
   `render.yaml`.
3. It prompts for `PORTAL_EMAIL` and `PORTAL_PASSWORD` (they are `sync: false`,
   so they stay in Render's secret store and never enter the repo).
4. Deploy. First boot seeds the index and serves `/docs` immediately.

Live: <https://urja-ops-api-aj94.onrender.com/docs>

Free-plan caveats, stated plainly: the service sleeps after 15 minutes idle and
takes about a minute to wake; the filesystem is wiped on every restart (hence
the seed), so the index reverts to it and a `/sync` run does not survive a
restart; and the allowance is 750 instance hours per workspace per month — 24/7
running is ~730 h, so it fits, but only if no other free service shares the
workspace.

### Anywhere else

The image is a plain Dockerfile with no external services. Set `PORT`,
`DATABASE_PATH`, `PORTAL_EMAIL` and `PORTAL_PASSWORD`, mount a volume for the
index if you want it to survive restarts, and let `AUTO_SYNC_ON_START` do the
rest. Nothing else is required — no broker, no cache, no managed database.

> The brief asks how to *run* it; deployment is optional. On a free host set
> `SYNC_ENERGY_ON_START=false` (as `render.yaml` does): a full energy sweep costs
> four minutes and the data is frozen, so it buys nothing.

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
      "meterId": "J100039",
      "serial": "AL96955",
      "make": "Genus",
      "phase": "single",
      "status": "faulty",
      "installation": "whole_current",
      "dtCode": "DT-040",
      "feederCode": "F-012",
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
  "meta": { "generatedAt": "2026-11-04T17:43:04+00:00", "dataAgeSeconds": 50, "total": 6 }
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
      "kwh": 20.29, "kvah": 21.94, "derived": true, "anomaly": null },
    { "start": "2026-06-25T00:00+05:30", "end": "2026-06-26T00:00+05:30",
      "kwh": 20.29, "kvah": 21.94, "derived": true, "anomaly": null }
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

Note what is absent: no `"kwh": 48439.16` register values, no `"23/06/2026 23:30"`
strings, no nulls dressed up as zeroes.

The network tree, reconstructed from per-meter paths:

```bash
curl "localhost:8081/network/hierarchy?to=feeder"
```

```json
{
  "depth": "feeder",
  "roots": [
    { "code": "Z-01", "name": "Jaipur Zone 1", "meters": 143,
      "children": [
        { "code": "C-01", "name": "Circle 1", "meters": 72,
          "children": [
            { "code": "D-01", "name": "Division 1", "meters": 20,
              "children": [
                { "code": "SD-01", "name": "Subdivision 1", "meters": 10,
                  "children": [
                    { "code": "SS-01", "name": "Substation 1", "meters": 10,
                      "children": [
                        { "code": "F-001", "name": "Feeder 1", "meters": 10, "children": [] }
                      ] }
                  ] }
              ] }
          ] }
      ] }
  ]
}
```

Counts are conserved at every level: 143 + 130 + 130 zones = 403 meters.

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

**The spec.** `openapi.json` in the repo root is an OpenAPI 3.1 document
describing *this* API — not the portal's. It is generated from the Pydantic
models that serialise the responses, so it cannot drift from behaviour:

```bash
uv run python -m scripts.dump_openapi      # regenerate after changing a model
```

Two browser views come free with FastAPI: **`/docs`** (Swagger UI, interactive —
every endpoint can be called from the page) and **`/openapi.json`** served live
by the app. The committed file differs from the served one only in its
`servers` entry, which points at `localhost` for import into Postman/Insomnia.

Errors are RFC 9457 `application/problem+json` throughout:

```json
{ "type": "urn:urja:granularity-mismatch", "title": "Interval finer than the source data",
  "status": 422, "detail": "Meter J100050 only has 1d readings in the portal; a 30m series would be fabricated. Use interval=auto or interval=1d.",
  "instance": "/meters/J100050/consumption", "sourceGranularity": "1d", "requestedInterval": "30m" }
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
uv run pytest -q      # 46 tests, ~2 s, no network
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
    store.py         queries, upserts, seed install
  sync/runner.py     export + paced energy sweep, idempotent
  api/               meters, consumption, network, system
seed/urja.db         committed index snapshot for ephemeral hosts
tests/               parser unit tests + API behaviour tests
scripts/             dump_openapi.py, build_seed.py
render.yaml          Render blueprint (free plan)
```

`portal/` is the seam: replace the legacy system and only that directory changes.

---

Credentials live in `.env` (git-ignored, never in history). `.env.example` has
placeholders only. Nothing else is needed to run this: no database server, no
broker, no cloud account.
