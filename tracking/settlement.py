"""
tennis_model/tracking/settlement.py
====================================
Simple, manual settlement of picks against real match outcomes.

Step 2 post-P6: attach a winner to a PickRecord, compute profit, persist.
No automatic score fetching, no database, no async.
"""
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

from tennis_model.tracking.pick_store import PickRecord, append_jsonl

log = logging.getLogger(__name__)

_OUTCOMES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "outcomes")
)


# ── OutcomeRecord dataclass ──────────────────────────────────────────────────

@dataclass
class OutcomeRecord:
    """One settled pick — links a PickRecord to a real-world result."""

    date:           str
    match_id:       str
    player_a:       str
    player_b:       str
    pick_side:      str            # "A" | "B"
    winner:         str            # "A" | "B"
    result:         str            # "win" | "loss"
    odds:           float
    stake_units:    float
    profit_units:   float
    settled_at:     str = ""       # ISO timestamp, filled at save time


# ── Core computation ─────────────────────────────────────────────────────────

def compute_profit_units(odds: float, stake_units: float, result: str) -> float:
    """Return net profit/loss for a settled pick.

    - win:  stake_units * (odds - 1)
    - loss: -stake_units
    """
    if result == "win":
        return round(stake_units * (odds - 1), 4)
    return round(-stake_units, 4)


# ── Settlement ───────────────────────────────────────────────────────────────

def settle_pick_record(pick: PickRecord, winner: str) -> OutcomeRecord:
    """Settle a PickRecord against a winner side.

    Parameters
    ----------
    pick : PickRecord
        The persisted pick to settle.
    winner : str
        "A" or "B" — which side won the real match.

    Returns
    -------
    OutcomeRecord with result and profit_units computed.

    Raises
    ------
    ValueError
        If *winner* is not "A" or "B".
    """
    winner = winner.upper()
    if winner not in ("A", "B"):
        raise ValueError(f"winner must be 'A' or 'B', got {winner!r}")

    result = "win" if pick.pick_side == winner else "loss"
    profit = compute_profit_units(pick.odds, pick.stake_units, result)

    return OutcomeRecord(
        date=pick.date,
        match_id=pick.match_id,
        player_a=pick.player_a,
        player_b=pick.player_b,
        pick_side=pick.pick_side,
        winner=winner,
        result=result,
        odds=pick.odds,
        stake_units=pick.stake_units,
        profit_units=profit,
    )


# ── Persistence ──────────────────────────────────────────────────────────────

def _outcome_file(date_str: str) -> str:
    """Return the JSONL path for a given ISO date string."""
    return os.path.join(_OUTCOMES_DIR, f"{date_str}.jsonl")


def save_outcome_record(outcome: OutcomeRecord) -> None:
    """Persist an OutcomeRecord to the daily JSONL file.

    Non-blocking: logs a warning on disk failure but never raises.
    """
    if not outcome.settled_at:
        outcome.settled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        append_jsonl(_outcome_file(outcome.date), asdict(outcome))
        log.debug("Outcome record saved: %s", outcome.match_id)
    except OSError as exc:
        log.warning("Outcome record write failed (non-blocking): %s", exc)


def load_outcome_records(date: Optional[str] = None) -> List[dict]:
    """Load outcome records from the daily JSONL file.

    If *date* is None, uses today's date.
    Returns an empty list if the file does not exist.
    """
    from datetime import date as _date_cls

    date_str = date or _date_cls.today().isoformat()
    path = _outcome_file(date_str)
    if not os.path.isfile(path):
        return []
    records: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
