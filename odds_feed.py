"""
odds_feed.py — Live odds from The Odds API.

Reads ODDS_API_KEY from environment or config.json "odds_api_key".
The Odds API uses tournament-specific sport keys (e.g. "tennis_wta_miami_open"),
not generic "tennis_wta" keys.  get_live_odds() discovers active keys dynamically
via the /sports endpoint and searches across all matching tournaments.
"""
import json
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_ODDS_API_BASE   = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
_SPORTS_ENDPOINT = "https://api.the-odds-api.com/v4/sports/"
_TIMEOUT = 10  # seconds
_sport_keys_cache: dict = {}  # tour → list[str], per session


def _get_api_key() -> str | None:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if key:
        return key
    # Check same dir as this module first (tennis_model/config.json),
    # then parent dir, then CWD — covers all invocation styles.
    _here = os.path.dirname(os.path.abspath(__file__))
    for cfg_path in [
        os.path.join(_here, "config.json"),
        os.path.join(_here, "..", "config.json"),
        "config.json",
    ]:
        if os.path.exists(cfg_path):
            break
    try:
        with open(cfg_path) as f:
            return json.load(f).get("odds_api_key", "").strip() or None
    except (OSError, json.JSONDecodeError):
        return None


def _last_name(name: str) -> str:
    """Extract last name, handling 'A. Surname' or 'FirstName Surname' formats."""
    parts = name.strip().split()
    return parts[-1].lower() if parts else name.lower()


def _names_match(api_name: str, query: str) -> bool:
    """True if query's last name appears in the API participant name."""
    return _last_name(query) in api_name.lower()


def _active_tennis_sport_keys(api_key: str, tour: str) -> list[str]:
    """
    Fetch the /sports endpoint and return all active tennis sport keys
    matching the tour ('wta' or 'atp').  The API uses per-tournament keys
    like 'tennis_wta_miami_open' rather than a single generic key.
    Result is cached for the lifetime of the process (keys don't change mid-session).
    """
    cache_key = tour.lower()
    if cache_key in _sport_keys_cache:
        return _sport_keys_cache[cache_key]
    try:
        r = requests.get(
            _SPORTS_ENDPOINT,
            params={"apiKey": api_key},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        tour_tag = tour.lower()
        keys = [
            s["key"] for s in r.json()
            if s.get("active") and tour_tag in s.get("key", "").lower()
        ]
        log.info(f"Active {tour.upper()} sport keys: {keys}")
        _sport_keys_cache[cache_key] = keys
        return keys
    except Exception as exc:
        log.warning(f"Could not fetch sport keys: {exc}")
        return []


def get_live_odds(player_a: str, player_b: str, tour: str = "wta") -> dict | None:
    """
    Fetch best available odds for a match from The Odds API.

    Discovers active tournament sport keys dynamically (the API uses keys like
    'tennis_wta_miami_open', not generic 'tennis_wta').

    Returns:
        {"odds_a": float, "odds_b": float, "bookmaker": str, "timestamp": str}
        or None if the match is not found / API unavailable.
    """
    api_key = _get_api_key()
    if not api_key:
        log.warning("ODDS_API_KEY not set — skipping live odds fetch")
        return None

    sport_keys = _active_tennis_sport_keys(api_key, tour)
    if not sport_keys:
        log.warning(f"No active {tour.upper()} sport keys found — skipping live odds fetch")
        return None

    params = {
        "apiKey":      api_key,
        "regions":     "eu",
        "markets":     "h2h",
        "oddsFormat":  "decimal",
    }

    # Search across every active tournament for this tour
    for sport in sport_keys:
        url = _ODDS_API_BASE.format(sport=sport)
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT)
            r.raise_for_status()
            events = r.json()
        except requests.Timeout:
            log.warning(f"Timeout fetching {sport} — skipping")
            continue
        except requests.HTTPError as exc:
            log.warning(f"HTTP {exc.response.status_code} for {sport} — skipping")
            continue
        except (requests.RequestException, ValueError) as exc:
            log.warning(f"Error fetching {sport}: {exc} — skipping")
            continue

        # Find the matching event within this tournament
        for event in events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            # Both players must match (order can be either way)
            if not ((_names_match(home, player_a) and _names_match(away, player_b)) or
                    (_names_match(home, player_b) and _names_match(away, player_a))):
                continue

            # Line-shop: best odds for each player across all bookmakers
            best_odds_home = 0.0
            best_odds_away = 0.0
            best_bk_home = ""
            best_bk_away = ""

            for bk in event.get("bookmakers", []):
                bk_name = bk.get("title", "unknown")
                for market in bk.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                    price_home = outcomes.get(home, 0.0)
                    price_away = outcomes.get(away, 0.0)
                    if price_home > best_odds_home:
                        best_odds_home = price_home
                        best_bk_home = bk_name
                    if price_away > best_odds_away:
                        best_odds_away = price_away
                        best_bk_away = bk_name

            if best_odds_home == 0.0 or best_odds_away == 0.0:
                log.warning(f"Match found but no valid odds: {home} vs {away}")
                return None

            # Map home/away back to player_a / player_b
            if _names_match(home, player_a):
                odds_a, odds_b = best_odds_home, best_odds_away
                bookmaker = best_bk_home if best_odds_home >= best_odds_away else best_bk_away
            else:
                odds_a, odds_b = best_odds_away, best_odds_home
                bookmaker = best_bk_away if best_odds_away >= best_odds_home else best_bk_home

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log.info(
                f"Live odds from {bookmaker} ({sport}): "
                f"{player_a} @{odds_a} / {player_b} @{odds_b}"
            )
            return {
                "odds_a":    odds_a,
                "odds_b":    odds_b,
                "bookmaker": bookmaker,
                "timestamp": timestamp,
            }

    log.warning(
        f"No live odds found for '{player_a}' vs '{player_b}' "
        f"({tour.upper()}) — searched {len(sport_keys)} tournament(s)"
    )
    return None


