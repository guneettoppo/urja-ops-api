# PROTOCOL.md — how the Urja Meter Ops portal actually works

Notes from mapping the portal as a logged-in operator. Everything here was
observed from a normal user session: no exploit, no privileged path, no
guessing of undocumented admin routes.

Every command below is reproducible against the live portal with a valid
session cookie.

---

## 1. Summary

| Question | Answer |
|---|---|
| Frontend | SvelteKit (Svelte 5) SPA, Tailwind |
| Auth | Better Auth session cookie, **1 hour** lifetime, no refresh |
| Data channel | JSON endpoints under `/portal/*`, called client-side |
| Bulk path | `GET /portal/export` — all meters in one request, **HMAC-signed** |
| Meter detail | Server-rendered page payload at `/meters/{id}/__data.json` |
| Filters supported | A single substring search over **meter id and serial only** |
| Rate limiting | Yes, burst-based, **no `Retry-After` header** |
| Data granularity | Per meter: 30 minutes for some, daily for others |
| Data freshness | Readings stop at **2026-06-30**; dataset appears frozen |

The important discovery is that the portal **is not HTML scraping**. It is a
client-rendered app talking to a small JSON backend, which makes a clean
service in front of it practical rather than a screen-scraper.

---

## 2. Authentication

### Login

```
POST /login
Content-Type: application/x-www-form-urlencoded
Origin: https://urja-ops.flockenergy.tech

email=…&password=…
```

Successful response — `200` with:

```
Set-Cookie: __Secure-better-auth.session_token=<token>; Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax

{"type":"redirect","status":303,"location":"/meters"}
```

Failure — `200` with an SvelteKit action failure body:

```json
{"type":"failure","status":401,"data":"[{\"email\":1,\"error\":2},\"REDACTED\",\"Invalid email or password.\"]"}
```

Two things worth knowing:

* Without an `Origin` header the portal answers `403 Cross-site POST form
  submissions are forbidden`. That is SvelteKit's CSRF guard, and a plain
  scripted client has to send the header explicitly.
* The cookie is `__Secure-` prefixed, so it only travels over HTTPS.

### Session inspection

`GET /api/auth/get-session` returns the live session, including `expiresAt`:

```json
{"session":{"expiresAt":"2026-11-04T12:52:17.807Z","token":"54O6…","ipAddress":"…","userAgent":"curl/8.18.0"},
 "user":{"name":"Ops Desk","email":"REDACTED"}}
```

### What this means for a service

There is no refresh token and no expiry hint on failure. Every `/portal/*`
call returns:

```
401 {"error":"unauthorized","message":"A valid session is required."}
```

So a long-running client must detect a `401`, log in again, and retry. Page
routes redirect (`302 /login`), API routes do not — check status codes per
route type, not globally.

Ending a session is `POST /api/auth/sign-out`.

---

## 3. Where the data actually lives

The app uses two different mechanisms, and both are reachable:

**(a) Plain JSON endpoints** the SPA calls from the browser:

| Method | Path | Returns |
|---|---|---|
| GET | `/portal/meters/search?q=&page=N` | paged meter list |
| GET | `/portal/dts?page=N` | transformer list |
| GET | `/portal/meters/{id}/geo` | `{latitude, longitude}` |
| GET | `/portal/meters/{id}/energy` | reading series |
| GET | `/portal/keys` | the export signing secret |
| GET | `/portal/export?page=1` | full dataset (signed) |

**(b) SvelteKit server-load payloads**, one per page, available as
`<page-url>/__data.json`. The meter detail page uses this, which is why its
data is not in a `/portal/*` route.

I found the `/portal/*` paths by harvesting them out of the shipped JavaScript
bundles rather than guessing:

```bash
curl -s -b cookies.txt "$BASE/meters/J100000" \
  | grep -oE '_app/immutable/nodes/[0-9]+\.[A-Za-z0-9_-]+\.js'
# then grep those bundles for template literals containing "/portal/"
```

Minified bundles keep their string literals, so path fragments survive.

---

## 4. Endpoint semantics (measured, not assumed)

Portal endpoints accept unknown query parameters **without erroring**, so a
`200` proves nothing. Each of the following was established by diffing
responses against a baseline.

### `/portal/meters/search`

```json
{"data":[{"meterId":"J100000","serialNo":"SE33962","make":"HPL",
          "phaseType":"single","installStatus":"Decommissioned","dtCode":"DT-001"}],
 "total":403,"page":1,"pageSize":20}
```

* `q` is a **case-insensitive substring** match across `meterId` and
  `serialNo` only. `q=84132` and `q=se652` both return one row; `q=GEN` (a
  make) and `q=DT-001` (a feeder-ish code) return zero.
* `dt=`, `status=`, `make=`, `feeder=`, `pageSize=` are **ignored**.
* `pageSize` is always 20 and cannot be overridden.
* `page=0` and `page=-1` behave like page 1.
* Past the last page: `{"data":[],"total":403,"page":999999,"pageSize":20}` —
  an empty page, not an error.

