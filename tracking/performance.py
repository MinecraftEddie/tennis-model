"""
tennis_model/tracking/performance.py
=====================================
Aggregate OutcomeRecords into a simple performance summary.

Step 3 post-P6: ROI / win-rate metrics from settled picks.
No database, no async, no pipeline changes.
"""
from dataclasses import dataclass
from typing import List, Optional


# ── PerformanceSummary dataclass ─────────────────────────────────────────────

@dataclass
class PerformanceSummary:
    """Aggregated metrics over a set of settled picks."""

    total_picks:         int   = 0
    settled_picks:       int   = 0
    wins:                int   = 0
    losses:              int   = 0
    win_rate:            float = 0.0
    total_stake_units:   float = 0.0
    total_profit_units:  float = 0.0
    roi:                 float = 0.0
    average_odds:        float = 0.0
    average_stake_units: float = 0.0


# ── Aggregation ──────────────────────────────────────────────────────────────

def summarize_outcomes(outcomes: List[dict]) -> PerformanceSummary:
    """Build a PerformanceSummary from a list of outcome dicts.

    Each dict is expected to have at least:
      result, odds, stake_units, profit_units
    (the shape written by save_outcome_record).
    """
    s = PerformanceSummary()
    s.total_picks = len(outcomes)

    if not outcomes:
        return s

    odds_acc: List[float] = []
    stake_acc: List[float] = []

    for o in outcomes:
        result = o.get("result", "")
        if result in ("win", "loss"):
            s.settled_picks += 1
        if result == "win":
            s.wins += 1
        elif result == "loss":
            s.losses += 1

        stake = o.get("stake_units", 0.0) or 0.0
        profit = o.get("profit_units", 0.0) or 0.0
        odds = o.get("odds", 0.0) or 0.0

        s.total_stake_units += stake
        s.total_profit_units += profit

        if odds > 0:
            odds_acc.append(odds)
        if stake > 0:
            stake_acc.append(stake)

    s.total_profit_units = round(s.total_profit_units, 4)
    s.total_stake_units = round(s.total_stake_units, 4)
    s.win_rate = round(s.wins / s.settled_picks, 4) if s.settled_picks > 0 else 0.0
    s.roi = round(s.total_profit_units / s.total_stake_units, 4) if s.total_stake_units > 0 else 0.0
    s.average_odds = round(sum(odds_acc) / len(odds_acc), 4) if odds_acc else 0.0
    s.average_stake_units = round(sum(stake_acc) / len(stake_acc), 4) if stake_acc else 0.0

    return s


# ── Convenience loader ───────────────────────────────────────────────────────

def load_and_summarize(date: Optional[str] = None) -> PerformanceSummary:
    """Load outcome records for *date* and return an aggregated summary.

    If *date* is None, uses today's date.
    """
    from tennis_model.tracking.settlement import load_outcome_records

    outcomes = load_outcome_records(date)
    return summarize_outcomes(outcomes)
