"""
tennis_model/tracking/settle_predictions.py
============================================
Settle logged forward predictions against known match results.

Source : data/forward_predictions.jsonl  (written by prediction_logger.py)
Output : data/settled_predictions.jsonl  (appended; source file never touched)

Usage
-----
    from tennis_model.tracking.settle_predictions import settle, void_match

    # By player name (fuzzy, same logic as backtest.record_result)
    settle("2026-03-21_sinner_alcaraz", winner="Sinner")

    # Or by explicit side
    settle("2026-03-21_sinner_alcaraz", winner="A")

    # With closing odds for CLV tracking
    settle("2026-03-21_sinner_alcaraz", winner="Sinner", closing_odds=2.05)

    # Void (retired / cancelled before policy threshold)
    void_match("2026-03-21_sinner_alcaraz", notes="Retired 3rd set")

Settlement rules
----------------
  is_pick = False                    → NO_BET,  pnl = 0
  is_pick = True, side matches       → WIN,     pnl = settled_odds - 1
  is_pick = True, side doesn't match → LOSS,    pnl = -1
  void / cancelled                   → VOID,    pnl = 0
  no result supplied                 → UNSETTLED (use settle_unsettled helper)

Assumes 1-unit stake per pick.
"""
import json
import logging
import os
import unicodedata
from datetime import datetime
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_DATA_DIR      = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
_FORWARD_FILE  = os.path.join(_DATA_DIR, "forward_predictions.jsonl")
_SETTLED_FILE  = os.path.join(_DATA_DIR, "settled_predictions.jsonl")


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _load_forward() -> list:
    """Return all records from forward_predictions.jsonl."""
    if not os.path.exists(_FORWARD_FILE):
        return []
    records = []
    with open(_FORWARD_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning(f"Skipping malformed line in {_FORWARD_FILE}")
    return records


def _settled_ids() -> set:
    """Return set of match_ids already present in settled_predictions.jsonl.

    Excludes records where settlement_confidence is "no_match" or "ambiguous":
    those are failed auto-settlement attempts (winner name could not be resolved)
    that should not permanently block a subsequent correct settle() call.

    Intentionally-settled records (WIN/LOSS/VOID/NO_BET) and intentionally-
    unsettled records written by mark_unsettled() (settlement_confidence=None)
    still count and block re-settlement as before.
    """
    if not os.path.exists(_SETTLED_FILE):
        return set()
    ids = set()
    _failed = {"no_match", "ambiguous"}
    with open(_SETTLED_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    if rec.get("settlement_confidence") not in _failed:
                        ids.add(rec["match_id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    return ids


def _append(record: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SETTLED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _current_bankroll() -> float:
    """Return the last bankroll_after from settled file, or BANKROLL_START env var (default 1000.0)."""
    from tennis_model.config.runtime_config import BANKROLL_START as _BR_START
    start = _BR_START
    if not os.path.exists(_SETTLED_FILE):
        return start
    last = None
    with open(_SETTLED_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    if rec.get("bankroll_after") is not None:
                        last = rec["bankroll_after"]
                except (json.JSONDecodeError, KeyError):
                    pass
    return last if last is not None else start


def normalize(name: str) -> str:
    """Lower, remove accents, remove dots, collapse whitespace."""
    nfkd = unicodedata.normalize("NFD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = no_accents.lower().replace(".", "")
    return " ".join(cleaned.split())


def _names_match(winner: str, player: str) -> bool:
    """
    Strict normalized name match. Accepts:
      - exact equality after normalize()
      - all tokens of winner are in player tokens, each token >= 3 chars,
        and at least one token >= 4 chars (prevents initial-only matches).
    Rejects partial substrings, single initials, and 2-char tokens.
    """
    w = normalize(winner)
    p = normalize(player)
    if w == p:
        return True
    w_tokens = w.split()
    p_tokens = p.split()
    if not all(t in p_tokens for t in w_tokens):
        return False
    if not any(len(t) >= 4 for t in w_tokens):
        return False
    return True


def _find_forward(match_id: str) -> Optional[dict]:
    for rec in _load_forward():
        if rec.get("match_id") == match_id:
            return rec
    return None


def _resolve_side(forward: dict, winner: str) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Map *winner* to ("A"|"B", confidence, warning).

    confidence values: "manual" (explicit A/B), "name_match", "ambiguous", "no_match"
    Returns (None, reason, warning_message) when the match cannot be made safely.
    Never raises — callers must handle the None case.
    """
    upper = winner.strip().upper()
    if upper in ("A", "B"):
        return upper, "manual", None

    match_a = _names_match(winner, forward.get("player_a", ""))
    match_b = _names_match(winner, forward.get("player_b", ""))

    if match_a and match_b:
        msg = (f"Ambiguous: {winner!r} matches both "
               f"player_a={forward['player_a']!r} and player_b={forward['player_b']!r} "
               f"— use 'A' or 'B' explicitly")
        log.warning(msg)
        return None, "ambiguous", msg

    if match_a:
        return "A", "name_match", None
    if match_b:
        return "B", "name_match", None

    msg = (f"No match: {winner!r} does not safely match "
           f"player_a={forward.get('player_a')!r} or player_b={forward.get('player_b')!r}")
    log.warning(msg)
    return None, "no_match", msg


def _build_record(forward: dict, result: str, winner_side: Optional[str],
                  closing_odds: Optional[float], notes: str,
                  settlement_confidence: Optional[str] = None,
                  settlement_warning: Optional[str] = None,
                  stake: float = 1.0) -> dict:
    """Assemble the settled record from a forward prediction + settlement inputs."""
    picked_side  = forward.get("picked_side")
    settled_odds = forward.get("odds_a") if picked_side == "A" else (
                   forward.get("odds_b") if picked_side == "B" else None)

    if result == "WIN":
        pnl_units = round(stake * ((settled_odds or 1.0) - 1.0), 4)
    elif result == "LOSS":
        pnl_units = round(-stake, 4)
    else:
        pnl_units = 0.0

    roi_percent = round(pnl_units * 100, 2) if result in ("WIN", "LOSS") else None

    clv_percent = None
    if closing_odds and settled_odds and closing_odds > 1.0:
        clv_percent = round((settled_odds / closing_odds - 1.0) * 100, 2)

    return {
        # Identifiers
        "timestamp":                datetime.utcnow().isoformat() + "Z",
        "date":                     forward.get("date"),
        "match_id":                 forward.get("match_id"),
        "player_a":                 forward.get("player_a"),
        "player_b":                 forward.get("player_b"),
        "picked_side":              picked_side,
        "is_pick":                  forward.get("is_pick", False),
        # Original prediction fields (carried through for analysis)
        "odds_a":                   forward.get("odds_a"),
        "odds_b":                   forward.get("odds_b"),
        "edge_a":                   forward.get("edge_a"),
        "edge_b":                   forward.get("edge_b"),
        "confidence":               forward.get("confidence"),
        "quality_tier":             forward.get("quality_tier"),
        "evaluator_decision":       forward.get("evaluator_decision"),
        "blocked_reason":           forward.get("blocked_reason"),
        "model_version":            forward.get("model_version"),
        # Settlement fields
        "winner":                   winner_side,
        "result":                   result,
        "settled_odds":             settled_odds,
        "pnl_units":                pnl_units,
        "roi_percent":              roi_percent,
        "settled_at":               datetime.utcnow().isoformat() + "Z",
        # CLV (optional — only when closing_odds supplied)
        "closing_odds_picked_side": closing_odds,
        "clv_percent":              clv_percent,
        "notes":                    notes or None,
        # Settlement provenance / safety audit
        "settlement_source":        "auto",
        "settlement_confidence":    settlement_confidence,
        "settlement_warning":       settlement_warning,
    }


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

def settle(match_id: str, winner: str,
           closing_odds: Optional[float] = None,
           notes: str = "") -> dict:
    """
    Settle a forward prediction as WIN, LOSS, or NO_BET.

    Args:
        match_id:     key from forward_predictions.jsonl
        winner:       player name, "A", or "B"
        closing_odds: (optional) closing line for the picked side → enables CLV
        notes:        (optional) free-text annotation

    Returns the settled record.
    Raises ValueError if match_id is unknown or winner is ambiguous.
    Skips silently (logs warning) if already settled.
    """
    forward = _find_forward(match_id)
    if forward is None:
        raise ValueError(f"No forward prediction found for match_id={match_id!r}")

    if match_id in _settled_ids():
        log.warning(f"Already settled — skipping duplicate: {match_id}")
        return {}

    winner_side, confidence, warning = _resolve_side(forward, winner)

    if winner_side is None:
        log.warning(f"Settlement rejected — cannot safely identify winner: {match_id} | {warning}")
        bankroll_now = _current_bankroll()
        record = _build_record(forward, "UNSETTLED", None, None,
                               "SETTLEMENT_MATCH_FAILED",
                               settlement_confidence=confidence,
                               settlement_warning=warning)
        record["bankroll_before"] = round(bankroll_now, 4)
        record["stake_units"]     = 0.0
        record["bankroll_after"]  = round(bankroll_now, 4)
        _append(record)
        return record

    is_pick     = forward.get("is_pick", False)
    picked_side = forward.get("picked_side")

    if not is_pick:
        result = "NO_BET"
    elif picked_side == winner_side:
        result = "WIN"
    else:
        result = "LOSS"

    bankroll_now = _current_bankroll()
    fwd_stake    = forward.get("stake_units") if result in ("WIN", "LOSS") else None
    stake_units  = fwd_stake if fwd_stake is not None else (
                       1.0 if result in ("WIN", "LOSS") else 0.0)
    record = _build_record(forward, result, winner_side, closing_odds, notes,
                           settlement_confidence=confidence,
                           settlement_warning=warning,
                           stake=stake_units)
    record["bankroll_before"] = round(bankroll_now, 4)
    record["stake_units"]     = stake_units
    record["bankroll_after"]  = round(bankroll_now + record["pnl_units"], 4)
    _append(record)

    pl_str = f"+{record['pnl_units']:.3f}" if record["pnl_units"] > 0 else f"{record['pnl_units']:.3f}"
    log.info(f"Settled: {match_id}  result={result}  P&L={pl_str}")

    # Update ELO ratings for confirmed WIN/LOSS only
    if result in ("WIN", "LOSS"):
        winner_name = forward.get("player_a") if winner_side == "A" else forward.get("player_b")
        loser_name  = forward.get("player_b") if winner_side == "A" else forward.get("player_a")
        try:
            from tennis_model.elo import get_elo_engine, canonical_id
            get_elo_engine().update(
                winner_id=canonical_id(winner_name),
                loser_id=canonical_id(loser_name),
                surface=forward.get("surface", "Hard"),
                tournament_level=forward.get("tournament_level", "atp_250"),
            )
            log.info(f"ELO updated: {winner_name} beat {loser_name}")
        except Exception as exc:
            log.warning(f"ELO update skipped: {exc}")

    return record


def void_match(match_id: str, notes: str = "") -> dict:
    """
    Mark a match VOID (retired / cancelled before settlement policy threshold).
    P&L = 0 regardless of pick status.
    """
    forward = _find_forward(match_id)
    if forward is None:
        raise ValueError(f"No forward prediction found for match_id={match_id!r}")

    if match_id in _settled_ids():
        log.warning(f"Already settled — skipping duplicate: {match_id}")
        return {}

    bankroll_now = _current_bankroll()
    record = _build_record(forward, "VOID", None, None, notes)
    record["bankroll_before"] = round(bankroll_now, 4)
    record["stake_units"]     = 0.0
    record["bankroll_after"]  = round(bankroll_now, 4)
    _append(record)
    log.info(f"Voided: {match_id}")
    return record


def mark_unsettled(match_id: str, notes: str = "") -> dict:
    """
    Explicitly mark a prediction UNSETTLED (result unknown / data missing).
    Use when you want a record in the settled file without guessing the outcome.
    """
    forward = _find_forward(match_id)
    if forward is None:
        raise ValueError(f"No forward prediction found for match_id={match_id!r}")

    if match_id in _settled_ids():
        log.warning(f"Already settled — skipping duplicate: {match_id}")
        return {}

    bankroll_now = _current_bankroll()
    record = _build_record(forward, "UNSETTLED", None, None, notes)
    record["bankroll_before"] = round(bankroll_now, 4)
    record["stake_units"]     = 0.0
    record["bankroll_after"]  = round(bankroll_now, 4)
    _append(record)
    log.info(f"Marked unsettled: {match_id}")
    return record


def pending() -> list:
    """
    Return all forward predictions that have not yet been settled.
    Useful for checking what still needs a result recorded.
    """
    done = _settled_ids()
    return [r for r in _load_forward() if r.get("match_id") not in done]