def fetch_slate(api_key: str | None = None) -> dict[str, list[dict]]:
    """
    Fetch the full active tennis slate from The Odds API for all tours.

    Returns {"atp": [...], "wta": [...]} where each item is:
      {player_a, player_b, odds_a, odds_b, bookmaker, sport_key, commence_time}

    Best odds are line-shopped across bookmakers (same logic as get_live_odds).
    """
    if api_key is None:
        api_key = _get_api_key()
    if not api_key:
        log.warning("ODDS_API_KEY not set — cannot fetch slate")
        return {"atp": [], "wta": []}

    result: dict[str, list[dict]] = {"atp": [], "wta": []}
    params = {
        "apiKey":     api_key,
        "regions":    "eu",
        "markets":    "h2h",
        "oddsFormat": "decimal",
    }

    for tour in ("atp", "wta"):
        sport_keys = _active_tennis_sport_keys(api_key, tour)
        for sport in sport_keys:
            url = _ODDS_API_BASE.format(sport=sport)
            try:
                r = requests.get(url, params=params, timeout=_TIMEOUT)
                r.raise_for_status()
                events = r.json()
            except Exception as exc:
                log.warning(f"fetch_slate: error fetching {sport}: {exc}")
                continue

            for event in events:
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                if not home or not away:
                    continue

                # Line-shop best odds across bookmakers
                best_home = best_away = 0.0
                best_bk = ""
                for bk in event.get("bookmakers", []):
                    bk_name = bk.get("title", "")
                    for market in bk.get("markets", []):
                        if market.get("key") != "h2h":
                            continue
                        outcomes = {o["name"]: o["price"]
                                    for o in market.get("outcomes", [])}
                        ph = outcomes.get(home, 0.0)
                        pa_price = outcomes.get(away, 0.0)
                        if ph > best_home:
                            best_home = ph
                        if pa_price > best_away:
                            best_away = pa_price
                        if ph >= pa_price and ph > 0:
                            best_bk = bk_name
                        elif pa_price > ph:
                            best_bk = bk_name

                if best_home <= 0.0 or best_away <= 0.0:
                    continue

                result[tour].append({
                    "player_a":      home,
                    "player_b":      away,
                    "odds_a":        best_home,
                    "odds_b":        best_away,
                    "bookmaker":     best_bk,
                    "sport_key":     sport,
                    "commence_time": event.get("commence_time", ""),
                })

    log.info(
        f"fetch_slate: {len(result['atp'])} ATP, {len(result['wta'])} WTA events"
    )
    return result


def print_odds_check(player_a: str, player_b: str, tour: str = "wta") -> None:
    """CLI helper: print live odds without running the model."""
    result = get_live_odds(player_a, player_b, tour)
    if result:
        print(
            f"\nLive odds  ({tour.upper()})\n"
            f"  {player_a:<30} @{result['odds_a']}\n"
            f"  {player_b:<30} @{result['odds_b']}\n"
            f"  Bookmaker : {result['bookmaker']}\n"
            f"  Fetched   : {result['timestamp']}\n"
        )
    else:
        print(f"\nNo live odds available for {player_a} vs {player_b} ({tour.upper()})\n")
