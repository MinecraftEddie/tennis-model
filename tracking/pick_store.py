"""
tennis_model/tracking/pick_store.py
====================================
Persists PICK-path results as JSONL records in data/picks/YYYY-MM-DD.jsonl.

Step 1 post-P6: simple, non-blocking pick tracking.
No settlement, no database, no async.
"""
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

log = logging.getLogger(__name__)

_PICKS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "picks")
)


# ── PickRecord dataclass ────────────────────────────────────────────────────

@dataclass
class PickRecord:
    """One persisted pick — everything needed to review / settle later."""

    date:               str
    match_id:           str
    player_a:           str
    player_b:           str
    pick_side:          str                    # "A" | "B"
    odds:               float                  # market odds on picked side
    stake_units:        float                  # Kelly-sized stake (0.0 if blocked)
    profile_quality_a:  str                    # "full" | "degraded" | "unknown"
    profile_quality_b:  str
    evaluator_status:   str                    # EvaluatorStatus.value
    final_status:       str                    # MatchFinalStatus.value
    reason_codes:       list  = field(default_factory=list)
    confidence:         Optional[str]  = None  # "HIGH" | "MEDIUM" | "LOW"
    ev:                 Optional[float] = None
    is_dry_run:         bool  = False
    created_at:         str   = ""             # ISO timestamp, filled at save time


# ── JSONL helpers ────────────────────────────────────────────────────────────

def append_jsonl(path: str, row: dict) -> None:
    """Append a single JSON object as one line to *path*.

    Creates parent directories if needed.  Raises on I/O failure
    (caller decides whether to swallow).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pick_file(date_str: str) -> str:
    """Return the JSONL path for a given ISO date string."""
    return os.path.join(_PICKS_DIR, f"{date_str}.jsonl")


# ── Public API ───────────────────────────────────────────────────────────────

def save_pick_record(record: PickRecord) -> None:
    """Persist a PickRecord to the daily JSONL file.

    Non-blocking: logs a warning on disk failure but never raises.
    """
    if not record.created_at:
        record.created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        append_jsonl(_pick_file(record.date), asdict(record))
        log.debug("Pick record saved: %s", record.match_id)
    except OSError as exc:
        log.warning("Pick record write failed (non-blocking): %s", exc)


def load_pick_records(date: Optional[str] = None) -> List[dict]:
    """Load pick records from the daily JSONL file.

    If *date* is None, uses today's date.
    Returns an empty list if the file does not exist.
    """
    from datetime import date as _date_cls

    date_str = date or _date_cls.today().isoformat()
    path = _pick_file(date_str)
    if not os.path.isfile(path):
        return []
    records: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ── Bridge from MatchRunResult ───────────────────────────────────────────────

# Statuses that represent a real PICK worth persisting.
_PICK_STATUSES = frozenset({
    "PICK_ALERT_SENT",
    "PICK_DRY_RUN",
    "PICK_SUPPRESSED",
    "PICK_FAILED",
    "PICK_SKIPPED_DEDUPE",
})

_DRY_RUN_STATUSES = frozenset({
    "PICK_DRY_RUN",
})


def maybe_record_pick(result) -> Optional[PickRecord]:
    """Build and save a PickRecord from a MatchRunResult if it's a PICK path.

    Returns the PickRecord if saved, None otherwise.
    Non-blocking: never raises.
    """
    try:
        fs = result.final_status
        status_val = fs.value if hasattr(fs, "value") else str(fs)

        if status_val not in _PICK_STATUSES:
            return None

        pick = result.pick
        if pick is None:
            return None

        # Determine picked side
        picked = pick.picked_side()
        if picked is None:
            return None

        side = picked["side"]
        odds = picked["market_odds"] or 0.0
        ev = picked["ev"]

        # Stake: prefer risk_decision.stake_units, fall back to pick.stake_units
        stake = 0.0
        if result.risk_decision is not None and hasattr(result.risk_decision, "stake_units"):
            stake = result.risk_decision.stake_units or 0.0
        elif pick.stake_units is not None:
            stake = pick.stake_units

        # Confidence from evaluator_decision if available, else from pick
        confidence = None
        ed = result.evaluator_decision
        if ed is not None and hasattr(ed, "confidence") and ed.confidence is not None:
            confidence = str(ed.confidence)
        elif pick.confidence:
            confidence = pick.confidence

        # Evaluator status
        ev_status = ""
        if ed is not None and hasattr(ed, "status"):
            ev_status = ed.status.value if hasattr(ed.status, "value") else str(ed.status)

        from datetime import date as _date_cls
        today = _date_cls.today().isoformat()

        record = PickRecord(
            date=today,
            match_id=result.match_id,
            player_a=result.player_a,
            player_b=result.player_b,
            pick_side=side,
            odds=odds,
            stake_units=stake,
            profile_quality_a=result.profile_quality_a,
            profile_quality_b=result.profile_quality_b,
            evaluator_status=ev_status,
            final_status=status_val,
            reason_codes=list(result.reason_codes) if result.reason_codes else [],
            confidence=confidence,
            ev=round(ev, 4) if ev is not None else None,
            is_dry_run=status_val in _DRY_RUN_STATUSES,
        )

        save_pick_record(record)
        return record

    except Exception as exc:
        log.warning("maybe_record_pick failed (non-blocking): %s", exc)
        return None
