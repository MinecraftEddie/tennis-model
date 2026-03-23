"""
ingestion/http_utils.py
=======================
Lightweight HTTP helpers shared across ingestion modules.

Provides fetch_with_retry() — a thin wrapper around requests.Session.get()
that retries on transient failures (429, 502, 503, 504, Timeout) with
exponential backoff.  No external dependencies beyond requests.
"""
import logging
import time

import requests

log = logging.getLogger(__name__)

# HTTP status codes that warrant a retry (transient server-side errors)
_RETRY_STATUS = frozenset({429, 502, 503, 504})


def fetch_with_retry(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 12.0,
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> requests.Response:
    """
    GET *url* using *session*, retrying on transient failures.

    Retry triggers:
      - HTTP 429 / 502 / 503 / 504 — server-side transient error
      - requests.Timeout            — network timeout

    Backoff: base_delay * 2^attempt  (2s, 4s, 8s by default).
    Non-transient errors (connection refused, DNS, 4xx ≠ 429) propagate immediately.

    Returns the final requests.Response on success (status may still be a
    retry-status if retries are exhausted — caller should check r.status_code).
    Raises requests.Timeout if all timeout attempts are exhausted.
    """
    last_timeout_exc: Exception | None = None

    for attempt in range(max_attempts):
        try:
            r = session.get(url, timeout=timeout)

            if r.status_code in _RETRY_STATUS and attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    f"[HTTP] {r.status_code} on attempt {attempt + 1}/{max_attempts} "
                    f"— retrying in {delay:.0f}s: {url}"
                )
                time.sleep(delay)
                continue

            # Either success, non-retry status, or last attempt — return as-is
            return r

        except requests.Timeout as exc:
            last_timeout_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    f"[HTTP] Timeout on attempt {attempt + 1}/{max_attempts} "
                    f"— retrying in {delay:.0f}s: {url}"
                )
                time.sleep(delay)
            # else: fall through to raise below

        except requests.RequestException:
            # Non-transient (connection error, DNS, etc.) — don't retry
            raise

    # All timeout attempts exhausted
    raise last_timeout_exc  # type: ignore[misc]
