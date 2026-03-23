"""
tests/test_profile_fetcher.py
==============================
Unit tests for ingestion/profile_fetcher.py.

Scenarios:
  1. identity unresolved → UNKNOWN
  2. identity resolved + fresh data → FULL
  3. identity resolved + cache hit (degraded quality, clean data_source) → DEGRADED from_cache=True
  4. identity resolved + fetch 429 (degraded_ratelimit data_source) → DEGRADED
  5. fetch raises exception → UNKNOWN, no exception propagated
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from tennis_model.models import PlayerProfile
from tennis_model.quality.profile_quality import ProfileQuality


def _profile(short_name, identity_source, data_source, profile_quality, ranking=50,
             hard_wins=20, hard_losses=10):
    p = PlayerProfile(short_name=short_name)
    p.full_name       = short_name
    p.identity_source = identity_source
    p.data_source     = data_source
    p.profile_quality = profile_quality
    p.ranking         = ranking
    p.hard_wins       = hard_wins
    p.hard_losses     = hard_losses
    return p


# ── 1. Unresolved identity → UNKNOWN ─────────────────────────────────────────

def test_unresolved_identity_returns_unknown():
    mock_p = _profile("J. Doe", "unresolved", "unknown", "unknown", ranking=9999,
                      hard_wins=0, hard_losses=0)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("J. Doe")
    assert qr.quality == ProfileQuality.UNKNOWN
    assert profile.profile_quality == "unknown"


# ── 2. Fresh data → FULL ──────────────────────────────────────────────────────

def test_resolved_fresh_data_returns_full():
    mock_p = _profile("N. Djokovic", "map", "tennis_abstract", "full", ranking=1)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("N. Djokovic")
    assert qr.quality == ProfileQuality.FULL
    assert profile.profile_quality == "full"
    assert qr.from_cache is False


def test_wta_dynamic_returns_full():
    mock_p = _profile("E. Rybakina", "wta_profiles", "tennis_abstract_dynamic", "full", ranking=4)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("E. Rybakina", tour="wta")
    assert qr.quality == ProfileQuality.FULL


# ── 3. Cache hit → DEGRADED with from_cache=True ─────────────────────────────

def test_cache_hit_returns_degraded_from_cache():
    """Cache hit: profile_quality=degraded but data_source looks clean."""
    mock_p = _profile("T. Etcheverry", "map", "tennis_abstract", "degraded", ranking=50)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("T. Etcheverry")
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.from_cache is True
    assert profile.profile_quality == "degraded"


# ── 4. Rate-limited, no cache → DEGRADED from_cache=False ────────────────────

def test_rate_limited_no_cache_returns_degraded():
    mock_p = _profile("X. Player", "atp_search", "degraded_ratelimit", "degraded",
                      ranking=9999, hard_wins=0, hard_losses=0)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("X. Player")
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.from_cache is False


# ── 5. Exception in fetch_player_profile → UNKNOWN, no propagation ───────────

def test_exception_returns_unknown_no_raise():
    with patch("tennis_model.pipeline.fetch_player_profile",
               side_effect=RuntimeError("network down")):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("Ghost Player")
    # Must not raise; must return UNKNOWN
    assert qr.quality == ProfileQuality.UNKNOWN
    assert qr.identity_source == "unresolved"


# ── 6. profile.profile_quality kept in sync ──────────────────────────────────

def test_profile_quality_synced_to_classification():
    """After fetch_profile_with_quality, profile.profile_quality == qr.quality.value."""
    mock_p = _profile("A. Player", "map", "tennis_abstract", "full", ranking=20)
    with patch("tennis_model.pipeline.fetch_player_profile", return_value=mock_p):
        from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
        profile, qr = fetch_profile_with_quality("A. Player")
    assert profile.profile_quality == qr.quality.value