### `/portal/dts`

Same envelope, `total: 40`, 20 per page. Row shape:

```json
{"code":"DT-001","name":"Malviya Nagar DT 1","feederCode":"F-001","capacityKva":100}
```

This is the authority for which feeder a transformer belongs to, and it is
what makes repairing blank feeder codes possible.

### `/portal/meters/{id}/energy`

```json
{"data":[{"timestamp":"23/06/2026 23:30","kwh":"48438.74",
          "kvah":"52313.84","voltR":"226"}]}
```

Four properties that matter enormously downstream:

1. **`kwh` is a cumulative register**, not consumption. Consecutive values
   increase monotonically. Consumption is the difference between readings.
2. **Every number is a string**, and a missing value is the **empty string
   `""`**, not `null`.
3. **Timestamps are `dd/MM/yyyy HH:MM`, day-first, naive local time.** Parsing
   them with a US format succeeds and is wrong by months.
4. **Query parameters are ignored.** `from`, `to`, `days`, `start` and `page`
   all return byte-identical data. The window is fixed server-side.

### `/portal/meters/{id}/geo`

```json
{"data":{"latitude":"26.938961002479868","longitude":"75.83095696146852"}}
```

Also strings. Unknown meter → `404 {"error":"not_found","message":"Meter not found"}`.

### `/meters/{id}/__data.json` (detail page payload)

SvelteKit flattens data into an index array. Two shapes exist, selected by the
meter's `build` field:

* `build: "legacy"` — attributes as key/value pairs:
  `[{"parameterName":"Make","parameterValue":"HPL"}, …]`
* `build: "v2"` — a `classData` field containing **a JSON string that must be
  parsed a second time**, with PascalCase keys:
  `{"installed_meter":{"MeterId":"J100004","SerialNo":"SE65293", …}}`

Both carry `hierarchy` as display strings with the code embedded:

```json
{"Zone":"Jaipur Zone 1 (Z-01)","Circle":"Circle 1 (C-01)", …,"DT":"Malviya Nagar DT 1 (DT-001)"}
```

**Quirk:** a non-existent meter returns **HTTP 200** whose payload contains an
error node (`{"type":"error","error":{"message":"Meter not found"},"status":404}`).
Checking only the status code reports a broken meter as a healthy one.

---

## 5. The bulk export (the important find)

The Transformers page has an *Export JSON* button. Behind it:

```
GET /portal/keys
→ {"data":{"signingSecret":"<32 chars>"}}

signature = HMAC_SHA256(secret, "GET\n/portal/export\n<query>\n<unix_seconds>")
→ headers: x-timestamp: <unix_seconds>, x-signature: <lowercase hex>
```

The signing routine is plain JavaScript in the bundle:

```js
async function C(method, path, query, secret) {
  const timestamp = String(Math.floor(Date.now() / 1e3));
  const message = [method, path, query, timestamp].join("\n");
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
                                            {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return {timestamp, signature: [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("")};
}
```

Observations:

* **`page` is part of the signature but ignored by the server.** `page=1`,
  `page=2` and `page=99` all return the same 403 meters (~350 KB). One request
  replaces 21 paginated ones.
* The timestamp has a tolerance window: an offset of −120 s is accepted, −600 s
  is rejected with `401 {"error":"signature_invalid"}`. So the window is
  somewhere between two and ten minutes — relevant if a host's clock drifts.
* The query string is inside the signature, so **any parameter change requires
  re-signing**.
* The secret is handed to any logged-in browser. That is the portal's design,
  not a vulnerability: a signed export is a legitimate path for a logged-in
  service. I treat `/portal/keys` as a required step of the integration, not as
  something to exploit further.

Export rows are **richer** than the search endpoint: they add `installType`,
`build`, a structured `hierarchy` object (`{name, code}` per level — no string
parsing needed), and `geo` as real numbers.

---

## 6. Rate limiting

Undocumented, and it matters.

* A burst of ~80 sequential requests completed in 9.5 s without complaint.
* A sweep of ~350 requests within ~10 s produced:
  `429 {"error":"rate_limited","message":"Rate limit exceeded; slow down."}`
* **There is no `Retry-After` header** — a client has to back off blind.
* Recovery was complete within ~45 s; a steady 1 request/second ran 120
  requests cleanly.

The client here therefore paces requests (`POLITE_DELAY_MS`, default 120 ms),
caps concurrency at 2, and on `429` sleeps 20 s and retries the same request.
A full 403-meter reading sweep takes roughly four minutes and never trips the
limiter.

Other response-hygiene gaps: no `Cache-Control`, `ETag` or `Last-Modified` on
`/portal/*`, and no CORS or rate-limit headers to introspect. Everything must
be learned by observation.

