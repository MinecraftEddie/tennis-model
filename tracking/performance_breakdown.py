"""
tennis_model/tracking/performance_breakdown.py
===============================================
Breakdown performance by profile quality, final status, and dry-run flag.

Step 4 post-P6: joins picks + outcomes, groups into buckets, computes
the same metrics as PerformanceSummary per bucket.
No database, no async, no pipeline changes.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Bucket dataclass ─────────────────────────────────────────────────────────

@dataclass
class PerformanceBucket:
    """Metrics for one slice of picks (same shape as PerformanceSummary)."""

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


@dataclass
class PerformanceBreakdown:
    """Three breakdown axes over the same set of picks."""

    by_profile_quality: Dict[str, PerformanceBucket] = field(default_factory=dict)
    by_final_status:    Dict[str, PerformanceBucket] = field(default_factory=dict)
    by_is_dry_run:      Dict[str, PerformanceBucket] = field(default_factory=dict)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _finalize_bucket(b: PerformanceBucket) -> None:
    """Compute derived metrics in place after accumulation."""
    b.total_profit_units = round(b.total_profit_units, 4)
    b.total_stake_units = round(b.total_stake_units, 4)
    b.win_rate = round(b.wins / b.settled_picks, 4) if b.settled_picks > 0 else 0.0
    b.roi = round(b.total_profit_units / b.total_stake_units, 4) if b.total_stake_units > 0 else 0.0


def _picked_quality(pick: dict) -> str:
    """Return the profile quality of the picked side."""
    side = pick.get("pick_side", "")
    if side == "A":
        return pick.get("profile_quality_a", "unknown")
    if side == "B":
        return pick.get("profile_quality_b", "unknown")
    return "unknown"


# ── Core aggregation ─────────────────────────────────────────────────────────

def summarize_joined_records(
    picks: List[dict],
    outcomes: List[dict],
) -> PerformanceBreakdown:
    """Join picks with outcomes on match_id and produce breakdowns.

    - Picks without an outcome count toward total_picks but not settled_picks.
    - Orphan outcomes (no matching pick) are silently ignored.
    """
    outcome_by_id: Dict[str, dict] = {}
    for o in outcomes:
        mid = o.get("match_id")
        if mid:
            outcome_by_id[mid] = o

    # Accumulators: axis_name → key → (bucket, odds_list, stake_list)
    axes: Dict[str, Dict[str, list]] = {
        "quality":  defaultdict(lambda: [PerformanceBucket(), [], []]),
        "status":   defaultdict(lambda: [PerformanceBucket(), [], []]),
        "dry_run":  defaultdict(lambda: [PerformanceBucket(), [], []]),
    }

    for pick in picks:
        q_key = _picked_quality(pick)
        s_key = pick.get("final_status", "unknown")
        d_key = "dry_run" if pick.get("is_dry_run") else "live"

        outcome = outcome_by_id.get(pick.get("match_id"))

        for axis, key in [("quality", q_key), ("status", s_key), ("dry_run", d_key)]:
            bucket, odds_acc, stake_acc = axes[axis][key]
            bucket.total_picks += 1

            odds = pick.get("odds", 0.0) or 0.0
            stake = pick.get("stake_units", 0.0) or 0.0
            if odds > 0:
                odds_acc.append(odds)
            if stake > 0:
                stake_acc.append(stake)

            if outcome is not None:
                result = outcome.get("result", "")
                if result in ("win", "loss"):
                    bucket.settled_picks += 1
                if result == "win":
                    bucket.wins += 1
                elif result == "loss":
                    bucket.losses += 1

                bucket.total_stake_units += outcome.get("stake_units", 0.0) or 0.0
                bucket.total_profit_units += outcome.get("profit_units", 0.0) or 0.0

    # Finalize
    breakdown = PerformanceBreakdown()
    for axis_name, mapping in axes.items():
        out: Dict[str, PerformanceBucket] = {}
        for key, (bucket, odds_acc, stake_acc) in mapping.items():
            _finalize_bucket(bucket)
            bucket.average_odds = round(sum(odds_acc) / len(odds_acc), 4) if odds_acc else 0.0
            bucket.average_stake_units = round(sum(stake_acc) / len(stake_acc), 4) if stake_acc else 0.0
            out[key] = bucket
        if axis_name == "quality":
            breakdown.by_profile_quality = out
        elif axis_name == "status":
            breakdown.by_final_status = out
        elif axis_name == "dry_run":
            breakdown.by_is_dry_run = out

    return breakdown


# ── Convenience loader ───────────────────────────────────────────────────────

def load_and_summarize_breakdown(date: Optional[str] = None) -> PerformanceBreakdown:
    """Load picks + outcomes for *date* and return breakdowns.

    If *date* is None, uses today's date.
    """
    from tennis_model.tracking.pick_store import load_pick_records
    from tennis_model.tracking.settlement import load_outcome_records

    picks = load_pick_records(date)
    outcomes = load_outcome_records(date)
    return summarize_joined_records(picks, outcomes)
