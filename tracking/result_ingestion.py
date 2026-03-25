"""
tennis_model/tracking/result_ingestion.py
==========================================
Automatic match result ingestion from The Odds API /scores endpoint.

Fetches completed match winners, matches them to existing pick records by
player last-name, and falls back to manual_results/ JSON files when the
API is unavailable.

No database, no async.  Never raises — all errors are logged and swallowed.
"""
import logging
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport}/scores/"
_TIMEOUT = 10  # seconds


# ── Name matching helpers ────────────────────────────────────────────────────

def _last_name(name: str) -> str:
    """Extract last name from player name formats ('A. Surname', 'First Last')."""
    parts = name.strip().split()
    return parts[-1].lower() if parts else name.lower()


def _names_match(api_name: str, pick_name: str) -> bool:
    """True if the pick player's last name appears in the API participant name."""
    return _last_name(pick_name) in api_name.lower()


# ── Score parsing ────────────────────────────────────────────────────────────

def _determine_winner_name(scores: Optional[List[dict]]) -> Optional[str]:
    """Return the winning player's name from The Odds API scores list.

    The scores list has one entry per player with a ``"score"`` field
    representing sets won.  The player with more sets is the winner.
    Returns None if scores are missing, incomplete, or tied.
    """
    if not scores or len(scores) < 2:
        return None
    try:
        s0 = int(scores[0].get("score", "0"))
        s1 = int(scores[1].get("score", "0"))
    except (ValueError, TypeError):
        return None
    if s0 > s1:
        return scores[0].get("name")
    if s1 > s0:
        return scores[1].get("name")
    return None  # tie — shouldn't happen in tennis


# ── API fetching ─────────────────────────────────────────────────────────────

def _fetch_completed_events(api_key: str) -> List[dict]:
    """Fetch all completed tennis events from The Odds API /scores endpoint.

    Searches across all active ATP + WTA sport keys.
    Returns a flat list of completed event dicts.  Never raises.
    """
    import requests
    from tennis_model.odds_feed import _active_tennis_sport_keys

    events: List[dict] = []
    for tour in ("atp", "wta"):
        sport_keys = _active_tennis_sport_keys(api_key, tour)
        for sport_key in sport_keys:
            url = _SCORES_URL.format(sport=sport_key)
            try:
                r = requests.get(
                    url,
                    params={"apiKey": api_key, "daysFrom": 1},
                    timeout=_TIMEOUT,
                )
                r.raise_for_status()
                for ev in r.json():
                    if ev.get("completed"):
                        events.append(ev)
            except Exception as exc:
                log.warning("Scores fetch failed for %s: %s", sport_key, exc)
                continue
    return events


def fetch_completed_results(date: str) -> Dict[str, str]:
    """Fetch completed match winners from The Odds API scores endpoint.

    Loads pick records for *date*, fetches completed scores from the API,
    and matches API events to picks by player last-name.

    Returns
    -------
    dict[str, str]
        Mapping of ``match_id`` → winner side (``"A"`` or ``"B"``).
        Empty dict if the API is unavailable or no matches can be matched.

    Never raises.
    """
    from tennis_model.tracking.pick_store import load_pick_records

    picks = load_pick_records(date)
    if not picks:
        return {}

    try:
        from tennis_model.odds_feed import _get_api_key

        api_key = _get_api_key()
        if not api_key:
            log.info("No ODDS_API_KEY — skipping automatic result fetch")
            return {}

        completed = _fetch_completed_events(api_key)
        if not completed:
            log.info("No completed events from API for %s", date)
            return {}

        winners: Dict[str, str] = {}
        for pick in picks:
            match_id = pick.get("match_id", "")
            if not match_id:
                continue
            pick_a = pick.get("player_a", "")
            pick_b = pick.get("player_b", "")

            for event in completed:
                home = event.get("home_team", "")
                away = event.get("away_team", "")

                # Both players must match (order-independent)
                if not (
                    (_names_match(home, pick_a) and _names_match(away, pick_b))
                    or (_names_match(home, pick_b) and _names_match(away, pick_a))
                ):
                    continue

                winner_name = _determine_winner_name(event.get("scores"))
                if not winner_name:
                    continue

                # Map winner name → side A or B
                if _names_match(winner_name, pick_a):
                    winners[match_id] = "A"
                elif _names_match(winner_name, pick_b):
                    winners[match_id] = "B"
                break  # matched — move to next pick

        log.info(
            "Automatic results for %s: %d winner(s) from API", date, len(winners)
        )
        return winners

    except Exception as exc:
        log.warning("Automatic result fetch failed: %s", exc)
        return {}


# ── Combined winner source ───────────────────────────────────────────────────

def load_or_fetch_winners(date: str) -> Dict[str, str]:
    """Try automatic API results first, then merge manual results on top.

    Manual results override automatic ones for the same match_id,
    allowing user corrections.  Never raises.
    """
    # 1. Automatic (API)
    winners = fetch_completed_results(date)

    # 2. Manual fallback / override
    from tennis_model.tracking.auto_settlement import load_manual_winners

    manual = load_manual_winners(date)
    winners.update(manual)

    return winners
