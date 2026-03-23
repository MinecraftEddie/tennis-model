"""
tests/test_retry_backoff.py
============================
Unit tests for ingestion/http_utils.fetch_with_retry().

Covers:
  - 429 then success: retries and returns the good response
  - Timeout then success: retries and returns the good response
  - 502/503/504 then success: retries on server errors
  - Exhaust all retries on persistent Timeout: raises requests.Timeout
  - Non-transient error (e.g. ConnectionError): propagated immediately, no retry
  - Successful first attempt: only one call made
  - sleep() is called between retries (backoff verified)
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, call, patch
import requests

from tennis_model.ingestion.http_utils import fetch_with_retry


# ── Helpers ───────────────────────────────────────────────────────────────────

def _response(status: int) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    return r


# ── Retry on 429 ──────────────────────────────────────────────────────────────

def test_429_then_success():
    """429 on first attempt → retries → returns 200."""
    session = MagicMock()
    session.get.side_effect = [_response(429), _response(200)]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert r.status_code == 200
    assert session.get.call_count == 2


def test_429_twice_then_success():
    """Two 429s then 200: requires at least 3 attempts."""
    session = MagicMock()
    session.get.side_effect = [_response(429), _response(429), _response(200)]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert r.status_code == 200
    assert session.get.call_count == 3


def test_429_exhaust_returns_last_response():
    """When max_attempts exhausted on 429, return the final response (not raise)."""
    session = MagicMock()
    session.get.side_effect = [_response(429), _response(429), _response(429)]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    # fetch_with_retry returns the last response on exhaustion (caller checks status)
    assert r.status_code == 429
    assert session.get.call_count == 3


# ── Retry on Timeout ──────────────────────────────────────────────────────────

def test_timeout_then_success():
    """Timeout on first attempt → retries → returns 200."""
    session = MagicMock()
    session.get.side_effect = [requests.Timeout(), _response(200)]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert r.status_code == 200
    assert session.get.call_count == 2


def test_timeout_exhausted_raises():
    """All attempts timeout → raises requests.Timeout."""
    session = MagicMock()
    session.get.side_effect = [requests.Timeout(), requests.Timeout(), requests.Timeout()]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        with pytest.raises(requests.Timeout):
            fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert session.get.call_count == 3


# ── Retry on 502 / 503 / 504 ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", [502, 503, 504])
def test_server_error_then_success(status):
    """Transient server error → retries → returns 200."""
    session = MagicMock()
    session.get.side_effect = [_response(status), _response(200)]

    with patch("tennis_model.ingestion.http_utils.time.sleep"):
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert r.status_code == 200


# ── Non-transient errors ──────────────────────────────────────────────────────

def test_connection_error_not_retried():
    """Non-transient RequestException propagates immediately — no retry."""
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("refused")

    with pytest.raises(requests.ConnectionError):
        fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert session.get.call_count == 1


# ── Happy path ────────────────────────────────────────────────────────────────

def test_success_first_attempt():
    """Successful first attempt → no retry, returns response."""
    session = MagicMock()
    session.get.return_value = _response(200)

    with patch("tennis_model.ingestion.http_utils.time.sleep") as mock_sleep:
        r = fetch_with_retry(session, "http://example.com", max_attempts=3)

    assert r.status_code == 200
    assert session.get.call_count == 1
    mock_sleep.assert_not_called()


# ── Backoff timing ────────────────────────────────────────────────────────────

def test_backoff_sleep_called_between_retries():
    """sleep() must be called with exponential delay between retries."""
    session = MagicMock()
    session.get.side_effect = [_response(429), _response(429), _response(200)]

    with patch("tennis_model.ingestion.http_utils.time.sleep") as mock_sleep:
        fetch_with_retry(
            session, "http://example.com",
            max_attempts=3, base_delay=2.0
        )

    # Two retries → sleep called twice: 2.0s then 4.0s
    assert mock_sleep.call_count == 2
    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert delays[0] == pytest.approx(2.0)
    assert delays[1] == pytest.approx(4.0)
