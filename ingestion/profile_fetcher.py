"""
ingestion/profile_fetcher.py
=============================
Clean entry point for profile fetching with quality classification (P2).

This module provides fetch_profile_with_quality(), the canonical entry point
that run_match() should use instead of calling fetch_player_profile() directly.

Design
------
The actual fetch logic stays in pipeline.fetch_player_profile() (incremental
migration).  This module:
  1. Calls fetch_player_profile() via a lazy import (avoids circular imports —
     pipeline.py imports this module at module level).
  2. Derives the from_cache signal from the profile state (heuristic).
  3. Calls classify_profile_quality() to produce a structured ProfileQualityResult.
  4. Keeps profile.profile_quality in sync with the canonical classification.
  5. Returns (profile, quality_result).

Never raises — any exception from the fetch layer is caught and results in an
UNKNOWN quality so validation will block the pick cleanly.

from_cache heuristic
---------------------
When the profile cache is applied inside _top_up_from_tennis_abstract(), the
profile ends up with:
  - profile_quality = "degraded"
  - data_source     = the *cached* source (e.g. "tennis_abstract")

This combination (degraded quality + full-looking data_source) is unique to a
cache hit; all genuine fetch failures set data_source to a degraded string.
"""
from __future__ import annotations

import logging
from typing import Tuple

from tennis_model.models import PlayerProfile
from tennis_model.quality.profile_quality import (
    ProfileQuality,
    ProfileQualityResult,
    classify_profile_quality,
    FULL_DATA_SOURCES,
)

log = logging.getLogger(__name__)


def fetch_profile_with_quality(
    short_name: str,
    tour: str = "",
) -> Tuple[PlayerProfile, ProfileQualityResult]:
    """
    Fetch a player profile and classify its data quality.

    Parameters
    ----------
    short_name : str
        Player display name (e.g. "N. Djokovic").
    tour : str
        Optional tour hint ("atp" or "wta").

    Returns
    -------
    (PlayerProfile, ProfileQualityResult)
        Always a valid pair; never raises.
        quality=UNKNOWN   → identity unresolved (validation will block).
        quality=DEGRADED  → data was cached / failed / estimated.
        quality=FULL      → live, complete fetch succeeded.
    """
    try:
        # Lazy import: pipeline.py imports this module at the top level, so a
        # module-level import of pipeline here would create a circular dependency.
        from tennis_model.pipeline import fetch_player_profile  # noqa: PLC0415
        profile = fetch_player_profile(short_name, tour)
    except Exception as exc:
        log.error(f"[FETCHER] fetch_player_profile failed for '{short_name}': {exc}")
        profile = PlayerProfile(short_name=short_name, identity_source="unresolved")
        qr = ProfileQualityResult(
            quality=ProfileQuality.UNKNOWN,
            reason_code="IDENTITY_UNRESOLVED",
            identity_source="unresolved",
            data_source="unknown",
            from_cache=False,
            degraded_reason=str(exc),
        )
        return profile, qr

    # Heuristic: cache hit = profile_quality already set to "degraded" by the
    # cache path in _top_up_from_tennis_abstract, but data_source looks like a
    # full/live source.  All genuine degraded cases use a degraded data_source.
    from_cache = (
        profile.profile_quality == "degraded"
        and profile.data_source in FULL_DATA_SOURCES
    )

    surface_n = profile.hard_wins + profile.hard_losses  # hard as proxy

    qr = classify_profile_quality(
        identity_source=profile.identity_source,
        data_source=profile.data_source,
        ranking=profile.ranking,
        surface_n=surface_n,
        from_cache=from_cache,
    )

    # Keep profile.profile_quality in sync with canonical classification
    profile.profile_quality = qr.quality.value

    log.debug(
        f"[FETCHER] {short_name}: quality={qr.quality.value} "
        f"identity={qr.identity_source} data={qr.data_source} "
        f"cache={qr.from_cache}"
    )
    return profile, qr
