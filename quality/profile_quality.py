"""
quality/profile_quality.py
===========================
ProfileQuality enum + classify_profile_quality() — centralised quality logic.

Design (P2)
-----------
ProfileQuality is the single source of truth for data-quality consequences.
classify_profile_quality() converts raw fetch signals into a structured
ProfileQualityResult.  QUALITY_RULES drives business consequences (allow_pick,
confidence_penalty, stake_factor) so callers don't hardcode thresholds.

Backward compat: ProfileQuality inherits from str, so existing code that
compares profile.profile_quality to plain strings ("full", "degraded", "unknown")
continues to work without changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from tennis_model.quality.reason_codes import ReasonCode


class ProfileQuality(str, Enum):
    """Quality tier for a player's stats profile."""
    FULL     = "full"
    DEGRADED = "degraded"
    UNKNOWN  = "unknown"


# Business consequences per quality tier.
# Used by validation (confidence_penalty), telegram (stake_factor), etc.
QUALITY_RULES: dict = {
    ProfileQuality.FULL: {
        "allow_pick":         True,
        "confidence_penalty": 0.0,
        "stake_factor":       1.0,
    },
    ProfileQuality.DEGRADED: {
        "allow_pick":         True,
        "confidence_penalty": 0.15,   # on top of validation.confidence_penalty
        "stake_factor":       0.5,    # halve stake when at least one player is degraded
    },
    ProfileQuality.UNKNOWN: {
        "allow_pick":         False,
        "confidence_penalty": None,   # N/A — pick is blocked
        "stake_factor":       0.0,
    },
}


@dataclass
class ProfileQualityResult:
    """Structured outcome of a profile quality classification."""
    quality:         ProfileQuality
    reason_code:     str            # one of ReasonCode.*
    identity_source: str
    data_source:     str
    from_cache:      bool           = False
    degraded_reason: Optional[str] = None  # human-readable cause for DEGRADED


# ── Data-source sets ──────────────────────────────────────────────────────────

# Sources that indicate live / reliable data
FULL_DATA_SOURCES: frozenset = frozenset({
    "static_curated",
    "tennis_abstract",
    "tennis_abstract_dynamic",
    "atp_api",
})

# Sources that indicate stale, estimated, or error data
DEGRADED_DATA_SOURCES: frozenset = frozenset({
    "degraded_ratelimit",
    "degraded_timeout",
    "degraded_empty",
    "wta_static",
    "wta_estimated",
    "fallback",
    "unknown",
})

# Map fetch_error strings to reason codes
_FETCH_ERROR_TO_REASON: dict = {
    "degraded_ratelimit": ReasonCode.DATA_RATE_LIMITED,
    "degraded_timeout":   ReasonCode.DATA_TIMEOUT,
    "degraded_empty":     ReasonCode.DATA_EMPTY,
}


def classify_profile_quality(
    identity_source: str,
    data_source:     str,
    ranking:         int           = 9999,
    surface_n:       int           = 0,
    from_cache:      bool          = False,
    fetch_error:     Optional[str] = None,
) -> ProfileQualityResult:
    """
    Classify a player profile into FULL / DEGRADED / UNKNOWN.

    Parameters
    ----------
    identity_source : str
        How the player was identified ("map", "wta_profiles", "atp_search",
        "unresolved").
    data_source : str
        Data source after the fetch cascade ("tennis_abstract",
        "degraded_ratelimit", etc.).
    ranking : int
        Current ranking (9999 = unknown).
    surface_n : int
        Total surface matches available (wins + losses).
    from_cache : bool
        True if stats came from the local profile cache (not a live fetch).
    fetch_error : str | None
        Error code from the fetch layer ("degraded_ratelimit", etc.).

    Returns
    -------
    ProfileQualityResult
        Never raises.
    """
    # Rule 1: identity unresolved → UNKNOWN
    if identity_source == "unresolved":
        return ProfileQualityResult(
            quality=ProfileQuality.UNKNOWN,
            reason_code=ReasonCode.IDENTITY_UNRESOLVED,
            identity_source=identity_source,
            data_source=data_source,
            from_cache=False,
            degraded_reason=None,
        )

    # Rule 2: data served from local cache → DEGRADED
    if from_cache:
        return ProfileQualityResult(
            quality=ProfileQuality.DEGRADED,
            reason_code=ReasonCode.DATA_CACHE_HIT,
            identity_source=identity_source,
            data_source=data_source,
            from_cache=True,
            degraded_reason=ReasonCode.DATA_CACHE_HIT,
        )

    # Rule 3: fetch failed after retries → DEGRADED
    if fetch_error:
        reason = _FETCH_ERROR_TO_REASON.get(fetch_error, ReasonCode.DATA_EMPTY)
        return ProfileQualityResult(
            quality=ProfileQuality.DEGRADED,
            reason_code=reason,
            identity_source=identity_source,
            data_source=data_source,
            from_cache=False,
            degraded_reason=reason,
        )

    # Rule 4: data source in degraded set → DEGRADED
    # Use the specific DATA_* code when data_source itself encodes the error type.
    if data_source in DEGRADED_DATA_SOURCES:
        _reason = _FETCH_ERROR_TO_REASON.get(data_source, ReasonCode.PROFILE_DEGRADED)
        return ProfileQualityResult(
            quality=ProfileQuality.DEGRADED,
            reason_code=_reason,
            identity_source=identity_source,
            data_source=data_source,
            from_cache=False,
            degraded_reason=_reason,
        )

    # Rule 5: data source is live/full → FULL
    if data_source in FULL_DATA_SOURCES:
        return ProfileQualityResult(
            quality=ProfileQuality.FULL,
            reason_code=ReasonCode.PROFILE_FULL,
            identity_source=identity_source,
            data_source=data_source,
            from_cache=False,
            degraded_reason=None,
        )

    # Rule 6: unknown data source — conservative fallback
    return ProfileQualityResult(
        quality=ProfileQuality.DEGRADED,
        reason_code=ReasonCode.PROFILE_DEGRADED,
        identity_source=identity_source,
        data_source=data_source,
        from_cache=False,
        degraded_reason=f"unknown_data_source:{data_source}",
    )
