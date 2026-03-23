"""
ingestion/identity.py
=====================
Player identity resolution: display name → structured IdentityResult.

Responsibility boundary
-----------------------
This module answers ONE question: who is this player?
It does NOT fetch stats, ranking, or match history — that belongs to the
profile-building layers in pipeline.py / tennis_abstract.py.

Resolution cascade
------------------
1. PLAYER_ID_MAP     — fastest, highest confidence (manually curated)
2. WTA_PROFILES      — WTA players without ATP IDs
3. ATP search HTML   — live lookup on atptour.com (medium confidence)
4. "unresolved"      — logs a warning, never raises

Sources
-------
"map"          — matched key in PLAYER_ID_MAP
"wta_profiles" — matched key in WTA_PROFILES
"atp_search"   — parsed from ATP search result HTML
"unresolved"   — identity could not be determined
"""
import logging
import re
from dataclasses import dataclass

import requests

from tennis_model.profiles import PLAYER_ID_MAP, WTA_PROFILES

log = logging.getLogger(__name__)

# Dedicated session for identity resolution (lightweight — text/html only)
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
})


@dataclass
class IdentityResult:
    """
    Outcome of a player identity resolution attempt.

    Attributes
    ----------
    full_name : str
        Resolved full name (e.g. "Novak Djokovic").
        Falls back to the input short_name when source="unresolved".
    slug : str
        URL slug used by Tennis Abstract / ATP website (e.g. "novak-djokovic").
        Empty string for WTA players and unresolved.
    atp_id : str
        4-character ATP player ID (e.g. "D643").
        Empty string for WTA players and unresolved.
    source : str
        How identity was resolved.  One of:
          "map"          — PLAYER_ID_MAP hit
          "wta_profiles" — WTA_PROFILES hit
          "atp_search"   — ATP search HTML
          "unresolved"   — no source succeeded
    """
    full_name: str
    slug:      str
    atp_id:    str
    source:    str


def resolve_identity(short_name: str, tour: str = "") -> IdentityResult:
    """
    Resolve a player's identity from their display name.

    Parameters
    ----------
    short_name : str
        Player name as it appears in match strings (e.g. "N. Djokovic").
    tour : str
        Optional tour hint ("atp" or "wta").  Not used to block any lookup
        path — provided for future filtering and logging only.

    Returns
    -------
    IdentityResult
        Always returns a result; never raises.  Callers must check
        result.source == "unresolved" to detect failure.
    """
    name_lower = short_name.lower().strip()

    # ── 1. PLAYER_ID_MAP (manually curated, fastest) ──────────────────────────
    for key, val in PLAYER_ID_MAP.items():
        if key in name_lower:
            log.info(f"[IDENTITY] map: {val[0]} ({val[2]})")
            return IdentityResult(
                full_name=val[0],
                slug=val[1],
                atp_id=val[2],
                source="map",
            )

    # ── 2. WTA_PROFILES ───────────────────────────────────────────────────────
    # WTA players have no ATP IDs; matching here avoids a pointless ATP search.
    _last = name_lower.replace(".", " ").split()[-1]
    for key, val in WTA_PROFILES.items():
        if key in name_lower or key.split()[-1] == _last:
            full_wta = val.get("full_name", short_name)
            log.info(f"[IDENTITY] wta_profiles: {full_wta}")
            return IdentityResult(
                full_name=full_wta,
                slug="",
                atp_id="",
                source="wta_profiles",
            )

    # ── 3. ATP search HTML ────────────────────────────────────────────────────
    # Parse href pattern: /players/<slug>/<ID>/overview
    parts = short_name.strip().split()
    # Use last name for the query; skip single-char initials as search terms
    query = parts[-1] if len(parts) > 1 and len(parts[0]) <= 2 else short_name
    search_url = (
        f"https://www.atptour.com/en/players"
        f"?query={requests.utils.quote(query)}"
    )
    try:
        r = _SESSION.get(search_url, timeout=12)
        r.raise_for_status()
        m = re.search(r'/players/([a-z0-9\-]+)/([A-Z0-9]{4})/overview', r.text)
        if m:
            slug = m.group(1)
            pid  = m.group(2)
            full = slug.replace("-", " ").title()
            log.info(f"[IDENTITY] atp_search: {full} | {pid}")
            return IdentityResult(
                full_name=full,
                slug=slug,
                atp_id=pid,
                source="atp_search",
            )
    except requests.RequestException as exc:
        log.warning(f"[IDENTITY] ATP search failed for '{short_name}': {exc}")

    # ── 4. Unresolved ─────────────────────────────────────────────────────────
    log.warning(
        f"[IDENTITY] Cannot resolve identity for '{short_name}'.\n"
        f"  → Add to PLAYER_ID_MAP in profiles.py:\n"
        f"    \"{name_lower.split()[-1]}\": (\"Full Name\", \"url-slug\", \"XXXX\")"
    )
    return IdentityResult(
        full_name=short_name,
        slug="",
        atp_id="",
        source="unresolved",
    )
