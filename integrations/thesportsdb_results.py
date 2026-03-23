"""
tennis_model/integrations/thesportsdb_results.py
=================================================
TheSportsDB free-tier result source for automated match settlement.

Endpoint
--------
    GET https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=YYYY-MM-DD&l={LEAGUE_ID}

    Both ATP (4464) and WTA (4517) leagues are queried for every call so the
    caller does not need to know the tour in advance.

Return format
-------------
    {
        "status":   "final" | "not_finished" | "ambiguous" | "not_found" | "void",
        "winner":   "A" | "B" | None,
        "source":   "thesportsdb",
        "event_id": str | None,
    }

Coverage note
-------------
    TheSportsDB tennis data is community-maintained and sparse.
    Most calls will return status="not_found", which is the safe default —
    settlement is skipped, never mis-applied.  When a confident result IS
    found, it is always derived from the set-score fields (intHomeScore /
    intAwayScore) and only accepted when strStatus indicates the match is
    finished.

Name matching
-------------
    Strict canonical-key matching.  For each player name a (surname, first)
    tuple is extracted after full normalisation (lower-case, accent-strip,
    de-dot, de-hyphen, whitespace-collapse).  The LAST token is the surname;
    the FIRST token (when a second exists) is the given name or initial.

    Match rules (all post-normalisation):
      1. Exact full-string match → always accepted.
      2. Surnames must be equal AND >= 4 characters (rejects "Lee", "Kim").
      3. If both sides have a first-name component they must be compatible:
           - equal first names, OR
           - one side is a single initial that equals the other's first letter.
         Two different multi-character first names → rejected.
      4. If either side has no first-name component, the surname match alone
         is sufficient.

    Broad token containment ("query token anywhere in field") is explicitly
    NOT used — it was the source of false-positive matches.

    Ambiguous  = two or more candidates found across the search window.
    No-match   = no candidate whose home AND away fields both map to the
                 requested players.

Date search window
------------------
    event_date − 1 day, event_date, and event_date + 1 day are all queried
    (deduplication by idEvent) to absorb timezone and midnight boundary
    mismatches.
"""
import logging
import unicodedata
from datetime import date as _date, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

_BASE_URL   = "https://www.thesportsdb.com/api/v1/json/3"
_TIMEOUT    = 8  # seconds per request

# League IDs on TheSportsDB (confirmed via search_all_leagues.php?s=Tennis)
_LEAGUE_ATP = 4464
_LEAGUE_WTA = 4517
_LEAGUES    = [_LEAGUE_ATP, _LEAGUE_WTA]

# strStatus values that indicate a completed match (case-insensitive)
_STATUS_FINISHED = {
    "match finished", "ft", "finished", "final",
    "aot", "ap",      # after overtime / after penalties (edge cases)
}

# strStatus or strPostponed values that indicate void / cancelled
_STATUS_VOID = {
    "postponed", "cancelled", "canceled",
    "suspended", "abandoned", "walkover",
}


