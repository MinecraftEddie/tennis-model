"""
tennis_model/tracking/blocked_diagnostic.py
=============================================
Blocked-matches diagnostic: aggregate why the model blocks picks,
broken down by reason, tour, and profile quality.

Diagnostic only — no threshold changes, no pipeline changes.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from typing import Dict, List, Optional

from tennis_model.orchestration.match_runner import (
    ALERT_SENT_STATUSES,
    EVALUATOR_BLOCKED_STATUSES,
    MatchFinalStatus,
    MatchRunResult,
)


# ── Filter-reason normalisation ──────────────────────────────────────────────

_REASON_PATTERNS: list[tuple[str, str]] = [
    (r"MODEL PROB.*BELOW FLOOR",            "prob_below_floor"),
    (r"LOW CONFIDENCE",                      "low_confidence"),
    (r"LONGSHOT",                            "longshot_guard"),
    (r"UNDERDOG.*THRESHOLD|requires edge",   "underdog_threshold"),
    (r"SUSPICIOUS EDGE",                     "suspicious_edge"),
    (r"ODDS.*BELOW MINIMUM",                "odds_below_minimum"),
    (r"INSUFFICIENT DATA",                   "insufficient_data"),
    (r"WTA DATA GATE",                       "wta_data_gate"),
    (r"NO MARKET ODDS",                      "no_market_odds"),
    (r"BLOCKED.*VALIDATION|VALIDATION.*FAIL","blocked_validation"),
    (r"EVALUATOR_WATCHLIST",                 "watchlist"),
    (r"UNRESOLVED|UNKNOWN.*IDENTITY",        "unresolved_identity"),
]


def normalize_filter_reason(reason: str) -> str:
    """Map a raw filter_reason string to a canonical bucket label."""
    if not reason:
        return "other"
    for pattern, label in _REASON_PATTERNS:
        if re.search(pattern, reason, re.IGNORECASE):
            return label
    return "other"


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class BlockedBucket:
    count: int = 0
    match_ids: List[str] = field(default_factory=list)

    def add(self, match_id: str = "") -> None:
        self.count += 1
        if match_id:
            self.match_ids.append(match_id)


@dataclass
class BlockedDiagnostic:
    total_matches: int = 0
    picks: int = 0
    watchlist: int = 0
    blocked: int = 0
    skipped: int = 0

    by_reason: Dict[str, BlockedBucket] = field(default_factory=dict)
    by_tour: Dict[str, BlockedBucket] = field(default_factory=dict)
    by_profile_quality: Dict[str, BlockedBucket] = field(default_factory=dict)

    days_covered: int = 0
    days_missing: int = 0

    warnings: List[str] = field(default_factory=list)


# ── Core builder (in-memory MatchRunResult list) ──────────────────────────────

def _profile_label(qa: str, qb: str) -> str:
    """Combine two per-player qualities into one match-level label."""
    if qa == "full" and qb == "full":
        return "full"
    if "unknown" in (qa, qb):
        return "unknown"
    return "degraded"


def summarize_blocked_matches(
    results: List[MatchRunResult],
    tour_map: Optional[Dict[str, str]] = None,
) -> BlockedDiagnostic:
    """
    Build a BlockedDiagnostic from a list of MatchRunResult objects.

    Parameters
    ----------
    results : list[MatchRunResult]
        All match results from the current scan (picks + blocked + skipped).
    tour_map : dict[str, str] | None
        Optional mapping of match_id → "ATP" | "WTA".
    """
    diag = BlockedDiagnostic()
    diag.total_matches = len(results)
    tour_map = tour_map or {}

    for r in results:
        fs = r.final_status

        # ── Classify high-level bucket ────────────────────────────────────
        if fs in ALERT_SENT_STATUSES:
            diag.picks += 1
            continue  # picks are not "blocked"
        elif fs == MatchFinalStatus.WATCHLIST:
            diag.watchlist += 1
        elif fs == MatchFinalStatus.FAILED:
            diag.skipped += 1
            continue
        else:
            diag.blocked += 1

        # ── Everything below is watchlist + blocked (not picks, not failed)

        # Reason breakdown
        reason = _extract_reason(r)
        label = normalize_filter_reason(reason)
        if label not in diag.by_reason:
            diag.by_reason[label] = BlockedBucket()
        diag.by_reason[label].add(r.match_id)

        # Tour breakdown
        tour = tour_map.get(r.match_id, "unknown")
        if tour not in diag.by_tour:
            diag.by_tour[tour] = BlockedBucket()
        diag.by_tour[tour].add(r.match_id)

        # Profile quality breakdown
        pq = _profile_label(r.profile_quality_a, r.profile_quality_b)
        if pq not in diag.by_profile_quality:
            diag.by_profile_quality[pq] = BlockedBucket()
        diag.by_profile_quality[pq].add(r.match_id)

    return diag


def _extract_reason(r: MatchRunResult) -> str:
    """Best-effort extraction of a human-readable block reason."""
    # 1. filter_reason on the result itself
    if r.filter_reason:
        return r.filter_reason
    # 2. evaluator_decision.filter_reason
    ed = r.evaluator_decision
    if ed is not None and hasattr(ed, "filter_reason") and ed.filter_reason:
        return ed.filter_reason
    # 3. evaluator_decision.message
    if ed is not None and hasattr(ed, "message") and ed.message:
        return ed.message
    return ""


# ── Audit-file builder (historical / CLI) ─────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))        # tracking/
_PKG_DIR = os.path.dirname(_HERE)                         # tennis_model/
_REPO_ROOT = os.path.dirname(_PKG_DIR)                    # <repo_root>/
_AUDITS_DIR = os.path.join(_REPO_ROOT, "data", "audits")


def load_and_summarize_blocked(target_date: Optional[str] = None) -> BlockedDiagnostic:
    """
    Build a BlockedDiagnostic from a saved audit JSON file.

    Falls back to today's date if target_date is None.
    Profile-quality and tour breakdowns use audit-level aggregates
    (per-match detail is not persisted in the audit).
    """
    if target_date is None:
        target_date = _date.today().isoformat()

    path = os.path.join(_AUDITS_DIR, f"{target_date}.json")
    if not os.path.exists(path):
        diag = BlockedDiagnostic()
        diag.warnings.append(f"Audit file not found: {path}")
        return diag

    with open(path, encoding="utf-8") as f:
        audit = json.load(f)

    diag = BlockedDiagnostic()
    diag.total_matches = audit.get("matches_scanned", 0)

    # --- High-level counts from final_status_breakdown ---
    fsb = audit.get("final_status_breakdown", {})
    for status_str, count in fsb.items():
        try:
            fs = MatchFinalStatus(status_str)
        except ValueError:
            continue
        if fs in ALERT_SENT_STATUSES:
            diag.picks += count
        elif fs == MatchFinalStatus.WATCHLIST:
            diag.watchlist += count
        elif fs == MatchFinalStatus.FAILED:
            diag.skipped += count
        else:
            diag.blocked += count

    # --- Reason breakdown from no_pick_reasons ---
    no_pick = audit.get("no_pick_reasons", {})
    for raw_reason, count in no_pick.items():
        label = normalize_filter_reason(raw_reason)
        if label not in diag.by_reason:
            diag.by_reason[label] = BlockedBucket()
        diag.by_reason[label].count += count

    # --- Profile quality (audit-level aggregate, not per-blocked-match) ---
    pf = audit.get("profiles_full", 0)
    pd_ = audit.get("profiles_degraded", 0)
    pfail = audit.get("profiles_failed", 0)
    if pf:
        diag.by_profile_quality["full"] = BlockedBucket(count=pf)
    if pd_:
        diag.by_profile_quality["degraded"] = BlockedBucket(count=pd_)
    if pfail:
        diag.by_profile_quality["unknown"] = BlockedBucket(count=pfail)

    # --- No tour breakdown available from audit ---
    if not diag.by_tour:
        diag.warnings.append("ATP/WTA breakdown not available from audit file")

    return diag


# ── Multi-day aggregation ─────────────────────────────────────────────────────

def _merge_bucket_dicts(
    target: Dict[str, BlockedBucket],
    source: Dict[str, BlockedBucket],
) -> None:
    """Merge source bucket dict into target in place."""
    for key, src_bucket in source.items():
        if key not in target:
            target[key] = BlockedBucket()
        target[key].count += src_bucket.count
        target[key].match_ids.extend(src_bucket.match_ids)


def merge_blocked_diagnostics(diagnostics: List[BlockedDiagnostic]) -> BlockedDiagnostic:
    """Merge multiple single-day diagnostics into one aggregate."""
    merged = BlockedDiagnostic()

    for d in diagnostics:
        merged.total_matches += d.total_matches
        merged.picks += d.picks
        merged.watchlist += d.watchlist
        merged.blocked += d.blocked
        merged.skipped += d.skipped
        merged.days_covered += max(d.days_covered, 1)
        merged.days_missing += d.days_missing

        _merge_bucket_dicts(merged.by_reason, d.by_reason)
        _merge_bucket_dicts(merged.by_tour, d.by_tour)
        _merge_bucket_dicts(merged.by_profile_quality, d.by_profile_quality)

    # Deduplicate warnings (keep unique only)
    seen: set[str] = set()
    for d in diagnostics:
        for w in d.warnings:
            if w not in seen:
                merged.warnings.append(w)
                seen.add(w)

    return merged


def load_and_summarize_blocked_range(
    start_date: str,
    end_date: str,
) -> BlockedDiagnostic:
    """
    Load audit files for every day in [start_date, end_date] and merge.

    Missing days are counted but silently skipped (no per-day warning).
    """
    start = _date.fromisoformat(start_date)
    end = _date.fromisoformat(end_date)

    if start > end:
        diag = BlockedDiagnostic()
        diag.warnings.append(f"Invalid range: {start_date} > {end_date}")
        return diag

    daily: list[BlockedDiagnostic] = []
    missing = 0
    current = start
    while current <= end:
        d = load_and_summarize_blocked(current.isoformat())
        if d.total_matches > 0:
            daily.append(d)
        else:
            missing += 1
        current += timedelta(days=1)

    if not daily:
        diag = BlockedDiagnostic()
        diag.days_missing = missing
        diag.warnings.append(
            f"No audit data found for range {start_date} to {end_date} "
            f"({missing} day(s) checked)"
        )
        return diag

    merged = merge_blocked_diagnostics(daily)
    merged.days_missing = missing
    # Clear per-file warnings that aren't useful at range level
    merged.warnings = [
        w for w in merged.warnings
        if "not found" not in w.lower()
    ]
    if missing:
        merged.warnings.append(f"{missing} day(s) had no audit file")

    return merged


# ── Formatter ─────────────────────────────────────────────────────────────────

def format_blocked_diagnostic(diag: BlockedDiagnostic) -> str:
    """Produce a human-readable blocked-matches diagnostic."""
    lines: list[str] = []

    def sep(char: str = "═", width: int = 56) -> str:
        return char * width

    # ── Header ────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(sep())
    lines.append("  BLOCKED DIAGNOSTIC")
    lines.append(sep())
    lines.append("")
    if diag.days_covered > 1:
        lines.append(f"  Days covered     : {diag.days_covered}")
        if diag.days_missing:
            lines.append(f"  Days missing     : {diag.days_missing}")
    lines.append(f"  Total matches    : {diag.total_matches}")
    lines.append(f"  Picks            : {diag.picks}")
    lines.append(f"  Watchlist        : {diag.watchlist}")
    lines.append(f"  Blocked          : {diag.blocked}")
    lines.append(f"  Skipped (error)  : {diag.skipped}")

    # ── Reason breakdown ──────────────────────────────────────────────────
    lines.append("")
    lines.append(sep("─"))
    lines.append("  BLOCKED BY REASON")
    lines.append(sep("─"))

    if diag.by_reason:
        sorted_reasons = sorted(
            diag.by_reason.items(), key=lambda kv: kv[1].count, reverse=True
        )
        max_label = max(len(label) for label, _ in sorted_reasons)
        for label, bucket in sorted_reasons:
            lines.append(f"  {label:<{max_label}}  : {bucket.count}")
    else:
        lines.append("  (none)")

    # ── Tour breakdown ────────────────────────────────────────────────────
    if diag.by_tour and "unknown" not in diag.by_tour:
        lines.append("")
        lines.append(sep("─"))
        lines.append("  BLOCKED BY TOUR")
        lines.append(sep("─"))
        for tour in ("ATP", "WTA"):
            b = diag.by_tour.get(tour)
            if b:
                lines.append(f"  {tour:<6}: {b.count}")

    # ── Profile quality breakdown ─────────────────────────────────────────
    if diag.by_profile_quality:
        lines.append("")
        lines.append(sep("─"))
        lines.append("  PROFILE QUALITY (all matches)")
        lines.append(sep("─"))
        for q in ("full", "degraded", "unknown"):
            b = diag.by_profile_quality.get(q)
            if b:
                lines.append(f"  {q:<10}: {b.count}")

    # ── Warnings ──────────────────────────────────────────────────────────
    if diag.warnings:
        lines.append("")
        lines.append(sep("─"))
        lines.append("  WARNINGS")
        lines.append(sep("─"))
        for w in diag.warnings:
            lines.append(f"  * {w}")

    lines.append("")
    return "\n".join(lines)
