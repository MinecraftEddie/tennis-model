"""
tennis_model/tracking/calibration_diagnostic.py
=================================================
Calibration diagnostic: breakdowns by odds range and stake range,
plus simple warning rules to flag segments worth investigating.

Step 5 post-P6: diagnostic only — no threshold changes, no pipeline changes.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tennis_model.tracking.performance_breakdown import (
    PerformanceBucket,
    PerformanceBreakdown,
    _finalize_bucket,
    _picked_quality,
    summarize_joined_records,
)


# ── Constants ────────────────────────────────────────────────────────────────

# Minimum settled picks before a warning can fire.
_MIN_SAMPLE = 10

# ROI threshold below which a segment is "strongly negative".
_ROI_ALARM = -0.20

# Absolute ROI gap between dry_run and live to flag.
_DRY_LIVE_GAP = 0.30


# ── Odds / stake range helpers ───────────────────────────────────────────────

_ODDS_RANGES = [
    ("<1.50",      lambda o: o < 1.50),
    ("1.50-1.99",  lambda o: 1.50 <= o < 2.00),
    ("2.00-2.99",  lambda o: 2.00 <= o < 3.00),
    ("3.00+",      lambda o: o >= 3.00),
]

_STAKE_RANGES = [
    ("<0.50",      lambda s: s < 0.50),
    ("0.50-0.99",  lambda s: 0.50 <= s < 1.00),
    ("1.00+",      lambda s: s >= 1.00),
]


def _classify(value: float, ranges: list) -> str:
    for label, predicate in ranges:
        if predicate(value):
            return label
    return "unknown"


# ── CalibrationBucket (alias for consistency) ────────────────────────────────

CalibrationBucket = PerformanceBucket


# ── CalibrationDiagnostic ────────────────────────────────────────────────────

@dataclass
class CalibrationDiagnostic:
    """Full diagnostic output: existing breakdowns + odds/stake + warnings."""

    global_summary:      PerformanceBucket       = field(default_factory=PerformanceBucket)
    by_profile_quality:  Dict[str, CalibrationBucket] = field(default_factory=dict)
    by_final_status:     Dict[str, CalibrationBucket] = field(default_factory=dict)
    by_is_dry_run:       Dict[str, CalibrationBucket] = field(default_factory=dict)
    by_odds_range:       Dict[str, CalibrationBucket] = field(default_factory=dict)
    by_stake_range:      Dict[str, CalibrationBucket] = field(default_factory=dict)
    warnings:            List[str]                     = field(default_factory=list)


# ── Range bucketing ──────────────────────────────────────────────────────────

def _bucket_by_range(
    picks: List[dict],
    outcome_by_id: Dict[str, dict],
    ranges: list,
    value_key: str,
) -> Dict[str, CalibrationBucket]:
    """Group picks into range buckets and compute metrics."""
    accumulators: Dict[str, list] = defaultdict(
        lambda: [CalibrationBucket(), [], []]
    )

    for pick in picks:
        val = pick.get(value_key, 0.0) or 0.0
        label = _classify(val, ranges)
        bucket, odds_acc, stake_acc = accumulators[label]
        bucket.total_picks += 1

        odds = pick.get("odds", 0.0) or 0.0
        stake = pick.get("stake_units", 0.0) or 0.0
        if odds > 0:
            odds_acc.append(odds)
        if stake > 0:
            stake_acc.append(stake)

        outcome = outcome_by_id.get(pick.get("match_id"))
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

    result_dict: Dict[str, CalibrationBucket] = {}
    for label, (bucket, odds_acc, stake_acc) in accumulators.items():
        _finalize_bucket(bucket)
        bucket.average_odds = round(sum(odds_acc) / len(odds_acc), 4) if odds_acc else 0.0
        bucket.average_stake_units = round(sum(stake_acc) / len(stake_acc), 4) if stake_acc else 0.0
        result_dict[label] = bucket
    return result_dict


def summarize_by_odds_range(
    picks: List[dict],
    outcome_by_id: Dict[str, dict],
) -> Dict[str, CalibrationBucket]:
    return _bucket_by_range(picks, outcome_by_id, _ODDS_RANGES, "odds")


def summarize_by_stake_range(
    picks: List[dict],
    outcome_by_id: Dict[str, dict],
) -> Dict[str, CalibrationBucket]:
    return _bucket_by_range(picks, outcome_by_id, _STAKE_RANGES, "stake_units")


# ── Global summary ───────────────────────────────────────────────────────────

def _build_global(
    picks: List[dict],
    outcome_by_id: Dict[str, dict],
) -> CalibrationBucket:
    """Single bucket over all picks."""
    b = CalibrationBucket()
    odds_acc: List[float] = []
    stake_acc: List[float] = []

    for pick in picks:
        b.total_picks += 1
        odds = pick.get("odds", 0.0) or 0.0
        stake = pick.get("stake_units", 0.0) or 0.0
        if odds > 0:
            odds_acc.append(odds)
        if stake > 0:
            stake_acc.append(stake)

        outcome = outcome_by_id.get(pick.get("match_id"))
        if outcome is not None:
            result = outcome.get("result", "")
            if result in ("win", "loss"):
                b.settled_picks += 1
            if result == "win":
                b.wins += 1
            elif result == "loss":
                b.losses += 1
            b.total_stake_units += outcome.get("stake_units", 0.0) or 0.0
            b.total_profit_units += outcome.get("profit_units", 0.0) or 0.0

    _finalize_bucket(b)
    b.average_odds = round(sum(odds_acc) / len(odds_acc), 4) if odds_acc else 0.0
    b.average_stake_units = round(sum(stake_acc) / len(stake_acc), 4) if stake_acc else 0.0
    return b


# ── Warning rules ────────────────────────────────────────────────────────────

def _generate_warnings(diag: CalibrationDiagnostic) -> List[str]:
    """Simple, readable warning rules.  No stats, no ML — just thresholds."""
    warnings: List[str] = []

    # 1. Degraded quality strongly negative
    deg = diag.by_profile_quality.get("degraded")
    if deg and deg.settled_picks >= _MIN_SAMPLE and deg.roi <= _ROI_ALARM:
        warnings.append(
            f"DEGRADED quality ROI is {deg.roi:+.1%} "
            f"over {deg.settled_picks} settled picks — consider tightening quality gate"
        )

    # 2. Odds 3.00+ strongly negative
    high_odds = diag.by_odds_range.get("3.00+")
    if high_odds and high_odds.settled_picks >= _MIN_SAMPLE and high_odds.roi <= _ROI_ALARM:
        warnings.append(
            f"Odds 3.00+ ROI is {high_odds.roi:+.1%} "
            f"over {high_odds.settled_picks} settled picks — longshot segment losing"
        )

    # 3. High stake segment strongly negative
    high_stake = diag.by_stake_range.get("1.00+")
    if high_stake and high_stake.settled_picks >= _MIN_SAMPLE and high_stake.roi <= _ROI_ALARM:
        warnings.append(
            f"Stake 1.00+ ROI is {high_stake.roi:+.1%} "
            f"over {high_stake.settled_picks} settled picks — large stakes underperforming"
        )

    # 4. Dry-run vs live gap
    dry = diag.by_is_dry_run.get("dry_run")
    live = diag.by_is_dry_run.get("live")
    if (dry and live
            and dry.settled_picks >= _MIN_SAMPLE
            and live.settled_picks >= _MIN_SAMPLE):
        gap = abs(dry.roi - live.roi)
        if gap >= _DRY_LIVE_GAP:
            warnings.append(
                f"Dry-run ROI ({dry.roi:+.1%}) vs live ROI ({live.roi:+.1%}) "
                f"differ by {gap:.1%} — investigate alerting filter divergence"
            )

    return warnings


# ── Formatter ────────────────────────────────────────────────────────────

def _fmt_bucket(b: CalibrationBucket) -> str:
    """One-line summary of a bucket."""
    wr = f"{b.win_rate:.1%}" if b.settled_picks else "n/a"
    roi = f"{b.roi:+.1%}" if b.settled_picks else "n/a"
    return (
        f"picks={b.total_picks}  settled={b.settled_picks}  "
        f"W={b.wins} L={b.losses}  win_rate={wr}  "
        f"roi={roi}  stake={b.total_stake_units:.2f}  "
        f"profit={b.total_profit_units:+.2f}"
    )


def format_calibration_diagnostic(diag: CalibrationDiagnostic) -> str:
    """Return a human-readable string for terminal display."""
    lines: List[str] = []

    # Global
    lines.append("GLOBAL")
    lines.append(f"  {_fmt_bucket(diag.global_summary)}")
    lines.append("")

    # By profile quality
    lines.append("BY PROFILE QUALITY")
    for key in sorted(diag.by_profile_quality):
        lines.append(f"  {key}: {_fmt_bucket(diag.by_profile_quality[key])}")
    lines.append("")

    # By final status
    lines.append("BY FINAL STATUS")
    for key in sorted(diag.by_final_status):
        lines.append(f"  {key}: {_fmt_bucket(diag.by_final_status[key])}")
    lines.append("")

    # By dry run
    lines.append("BY DRY RUN")
    for key in sorted(diag.by_is_dry_run):
        lines.append(f"  {key}: {_fmt_bucket(diag.by_is_dry_run[key])}")
    lines.append("")

    # By odds range
    lines.append("BY ODDS RANGE")
    for label, _ in _ODDS_RANGES:
        if label in diag.by_odds_range:
            lines.append(f"  {label}: {_fmt_bucket(diag.by_odds_range[label])}")
    lines.append("")

    # By stake range
    lines.append("BY STAKE RANGE")
    for label, _ in _STAKE_RANGES:
        if label in diag.by_stake_range:
            lines.append(f"  {label}: {_fmt_bucket(diag.by_stake_range[label])}")
    lines.append("")

    # Warnings
    lines.append("WARNINGS")
    if diag.warnings:
        for w in diag.warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# ── Main builder ─────────────────────────────────────────────────────────────

def build_calibration_diagnostic(date: Optional[str] = None) -> CalibrationDiagnostic:
    """Load picks + outcomes, produce full calibration diagnostic.

    If *date* is None, uses today's date.
    """
    from tennis_model.tracking.pick_store import load_pick_records
    from tennis_model.tracking.settlement import load_outcome_records

    picks = load_pick_records(date)
    outcomes = load_outcome_records(date)

    outcome_by_id: Dict[str, dict] = {}
    for o in outcomes:
        mid = o.get("match_id")
        if mid:
            outcome_by_id[mid] = o

    # Reuse existing breakdown for quality / status / dry_run
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        global_summary=_build_global(picks, outcome_by_id),
        by_profile_quality=bd.by_profile_quality,
        by_final_status=bd.by_final_status,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, outcome_by_id),
        by_stake_range=summarize_by_stake_range(picks, outcome_by_id),
    )
    diag.warnings = _generate_warnings(diag)
    return diag