# ──────────────────────────────────────────────────────────────────────────────
# NORMALISATION HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """
    Lower-case, strip diacritics, remove dots and hyphens, collapse whitespace.
    Example: "C. Alcaraz-Garfia" → "c alcaraz garfia"
    """
    nfkd    = unicodedata.normalize("NFKD", name)
    no_acc  = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = no_acc.lower().replace(".", "").replace("-", " ")
    return " ".join(cleaned.split())


def _canonical_key(name: str) -> tuple:
    """
    Return (canonical_surname, canonical_first_or_initial) for *name*.

    The LAST token after _norm() is the surname; the FIRST token (when at
    least two tokens exist) is the given name or initial.  Single-token
    names (surname only) return ("surname", "").

    Examples:
        "C. Alcaraz"          → ("alcaraz", "c")
        "Carlos Alcaraz"      → ("alcaraz", "carlos")
        "Alcaraz"             → ("alcaraz", "")
        "B. Haddad Maia"      → ("maia",    "b")
        "Beatriz Haddad Maia" → ("maia",    "beatriz")
        "Iga Świątek"         → ("swiatek", "iga")
        "Garcia-Lopez"        → ("lopez",   "garcia")
        ""                    → ("",        "")
    """
    tokens  = _norm(name).split()
    if not tokens:
        return ("", "")
    surname = tokens[-1]
    first   = tokens[0] if len(tokens) >= 2 and tokens[0] != tokens[-1] else ""
    return (surname, first)


def _player_matches_field(query: str, field: str) -> bool:
    """
    Strict canonical-key match.  Returns True only when confident.

    Rules (all applied after _norm()):

    1. Exact full-string match → True.

    2. Canonical surnames must be equal AND >= 4 characters.
       (Lengths 1–3 are too common to be distinctive: "Lee", "Li", "Kim".)

    3. If both sides carry a first-name component they must be compatible:
       a. Equal first names         → compatible.
       b. One side is a single-char initial → compatible iff it equals the
          first character of the other side's first name.
       c. Both multi-char and unequal → NOT compatible → return False.

    4. If either side has no first-name component (surname-only query or
       surname-only field) the surname match alone is sufficient.

    Never raises.

    False-positive cases explicitly rejected by this logic:
    - Token containment:  "Lee"  vs "Tommy Lee Jones"  (surnames differ)
    - Middle-name match:  "Garcia" vs "Garcia-Lopez"   (surnames differ)
    - Same surname, diff given name: "Venus Williams" vs "Serena Williams"
    - Short surnames:     "Kim"  vs "Kim"              (len < 4)
    """
    if not query or not field:
        return False

    q_norm = _norm(query)
    f_norm = _norm(field)

    # Rule 1 — exact full-string match
    if q_norm == f_norm:
        return True

    q_sur, q_first = _canonical_key(query)
    f_sur, f_first = _canonical_key(field)

    # Rule 2 — surnames must match exactly and be long enough
    if q_sur != f_sur or len(q_sur) < 4:
        return False

    # Rule 3 — first-name compatibility when both sides carry one
    if q_first and f_first:
        if q_first == f_first:
            return True
        # Allow initial ↔ full-name: "c" ~ "carlos", "carlos" ~ "c"
        if len(q_first) == 1 or len(f_first) == 1:
            return q_first[0] == f_first[0]
        # Both multi-char but different → different players
        return False

    # Rule 4 — one side lacks a first name; surname alone is sufficient
    return True


# ──────────────────────────────────────────────────────────────────────────────
# DATE WINDOW
# ──────────────────────────────────────────────────────────────────────────────

def _date_window(event_date: str) -> list:
    """
    Return [event_date − 1 day, event_date, event_date + 1 day] as ISO strings.

    Absorbs UTC/local timezone mismatches where a match played near midnight
    may be indexed under a neighbouring date in TheSportsDB.

    Falls back to [event_date] alone if the date string cannot be parsed.
    """
    try:
        d = _date.fromisoformat(event_date)
        return [
            (d - timedelta(days=1)).isoformat(),
            event_date,
            (d + timedelta(days=1)).isoformat(),
        ]
    except ValueError:
        log.debug(f"[TSDB] _date_window: cannot parse {event_date!r} — using as-is")
        return [event_date]


# ──────────────────────────────────────────────────────────────────────────────
# API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_events_for_league(date: str, league_id: int) -> list:
    """
    Fetch all events for *date* in *league_id*.
    Returns a list (may be empty).  Never raises — errors are logged at DEBUG.
    """
    url    = f"{_BASE_URL}/eventsday.php"
    params = {"d": date, "l": league_id}
    try:
        resp   = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        events = resp.json().get("events") or []
        log.debug(
            f"[TSDB] league={league_id} date={date} → {len(events)} event(s)"
        )
        return events
    except Exception as exc:
        log.debug(
            f"[TSDB] fetch failed (league={league_id}, date={date}): {exc}"
        )
        return []


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC INTERFACE
# ──────────────────────────────────────────────────────────────────────────────

def fetch_match_result(
    player_a:   str,
    player_b:   str,
    event_date: str,
) -> dict:
    """
    Query TheSportsDB for the result of player_a vs player_b on event_date.

    Parameters
    ----------
    player_a, player_b : str
        Player display names as stored in forward_predictions.jsonl.
        Short names ("C. Alcaraz"), full names ("Carlos Alcaraz"), or
        last-name-only strings all work.
    event_date : str
        ISO date string, e.g. "2026-03-21".

    Returns
    -------
    dict  — never raises, never returns None
        {
            "status":   "final" | "not_finished" | "ambiguous"
                        | "not_found" | "void",
            "winner":   "A" | "B" | None,
            "source":   "thesportsdb",
            "event_id": str | None,
        }
    """
    log.debug(
        f"[TSDB] fetch_match_result: {player_a!r} vs {player_b!r}  date={event_date!r}"
    )

    # ── Collect candidates across date window (±1 day) and both leagues ─────
    # Deduplication by idEvent prevents the same match appearing twice when
    # its date straddles two of the three window dates.
    candidates: list = []
    seen_ids:   set  = set()
    for check_date in _date_window(event_date):
        for league_id in _LEAGUES:
            for ev in _fetch_events_for_league(check_date, league_id):
                eid = ev.get("idEvent")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)

                home = ev.get("strHomeTeam") or ""
                away = ev.get("strAwayTeam") or ""

                # Both players must map to distinct sides (either order)
                order_ab = (
                    _player_matches_field(player_a, home) and
                    _player_matches_field(player_b, away)
                )
                order_ba = (
                    _player_matches_field(player_b, home) and
                    _player_matches_field(player_a, away)
                )
                if order_ab or order_ba:
                    candidates.append((ev, order_ab))  # (event_dict, home_is_player_a)

    # ── Zero candidates → not found ───────────────────────────────────────────
    if not candidates:
        log.info(
            f"[TSDB] NOT FOUND: {player_a!r} vs {player_b!r} on {event_date}"
        )
        return {
            "status":   "not_found",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": None,
        }

    # ── Multiple candidates → ambiguous (safe skip) ───────────────────────────
    if len(candidates) > 1:
        ids = [c[0].get("idEvent") for c in candidates]
        log.warning(
            f"[TSDB] AMBIGUOUS: {player_a!r} vs {player_b!r} on {event_date} "
            f"— {len(candidates)} candidates: {ids}"
        )
        return {
            "status":   "ambiguous",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": None,
        }

    # ── Exactly one candidate ─────────────────────────────────────────────────
    ev, home_is_a = candidates[0]
    event_id      = str(ev.get("idEvent") or "")
    raw_status    = (ev.get("strStatus")    or "").lower().strip()
    postponed     = (ev.get("strPostponed") or "").lower().strip()

    log.debug(
        f"[TSDB] MATCH FOUND: event_id={event_id!r}  "
        f"strStatus={raw_status!r}  strPostponed={postponed!r}"
    )

    # ── Void / cancelled ─────────────────────────────────────────────────────
    if postponed == "yes" or raw_status in _STATUS_VOID:
        log.info(
            f"[TSDB] VOID: {player_a!r} vs {player_b!r}  "
            f"status={raw_status!r}  postponed={postponed!r}  id={event_id}"
        )
        return {
            "status":   "void",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": event_id,
        }

    # ── Not finished yet ──────────────────────────────────────────────────────
    if raw_status not in _STATUS_FINISHED:
        log.info(
            f"[TSDB] NOT FINISHED: {player_a!r} vs {player_b!r} on {event_date} "
            f"(strStatus={raw_status!r})"
        )
        return {
            "status":   "not_finished",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": event_id,
        }

    # ── Finished — determine winner from set scores ───────────────────────────
    try:
        home_sets = int(ev.get("intHomeScore") or 0)
        away_sets = int(ev.get("intAwayScore") or 0)
    except (ValueError, TypeError):
        log.warning(
            f"[TSDB] MATCH FOUND but scores unreadable: "
            f"id={event_id}  intHomeScore={ev.get('intHomeScore')!r}  "
            f"intAwayScore={ev.get('intAwayScore')!r} — skipping"
        )
        return {
            "status":   "not_finished",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": event_id,
        }

    # Equal scores with "finished" status — treat as ambiguous rather than guess
    if home_sets == away_sets:
        log.warning(
            f"[TSDB] AMBIGUOUS: equal set scores ({home_sets}-{away_sets}) "
            f"with finished status — id={event_id}"
        )
        return {
            "status":   "ambiguous",
            "winner":   None,
            "source":   "thesportsdb",
            "event_id": event_id,
        }

    # home_is_a=True  → player_a is the home player
    # home_is_a=False → player_b is the home player
    if home_sets > away_sets:
        winner_side = "A" if home_is_a else "B"
    else:
        winner_side = "B" if home_is_a else "A"

    winner_name = player_a if winner_side == "A" else player_b
    log.info(
        f"[TSDB] FINAL → winner={winner_side!r} ({winner_name})  "
        f"sets={home_sets}-{away_sets}  id={event_id}"
    )
    return {
        "status":   "final",
        "winner":   winner_side,
        "source":   "thesportsdb",
        "event_id": event_id,
    }
