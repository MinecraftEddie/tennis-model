"""
tests/test_identity.py
======================
Unit tests for ingestion/identity.py — resolve_identity().

Covers:
  - resolution via PLAYER_ID_MAP
  - resolution via WTA_PROFILES
  - fallback ATP search HTML
  - unresolved returns gracefully (no exception)
"""
import sys
import os
import re

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock
from tennis_model.ingestion.identity import resolve_identity, IdentityResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_atp_search_html(slug: str, pid: str) -> str:
    """Minimal HTML that contains an ATP search result href."""
    return (
        f'<a href="/players/{slug}/{pid}/overview">'
        f"{slug.replace('-', ' ').title()}</a>"
    )


# ── Map resolution ────────────────────────────────────────────────────────────

def test_resolve_via_map_djokovic():
    """Djokovic is in PLAYER_ID_MAP — should resolve as 'map'."""
    result = resolve_identity("N. Djokovic")
    assert isinstance(result, IdentityResult)
    assert result.source == "map"
    assert result.atp_id != ""
    assert "Djokovic" in result.full_name


def test_resolve_via_map_case_insensitive():
    """Map lookup must be case-insensitive."""
    r1 = resolve_identity("N. Djokovic")
    r2 = resolve_identity("n. DJOKOVIC")
    assert r1.source == r2.source == "map"
    assert r1.atp_id == r2.atp_id


# ── WTA profiles resolution ───────────────────────────────────────────────────

def test_resolve_wta_profiles():
    """A WTA player present in WTA_PROFILES should resolve via 'wta_profiles'."""
    from tennis_model.profiles import WTA_PROFILES
    if not WTA_PROFILES:
        pytest.skip("WTA_PROFILES is empty — skip WTA resolution test")

    # Use the first entry in WTA_PROFILES for the test
    key = next(iter(WTA_PROFILES))
    sample_name = key.title()   # e.g. "rybakina" → "Rybakina"

    result = resolve_identity(sample_name)
    # Acceptable: map takes priority if the player is also in PLAYER_ID_MAP
    assert result.source in ("map", "wta_profiles"), (
        f"Expected map or wta_profiles, got {result.source!r}"
    )


def test_resolve_wta_returns_no_atp_id():
    """WTA players resolved via WTA_PROFILES must have an empty atp_id."""
    from tennis_model.profiles import WTA_PROFILES, PLAYER_ID_MAP
    if not WTA_PROFILES:
        pytest.skip("WTA_PROFILES is empty")

    # Find a WTA key that is NOT in PLAYER_ID_MAP (so it won't be map-resolved)
    wta_only_key = None
    for key in WTA_PROFILES:
        if not any(key in mk for mk in PLAYER_ID_MAP):
            wta_only_key = key
            break

    if wta_only_key is None:
        pytest.skip("All WTA_PROFILES keys also appear in PLAYER_ID_MAP — skip")

    result = resolve_identity(wta_only_key.title())
    if result.source == "wta_profiles":
        assert result.atp_id == "", "WTA_PROFILES resolution must not set atp_id"


# ── ATP search fallback ───────────────────────────────────────────────────────

def test_resolve_via_atp_search():
    """Unknown player resolved via ATP search HTML → source='atp_search'."""
    fake_html = _mock_atp_search_html("john-doe", "T000")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = fake_html
    mock_resp.raise_for_status = lambda: None

    with patch("tennis_model.ingestion.identity._SESSION") as mock_sess:
        mock_sess.get.return_value = mock_resp
        result = resolve_identity("J. Doe99_Unknown")

    assert result.source == "atp_search"
    assert result.atp_id == "T000"
    assert result.slug == "john-doe"


def test_resolve_via_atp_search_request_error():
    """ATP search network error → does not raise; falls through to 'unresolved'."""
    import requests

    with patch("tennis_model.ingestion.identity._SESSION") as mock_sess:
        mock_sess.get.side_effect = requests.ConnectionError("connection refused")
        result = resolve_identity("Z. Totally_Unknown_Player_XYZ")

    assert result.source == "unresolved"


# ── Unresolved path ───────────────────────────────────────────────────────────

def test_resolve_unresolved_no_exception():
    """Completely unknown player must return gracefully (no exception)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>no match here</html>"
    mock_resp.raise_for_status = lambda: None

    with patch("tennis_model.ingestion.identity._SESSION") as mock_sess:
        mock_sess.get.return_value = mock_resp
        result = resolve_identity("Z. Unknown_Player_9999")

    assert isinstance(result, IdentityResult)
    assert result.source == "unresolved"
    assert result.full_name == "Z. Unknown_Player_9999"   # falls back to input
    assert result.atp_id == ""
    assert result.slug == ""


def test_resolve_unresolved_fields():
    """Unresolved result must have sensible defaults — no None fields."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = ""
    mock_resp.raise_for_status = lambda: None

    with patch("tennis_model.ingestion.identity._SESSION") as mock_sess:
        mock_sess.get.return_value = mock_resp
        result = resolve_identity("Nobody_Exists_Ever")

    assert result.full_name is not None
    assert result.slug      is not None
    assert result.atp_id    is not None
    assert result.source    == "unresolved"
