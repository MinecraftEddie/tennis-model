"""
tennis_model/tracking/auto_settlement.py
=========================================
Automatic settlement of unsettled picks.

Primary winner source: The Odds API /scores endpoint (via result_ingestion).
Fallback: manual winners from data/manual_results/YYYY-MM-DD.json.

Reads picks from data/picks/, checks for existing outcomes in data/outcomes/,
loads winners from automatic + manual sources, and settles only picks that
have not already been settled.

No database, no async.
"""
import json
import logging
import os
from datetime import date as _date
from typing import Dict, Optional, Set

from tennis_model.tracking.pick_store import PickRecord, load_pick_records
from tennis_model.tracking.settlement import (
    load_outcome_records,
    save_outcome_record,
    settle_pick_record,
)

log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.normpath(os.path.join(_HERE, "..", "data"))
_MANUAL_RESULTS_DIR = os.path.join(_DATA_DIR, "manual_results")


# ── Winner source ────────────────────────────────────────────────────────────

def load_manual_winners(date: str) -> Dict[str, str]:
    """Load manual winners from data/manual_results/YYYY-MM-DD.json.

    Expected format::

        {
            "2026-03-24_sinner_alcaraz": "A",
            "2026-03-24_djokovic_nadal": "B"
        }

    Returns an empty dict if the file does not exist or is malformed.
    Never raises.
    """
    path = os.path.join(_MANUAL_RESULTS_DIR, f"{date}.json")
    if not os.path.isfile(path):
        log.info("No manual winners file for %s (expected %s)", date, path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("Manual winners file %s is not a JSON object — skipping", path)
            return {}
        # Normalise values to uppercase A/B
        winners: Dict[str, str] = {}
        for match_id, side in data.items():
            side_upper = str(side).upper()
            if side_upper in ("A", "B"):
                winners[match_id] = side_upper
            else:
                log.warning("Invalid winner '%s' for %s — skipping", side, match_id)
        return winners
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load manual winners for %s: %s", date, exc)
        return {}


# ── Settled-match dedup ──────────────────────────────────────────────────────

def load_settled_match_ids(date: Optional[str] = None) -> Set[str]:
    """Return the set of match_ids already settled for *date*.

    If *date* is None, uses today's date.
    """
    date_str = date or _date.today().isoformat()
    outcomes = load_outcome_records(date_str)
    return {o["match_id"] for o in outcomes if "match_id" in o}


# ── Main settlement logic ───────────────────────────────────────────────────

def settle_unsettled_picks(date: Optional[str] = None) -> int:
    """Settle all unsettled picks for *date*.

    Winner sources (in priority order):
        1. The Odds API /scores endpoint (automatic).
        2. Manual winners from data/manual_results/YYYY-MM-DD.json (fallback).
        Manual results override automatic ones for the same match_id.

    Steps:
        1. Load picks for the date.
        2. Load existing outcomes (already settled).
        3. Load winners from automatic + manual sources.
        4. For each pick not already settled and with a winner available,
           settle and persist the outcome.

    Returns the number of newly settled picks.
    Never raises — logs warnings on errors and continues.
    """
    date_str = date or _date.today().isoformat()

    # 1. Load picks
    picks = load_pick_records(date_str)
    if not picks:
        log.info("No picks found for %s — nothing to settle", date_str)
        return 0

    # 2. Load already-settled match IDs
    settled_ids = load_settled_match_ids(date_str)

    # 3. Load winners: automatic (API) + manual fallback
    from tennis_model.tracking.result_ingestion import load_or_fetch_winners

    winners = load_or_fetch_winners(date_str)
    if not winners:
        log.info("No winners available for %s — nothing to settle", date_str)
        return 0

    # 4. Settle unsettled picks
    settled_count = 0
    for pick_dict in picks:
        match_id = pick_dict.get("match_id", "")
        if not match_id:
            continue
        if match_id in settled_ids:
            log.debug("Already settled: %s — skipping", match_id)
            continue
        winner = winners.get(match_id)
        if winner is None:
            log.debug("No winner for %s — skipping", match_id)
            continue

        try:
            pick_record = PickRecord(**{
                k: v for k, v in pick_dict.items()
                if k in PickRecord.__dataclass_fields__
            })
            outcome = settle_pick_record(pick_record, winner)
            save_outcome_record(outcome)
            settled_ids.add(match_id)  # prevent duplicates within same run
            settled_count += 1
            log.info("Settled %s → %s (%s)", match_id, winner, outcome.result)
        except Exception as exc:
            log.warning("Failed to settle %s: %s", match_id, exc)
            continue

    log.info("Settlement complete for %s: %d newly settled", date_str, settled_count)
    return settled_count
