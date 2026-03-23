"""
tests/test_profile_quality.py
==============================
Unit tests for quality/profile_quality.py.

Covers: ProfileQuality enum, QUALITY_RULES values, classify_profile_quality().
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from tennis_model.quality.profile_quality import (
    ProfileQuality,
    ProfileQualityResult,
    classify_profile_quality,
    QUALITY_RULES,
    FULL_DATA_SOURCES,
    DEGRADED_DATA_SOURCES,
)
from tennis_model.quality.reason_codes import ReasonCode


# ── ProfileQuality is a str enum ─────────────────────────────────────────────

def test_profile_quality_str_compat():
    """ProfileQuality inherits str — must compare equal to plain string."""
    assert ProfileQuality.FULL     == "full"
    assert ProfileQuality.DEGRADED == "degraded"
    assert ProfileQuality.UNKNOWN  == "unknown"


# ── QUALITY_RULES structure ──────────────────────────────────────────────────

def test_quality_rules_full():
    r = QUALITY_RULES[ProfileQuality.FULL]
    assert r["allow_pick"] is True
    assert r["confidence_penalty"] == 0.0
    assert r["stake_factor"] == 1.0


def test_quality_rules_degraded():
    r = QUALITY_RULES[ProfileQuality.DEGRADED]
    assert r["allow_pick"] is True
    assert r["confidence_penalty"] == 0.15
    assert r["stake_factor"] == 0.5


def test_quality_rules_unknown():
    r = QUALITY_RULES[ProfileQuality.UNKNOWN]
    assert r["allow_pick"] is False
    assert r["confidence_penalty"] is None
    assert r["stake_factor"] == 0.0


# ── classify_profile_quality — UNKNOWN cases ─────────────────────────────────

def test_unknown_on_unresolved_identity():
    qr = classify_profile_quality("unresolved", "unknown")
    assert qr.quality == ProfileQuality.UNKNOWN
    assert qr.reason_code == ReasonCode.IDENTITY_UNRESOLVED


def test_unknown_even_with_good_data_source_if_unresolved():
    """Identity unresolved beats any data_source — always UNKNOWN."""
    qr = classify_profile_quality("unresolved", "tennis_abstract", ranking=50)
    assert qr.quality == ProfileQuality.UNKNOWN


# ── classify_profile_quality — FULL cases ────────────────────────────────────

def test_full_on_live_atp_data():
    qr = classify_profile_quality("map", "tennis_abstract", ranking=50, surface_n=100)
    assert qr.quality == ProfileQuality.FULL
    assert qr.reason_code == ReasonCode.PROFILE_FULL
    assert qr.from_cache is False


def test_full_on_static_curated():
    qr = classify_profile_quality("map", "static_curated", ranking=10, surface_n=200)
    assert qr.quality == ProfileQuality.FULL


def test_full_on_wta_dynamic():
    qr = classify_profile_quality("wta_profiles", "tennis_abstract_dynamic", ranking=8)
    assert qr.quality == ProfileQuality.FULL


def test_full_on_atp_api():
    qr = classify_profile_quality("atp_search", "atp_api", ranking=30)
    assert qr.quality == ProfileQuality.FULL


# ── classify_profile_quality — DEGRADED cases ────────────────────────────────

def test_degraded_on_rate_limit():
    qr = classify_profile_quality("map", "degraded_ratelimit")
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.reason_code == ReasonCode.DATA_RATE_LIMITED


def test_degraded_on_timeout():
    qr = classify_profile_quality("atp_search", "degraded_timeout")
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.reason_code == ReasonCode.DATA_TIMEOUT


def test_degraded_on_empty_response():
    qr = classify_profile_quality("map", "degraded_empty")
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.reason_code == ReasonCode.DATA_EMPTY


def test_degraded_from_cache():
    """from_cache=True → DEGRADED even if data_source looks clean."""
    qr = classify_profile_quality(
        "map", "tennis_abstract", ranking=50, surface_n=100, from_cache=True
    )
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.reason_code == ReasonCode.DATA_CACHE_HIT
    assert qr.from_cache is True
    assert qr.degraded_reason == ReasonCode.DATA_CACHE_HIT


def test_degraded_on_wta_static():
    qr = classify_profile_quality("wta_profiles", "wta_static", ranking=50)
    assert qr.quality == ProfileQuality.DEGRADED


def test_degraded_on_wta_estimated():
    qr = classify_profile_quality("wta_profiles", "wta_estimated")
    assert qr.quality == ProfileQuality.DEGRADED


def test_degraded_on_unknown_data_source():
    """Identity resolved but data_source='unknown' → DEGRADED, not UNKNOWN."""
    qr = classify_profile_quality("map", "unknown")
    assert qr.quality == ProfileQuality.DEGRADED


def test_degraded_on_completely_unknown_source():
    """Unrecognised data_source — conservative fallback is DEGRADED."""
    qr = classify_profile_quality("map", "some_future_source_we_dont_know")
    assert qr.quality == ProfileQuality.DEGRADED


def test_degraded_fetch_error_overrides_data_source():
    """fetch_error is checked before data_source — 429 still gives DEGRADED."""
    qr = classify_profile_quality(
        "map", "tennis_abstract",
        fetch_error="degraded_ratelimit"
    )
    assert qr.quality == ProfileQuality.DEGRADED
    assert qr.reason_code == ReasonCode.DATA_RATE_LIMITED
    assert qr.from_cache is False


# ── ProfileQualityResult fields ───────────────────────────────────────────────

def test_result_fields_populated():
    qr = classify_profile_quality("map", "degraded_ratelimit")
    assert qr.identity_source == "map"
    assert qr.data_source == "degraded_ratelimit"
    assert isinstance(qr, ProfileQualityResult)
