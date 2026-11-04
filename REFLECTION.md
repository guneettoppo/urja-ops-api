# REFLECTION.md

Five questions, answered honestly. The interesting parts of this exercise were
not the code.

---

### What assumptions did I make?

Three, and they are load-bearing.

**That the timestamps are IST.** The portal gives `dd/MM/yyyy HH:MM` with no
timezone anywhere — not in the payload, not in a header. I inferred UTC+05:30
from the day-first format, the Jaipur transformer names, and the fact that this
is an Indian discom deployment. If the source were actually UTC, every
consumption point in this API would be shifted by five and a half hours. I
chose IST because the alternative (naive local time treated as UTC) would put
meter readings at 18:00 for what the utility calls 23:30 — plausible for a
distribution feeder, implausible for the portal's own display.

**That the signed export is a legitimate integration path.** A signing secret
fetched from `/portal/keys` and used client-side is a design choice by the
portal's authors, not something I broke into. I treated it as the intended bulk
path and used it exactly as the portal's own frontend does. What I did *not* do
is keep probing that endpoint for anything beyond the documented button.

**That a 4-hour-old index is acceptable but a 4-month-old dataset is not
something to hide.** The dataset is frozen at 2026-06-30, so "freshness" splits
into two questions. Rather than present the index as live, the API reports both
`dataAgeSeconds` (our copy) and `counts.lastReadingAt` (the data). A consumer
that ignores the second number will build a chart of last June and think it is
today.

Two smaller ones: that a meter's `dtCode` is authoritative for its transformer
(it always matched `hierarchy.dt.code` across all 403 rows), and that the
portal's 1-hour session has no renew-on-use behaviour.

---

### Which part was the most difficult, and how did you get unstuck?

Finding the data channel. The portal looks like a plain server-rendered
internal app until you notice the SvelteKit fingerprints in the HTML head. My
first instinct — guess at REST paths like `/api/meters` — produced nothing but
404s, because the app uses `/portal/*` for its JSON and `__data.json` for page
payloads, which is not a naming convention anyone would guess.

What unstuck me was reading the application's own source instead of its
behaviour: pull the shipped bundles, map route → bundle through the entry
manifest, and grep the minified JavaScript for string literals. Path fragments
survive minification, so `fetch(`/portal/meters/search?q=…`)` was sitting there
in plain sight. That turned guessing into reading.

The second difficulty was *not* believing my first answer. A quick probe
appeared to show that `/portal/meters/search` served data without authentication.
That would have been a spectacular finding for the write-up and it was wrong —
an artefact of my test harness. Re-running the same request through two
independent clients showed `401` consistently. I nearly wrote a false claim
into a document whose whole purpose is to be trusted, and the lesson was cheap
only because I re-checked before writing it down.

---

### If you had another day, what would you improve?

**Incremental sync.** Right now a refresh refetches all 403 reading series,
which takes about four minutes regardless of how little changed. Meter metadata
comes down in one 350 KB request and could be diffed cheaply; only meters whose
`lastReadingAt` moved would need a new energy request. That would cut the
steady-state cost by an order of magnitude.

**A proper hierarchy model.** The tree is currently reconstructed from
per-meter paths at query time. A `nodes` table with explicit parent/child rows
would make "everything under this subdivision" a single indexed query instead
of an aggregation, and would let me attach transformer capacity to DT nodes —
which is already in `/portal/dts` and unused, and is the obvious join for load
analysis.

**Conditional responses.** The API sets no `ETag`/`Last-Modified` and ignores
`If-None-Match`. Given a frozen dataset, nearly every repeat request could be a
`304`. That is the single highest-leverage performance change available and it
is maybe twenty lines.

**An integration test against a stub portal.** Parsing and API behaviour are
covered, but the client's retry/re-login/429 paths are only exercised by hand.
A `respx`-based fake portal would let me assert "expired session → one login,
not five" in CI.

I deliberately did not build a web client. It would have been the most
enjoyable part and the least informative one for a reviewer trying to judge how
I think about systems.

---

### What mistake did you make while solving this?

The largest was **designing the schema before looking at the data**. I wrote
the `meters` table first — with `serial`, `model`, `account_number`, a
`status` enum of active/inactive/faulty — and then discovered the export uses
`serialNo`, has no model or account number, and calls the status field
`installStatus` with the values Installed/Faulty/Decommissioned. I had to
rewrite the schema and every query against it. Ten minutes of profiling the
actual payload first would have avoided all of it. The general form of the
mistake: I let a plausible domain model substitute for observation.

The smaller one is in the code rather than the design. My first parsing pass
assumed `kwh` was consumption, which is what the field name implies. It is a
cumulative register. Everything downstream — charts, "total units in June",
any anomaly rule — would have been wrong, and wrong in a way that looks
plausible: a flat line instead of a spike. The empty-string nulls were the same
class of error: `""` is not `None`, and one unguarded `float()` aborted an
entire 403-request sweep partway through.

---

### If you were reviewing your own submission, what would you criticise?

**No authentication on the API itself.** I said so in the README rather than
building half an API-key scheme, but a reviewer is right to note that "internal
service" is an assumption, not a control. The correct fix is a gateway or
mTLS, not a bespoke header check.

**The sync is a single sequential loop.** With `max_concurrency=2` and 120 ms
pacing it is deliberately gentle, but it also means one slow meter delays the
run, and there is no per-meter timeout budget — a single hung request costs the
full 20 s timeout before moving on.

**`suppressedPoints` conflates two things.** It counts intervals dropped for
missing data and also intervals dropped by the 5000-point cap. Those are
different failures and a consumer debugging a short series cannot tell which
happened.

**No database migrations.** `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so
a schema change requires deleting the index. Acceptable for a derived cache,
but it is a real limitation and should be named as one.

**The tests are synthetic.** They encode the quirks I found, which is the right
instinct, but they would not notice if the portal changed shape tomorrow —
nothing in the suite touches the real thing. A nightly smoke test against the
live portal, failing loudly on a schema change, is what this actually needs.
