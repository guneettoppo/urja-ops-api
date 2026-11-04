"""HTTP client for the Urja Meter Ops portal.

Responsibilities, all of which a consumer of the raw portal would otherwise
have to reimplement:

* authenticate, and re-authenticate when the 1-hour session dies
* sign requests to the bulk-export endpoint
* stay under the portal's undocumented burst limit (429 with no Retry-After)
* cap concurrency so we never look like an attack to a utility's portal
* fail fast (circuit breaker) when the portal is unhealthy, instead of hanging
* distinguish "portal said no such meter" from "portal is broken"
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from urja.config import Settings
from urja.portal.auth import PortalAuthError, PortalSession
from urja.portal.sign import export_headers

log = logging.getLogger(__name__)


class PortalError(RuntimeError):
    """Base class for portal failures."""


class PortalUnavailable(PortalError):
    """Transport-level failure: timeout, connection reset, 5xx, breaker open."""


class PortalProtocolError(PortalError):
    """The portal answered, but not with something we understand."""


class PortalNotFound(PortalError):
    """The portal says the entity does not exist (a real 404)."""


class PortalClient:
    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=cfg.portal_base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.request_timeout_seconds),
            follow_redirects=True,
            headers={"accept": "application/json", "user-agent": "urja-ops-api/0.1 (+integration)"},
        )
        self._session = PortalSession(self._http, cfg)
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrency))
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0
        self._secret: str | None = None
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    # ---------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> "PortalClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- primitives

    async def _pace(self) -> None:
        """Enforce a minimum gap between requests (the portal 429s on bursts)."""
        if self._cfg.polite_delay_ms <= 0:
            return
        async with self._pace_lock:
            gap = self._cfg.polite_delay_ms / 1000
            wait = self._last_request + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _breaker_open(self) -> bool:
        return time.monotonic() < self._breaker_open_until

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._breaker_open_until = time.monotonic() + 30
            log.warning("portal circuit breaker opened for 30s after %d failures", self._consecutive_failures)

    def _note_success(self) -> None:
        self._consecutive_failures = 0

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 4,
        allow_404: bool = False,
    ) -> Any:
        if self._breaker_open():
            raise PortalUnavailable("portal marked unhealthy; failing fast")

        last_error: Exception | None = None
        auth_retried = False

        for attempt in range(1, attempts + 1):
            generation = await self._session.ensure()
            try:
                async with self._sem:
                    await self._pace()
                    response = await self._http.get(path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._note_failure()
                log.warning("portal transport error on %s (attempt %d): %s", path, attempt, exc)
                last_error = exc
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            status = response.status_code

            if status == 200:
                self._note_success()
                try:
                    return response.json()
                except ValueError as exc:  # portal returned HTML, e.g. a redirect to /login
                    raise PortalProtocolError(f"expected JSON from {path}, got HTML") from exc

            if status == 401:
                self._note_failure()
                if auth_retried:
                    raise PortalAuthError("portal rejected a freshly minted session")
                auth_retried = True
                await self._session.invalidate(generation)
                continue

            if status == 404:
                if allow_404:
                    return None
                raise PortalNotFound(f"portal 404 for {path}")

            if status == 429:
                # No Retry-After header exists; back off blind and generously.
                self._note_failure()
                wait = 20
                log.warning("portal rate limited on %s; sleeping %ss", path, wait)
                await asyncio.sleep(wait)
                last_error = PortalError("rate limited")
                continue

            if status >= 500:
                self._note_failure()
                last_error = PortalError(f"portal {status}")
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            raise PortalProtocolError(f"unexpected {status} from {path}: {response.text[:200]}")

        raise PortalUnavailable(f"portal request failed after {attempts} attempts: {last_error}")

    # ---------------------------------------------------------------- endpoints

    async def ping(self) -> bool:
        try:
            await self._request("/portal/dts", params={"page": 1}, attempts=1)
            return True
        except PortalError:
            return False

    async def signing_secret(self, *, refresh: bool = False) -> str:
        """The export HMAC key, which the portal hands to any logged-in browser."""
        if self._secret and not refresh:
            return self._secret
        payload = await self._request("/portal/keys")
        secret = ((payload or {}).get("data") or {}).get("signingSecret")
        if not secret:
            raise PortalProtocolError("portal returned no signing secret")
        self._secret = secret
        return secret

    async def export_meters(self) -> tuple[list[dict[str, Any]], int]:
        """Fetch the whole meter corpus in one signed request.

        ``page`` is part of the signature but ignored by the server: every page
        number returns the full set. This is the only bulk path that exists.
        """
        query = "page=1"
        secret = await self.signing_secret()
        payload = await self._request("/portal/export", params={"page": 1}, headers=export_headers(secret, query))
        if payload is None:
            raise PortalProtocolError("empty export payload")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PortalProtocolError("export payload has no data array")
        return rows, int(payload.get("total") or len(rows))

    async def list_dts(self) -> list[dict[str, Any]]:
        """All distribution transformers, following the portal's own paging."""
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._request("/portal/dts", params={"page": page})
            rows = (payload or {}).get("data") or []
            rows = [r for r in rows if isinstance(r, dict)]
            page_size = int(payload.get("pageSize") or 20)
            collected.extend(rows)
            total = int(payload.get("total") or 0)
            if not rows or len(collected) >= total or page_size <= 0:
                break
            page += 1
        return collected

    async def meter_energy(self, meter_id: str) -> list[dict[str, Any]] | None:
        """Raw cumulative register readings for one meter (None if it is unknown)."""
        payload = await self._request(
            f"/portal/meters/{meter_id}/energy", allow_404=True
        )
        if payload is None:
            return None
        rows = payload.get("data")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise PortalProtocolError(f"unexpected energy payload for {meter_id}")
        return [r for r in rows if isinstance(r, dict)]
