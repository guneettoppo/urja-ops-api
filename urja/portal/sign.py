"""HMAC request signing for the portal's bulk export endpoint.

The portal's own SvelteKit frontend signs ``/portal/export`` requests in the
browser, using a secret it fetches from ``/portal/keys``. We reproduce that
exactly: it is the only path to the full dataset in one request.

    signature = HMAC_SHA256(secret, "METHOD\nPATH\nQUERY\nUNIX_SECONDS")
    headers   = x-timestamp: <unix seconds>, x-signature: <lowercase hex>

The server tolerates a timestamp roughly 2-10 minutes old (measured: -120s is
accepted, -600s is not), so a clock-skewed host must retry with a fresh stamp.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_request(
    secret: str,
    method: str,
    path: str,
    query: str = "",
    timestamp: int | None = None,
) -> tuple[str, str]:
    """Return ``(timestamp, signature)`` for a portal export request."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    message = "\n".join([method.upper(), path, query, stamp])
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return stamp, digest


def export_headers(secret: str, query: str = "page=1") -> dict[str, str]:
    stamp, signature = sign_request(secret, "GET", "/portal/export", query)
    return {"x-timestamp": stamp, "x-signature": signature}
