"""
tests/test_p0_profile_quality.py
=================================
P0 regression tests for the identity_source / data_source / profile_quality split.

Two invariants to protect:
  1. Identity resolved + stats degraded (429, timeout, empty)
     → profile_quality="degraded", validation passes with warning
  2. Identity unresolved
     → profile_quality="unknown", validation hard fails (pre-P0 behavior preserved)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.models import PlayerProfile
from tennis_model.validation import validate_match


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_profile(
    short_name: str,
    data_source: str,
    identity_source: str,
    profile_quality: str = "unknown",
    ranking: int = 50,
) -> PlayerProfile:
    p = PlayerProfile(short_name=short_name)
    p.data_source      = data_source
    p.identity_source  = identity_source
    p.profile_quality  = profile_quality
    p.ranking          = ranking
    p.hard_wins        = 30
    p.hard_losses      = 20
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Core P0 invariants
# ──────────────────────────────────────────────────────────────────────────────

def test_degraded_ratelimit_does_not_hard_fail():
    """
    Etcheverry case: identity resolved via atp_search, Tennis Abstract 429.
    Expected: v.passed=True, warning present, confidence_penalty applied.
    """
    pa = _make_profile(
        "Tomas Martin Etcheverry",
        data_source="degraded_ratelimit",
        identity_source="atp_search",
        profile_quality="degraded",
        ranking=9999,
    )
    pb = _make_profile(
        "Tommy Paul",
        data_source="tennis_abstract",
        identity_source="map",
        profile_quality="full",
    )
    v = validate_match(pa, pb, "Hard", market_odds_a=3.94, market_odds_b=1.30)

    assert v.passed is True, (
        f"Expected passed=True for degraded_ratelimit, got errors={v.errors}"
    )
    assert v.confidence_penalty > 0, "Expected confidence_penalty for degraded source"
    assert any("degraded" in w.lower() or "VALIDATION_PROFILE_DEGRADED" in w
               for w in v.warnings), f"Expected degraded warning, got: {v.warnings}"
    assert not any("VALIDATION_SOURCE_UNKNOWN" in e for e in v.errors)


def test_degraded_timeout_does_not_hard_fail():
    """Same invariant for timeout error code."""
    pa = _make_profile("SomePlayer", "degraded_timeout", "atp_search", "degraded")
    pb = _make_profile("Tommy Paul",  "tennis_abstract",  "map",        "full")
    v  = validate_match(pa, pb, "Hard", 2.50, 1.60)

    assert v.passed is True
    assert v.confidence_penalty > 0


def test_degraded_empty_does_not_hard_fail():
    """Same invariant for empty response error code."""
    pa = _make_profile("SomePlayer", "degraded_empty", "atp_search", "degraded")
    pb = _make_profile("Tommy Paul", "tennis_abstract", "map",        "full")
    v  = validate_match(pa, pb, "Hard", 2.50, 1.60)

    assert v.passed is True
    assert v.confidence_penalty > 0


def test_wta_estimated_does_not_hard_fail():
    """WTA estimated profile (identity known via wta_profiles, stats = defaults)."""
    pa = _make_profile("Talia Gibson", "wta_estimated", "wta_profiles", "degraded")
    pb = _make_profile("E. Rybakina",  "tennis_abstract_dynamic", "wta_profiles", "full")
    v  = validate_match(pa, pb, "Hard", 7.60, 1.16)

    assert v.passed is True
    assert v.confidence_penalty > 0


# ──────────────────────────────────────────────────────────────────────────────
# Hard-fail preservation (pre-P0 behavior must be intact)
# ──────────────────────────────────────────────────────────────────────────────

def test_unresolved_identity_hard_fails():
    """
    If identity_source="unresolved", pipeline must hard fail.
    This is the case where the player is genuinely unknown — not just rate-limited.
    """
    pa = _make_profile(
        "Totally Unknown Player",
        data_source="unknown",
        identity_source="unresolved",
        profile_quality="unknown",
        ranking=9999,
    )
    pb = _make_profile("Tommy Paul", "tennis_abstract", "map", "full")
    v  = validate_match(pa, pb, "Hard", 3.00, 1.50)

    assert v.passed is False, (
        "Expected validation to fail for unresolved identity"
    )
    assert any("VALIDATION_SOURCE_UNKNOWN" in e for e in v.errors), (
        f"Expected VALIDATION_SOURCE_UNKNOWN in errors, got: {v.errors}"
    )


def test_both_unresolved_hard_fails():
    """Both players unresolved → two errors, still hard fails."""
    pa = _make_profile("Unknown A", "unknown", "unresolved", "unknown", 9999)
    pb = _make_profile("Unknown B", "unknown", "unresolved", "unknown", 9999)
    v  = validate_match(pa, pb, "Hard", 2.00, 2.00)

    assert v.passed is False
    assert len([e for e in v.errors if "VALIDATION_SOURCE_UNKNOWN" in e]) == 2


# ──────────────────────────────────────────────────────────────────────────────
# Confidence penalty accumulation
# ──────────────────────────────────────────────────────────────────────────────

def test_two_degraded_players_double_penalty():
    """Both players degraded → penalty applied for each."""
    pa = _make_profile("Player A", "degraded_ratelimit", "atp_search", "degraded")
    pb = _make_profile("Player B", "degraded_timeout",   "atp_search", "degraded")
    v  = validate_match(pa, pb, "Hard", 2.00, 2.00)

    assert v.passed is True
    assert v.confidence_penalty >= 0.30, (
        f"Expected >=0.30 penalty for two degraded players, got {v.confidence_penalty}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Safety net: unknown data_source with resolved identity
# ──────────────────────────────────────────────────────────────────────────────

def test_unknown_datasource_with_resolved_identity_is_degraded_not_failed():
    """
    Edge case: data_source never updated (old code path) but identity resolved.
    Should be treated as degraded warning, not hard fail.
    """
    pa = _make_profile("SomeKnownPlayer", "unknown", "map", "unknown")
    pb = _make_profile("Tommy Paul",      "tennis_abstract", "map", "full")
    v  = validate_match(pa, pb, "Hard", 2.50, 1.60)

    assert v.passed is True, (
        "Known identity with stale data_source should not hard fail"
    )
    assert v.confidence_penalty > 0