---

## 7. Data quality findings

The portal's data is messy in specific, enumerable ways. Counts below are from
the full 403-meter export.

**Population:** 403 meters, ids `J100000`–`J100402`, contiguous.

| Attribute | Distribution |
|---|---|
| make | HPL 90, Genus 88, Secure 85, Allied 76, L&T 64 |
| phase | single 277, three 126 |
| install status | Installed 238, Faulty 90, Decommissioned 75 |
| install type | Whole Current 205, CT Operated 198 |
| build | legacy 238, v2 165 |

**Hierarchy:** 3 zones, 7 circles, 10 divisions, 14 subdivisions, 19
substations, 28 feeders (F-001…F-028) and 40 DTs.

### The specific problems

1. **Granularity varies per meter.** 40 meters return half-hourly readings
   (337 points, 2026-06-23 23:30 → 2026-06-30 23:30); the other **363 return
   daily readings** (8 points). There is no parameter to request an interval —
   the meter decides.
2. **A corrupt trailing row.** 5 daily meters (`J100089`, `J100134`,
   `J100195`, `J100211`, `J100330`) return a 9th row that repeats the last
   timestamp with empty `kwh` and `voltR`.
3. **Empty strings where a code should be.** 22 meters carry blank hierarchy
   fields: 5 feeders with a blank code (3 also blank name), 6 circles with a
   blank code (5 also blank name), 3 substations with a blank code.
4. **Blank feeder codes that are recoverable.** 5 meters (`J100044` DT-005,
   `J100078` DT-039, `J100134` DT-015, `J100210` DT-011, `J100268` DT-029) have
   no feeder code on the meter row, but `/portal/dts` knows which feeder their
   DT hangs off. The API repairs these and records
   `FEEDER_REPAIRED_FROM_DT`.
5. **The same DT under two names.** `/portal/dts` calls `DT-007`
   *"Sanganer DT 7"*; the meter rows call it *"Old Malviya Nagar Xfmr"*. A
   rename that was never backfilled.
6. **A mis-assigned transformer.** `DT-029` reports `feederCode: F-001` in
   `/portal/dts`, while its neighbours follow the obvious pattern and the
   feeder list stops at F-028. This looks like a data-entry error rather than
   real topology, so the API surfaces it but does not "correct" it.
7. **Serial numbers may contain non-alphanumerics** — e.g. `L&84997`. Never
   validate a serial against `[A-Z0-9]+`.
8. **The dataset is frozen.** The newest reading anywhere is
   `2026-06-30 23:30`, while the portal is live. Freshness therefore has two
   meanings, and the API reports both: how recently *we* synced
   (`dataAgeSeconds`) and how old the underlying data is
   (`counts.lastReadingAt`).

### Volume

* 403 meters, 16,389 readings, ~1.4 MB of reading JSON.
* One export request for meter metadata + 403 requests for readings — there is
  no bulk readings endpoint, and the energy route ignores every parameter that
  might have been one.

---

## 8. What I did not confirm

Stated plainly, because these are assumptions rather than findings:

* **Timezone.** I treat timestamps as IST (UTC+05:30). Evidence: day-first
  formatting, Jaipur DT names, and an Indian discom deployment. Not proven by
  any field in the API.
* **Session-IP binding.** `get-session` records `ipAddress`; I did not test
  whether the token is rejected from a different address.
* **The exact rate-limit window and burst size.** Only bounded by observation
  (80 OK / ~350 not OK / ~45 s recovery). A different client might have a
  different budget.
* **Whether the signing secret rotates.** It was constant for the whole
  session; `/portal/keys` may yet hand out a new one after some period.
* **Whether the energy window ever moves.** It was a fixed seven-day window on
  every meter on every request; whether that is "last 7 days" or a static
  export was impossible to distinguish from one session, because the data is
  frozen.

---

## 9. What this means for the API in this repository

| Portal behaviour | How the service handles it |
|---|---|
| Session dies hourly | Single-flight re-login; a `401` invalidates once, not N times |
| Signed export, ignored `page` | One signed request per sync, secret cached and refreshed on failure |
| 429 without `Retry-After` | Paced requests, capped concurrency, blind backoff on 429 |
| Register values | Consumption exposed as derived deltas, flagged `derived: true` |
| Mixed granularity | Reported per meter as `sourceGranularity`; 30m request on a daily meter is `422` |
| Empty-string nulls | Cast tolerantly; gaps counted in `suppressedPoints` |
| Day-first timestamps | Parsed with an explicit format, stored ISO-8601 with offset |
| 2 detail shapes | Both normalised to one schema; `build` retained as metadata |
| 200-wrapped 404 | Not relied upon — the index is authoritative for existence |
| Blank hierarchy codes | Kept as `null` and counted; feeder repaired from DT where possible |
| Dataset frozen ~4 months ago | Reported honestly rather than presented as live |
