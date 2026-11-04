"""Session management for the portal.

The portal issues a single ``__Secure-better-auth.session_token`` cookie with a
1-hour lifetime and no refresh token, so a long-running service must notice
expiry and log in again by itself. Concurrent callers share one login attempt
(single-flight) rather than stampeding the login endpoint.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from urja.config import Settings

log = logging.getLogger(__name__)


class PortalAuthError(RuntimeError):
    """The portal rejected our credentials."""


class PortalSession:
    def __init__(self, client: httpx.AsyncClient, cfg: Settings) -> None:
        self._client = client
        self._cfg = cfg
        self._lock = asyncio.Lock()
        self._generation = 0
        self._authenticated = False

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._client.cookies)

    async def login(self) -> None:
        """POST the portal's login form and keep the session cookie."""
        if not self._cfg.portal_email or not self._cfg.portal_password:
            raise PortalAuthError(
                "PORTAL_EMAIL / PORTAL_PASSWORD are not configured; copy .env.example to .env"
            )
        # The portal rejects posts without an Origin header (basic CSRF guard).
        response = await self._client.post(
            "/login",
            data={"email": self._cfg.portal_email, "password": self._cfg.portal_password},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self._cfg.portal_base_url.rstrip("/"),
                "Referer": f"{self._cfg.portal_base_url.rstrip('/')}/login",
            },
            follow_redirects=False,
        )
        body = response.text[:200]
        if response.status_code >= 400 or '"type":"failure"' in body:
            raise PortalAuthError(f"login rejected (HTTP {response.status_code}): {body}")

        has_session = any("session_token" in name for name in self._client.cookies)
        if not has_session:
            raise PortalAuthError("login returned no session cookie")

        self._authenticated = True
        self._generation += 1
        log.info("portal session established")

    async def ensure(self) -> int:
        """Log in if needed; returns the session generation for retry logic."""
        if self._authenticated:
            return self._generation
        async with self._lock:
            if not self._authenticated:  # re-check: another coroutine may have won
                await self.login()
        return self._generation

    async def invalidate(self, generation: int | None = None) -> None:
        """Mark the session dead after a 401.

        ``generation`` guards against N concurrent 401s each triggering its own
        login: only the first one to arrive after the failure re-authenticates.
        """
        async with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._authenticated = False
            self._client.cookies.clear()
            log.warning("portal session invalidated")
