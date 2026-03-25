"""
tests/test_blocked_diagnostic.py
=================================
Tests for the blocked-matches diagnostic module.
"""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from tennis_model.orchestration.match_runner import MatchFinalStatus, MatchRunResult
from tennis_model.tracking.blocked_diagnostic import (
    BlockedBucket,
    BlockedDiagnostic,
    format_blocked_diagnostic,
    load_and_summarize_blocked,
    load_and_summarize_blocked_range,
    merge_blocked_diagnostics,
    normalize_filter_reason,
    summarize_blocked_matches,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

class _FakeEvalDecision:
    """Minimal stub for EvaluatorDecision."""
    def __init__(self, filter_reason=None, message=None):
        self.filter_reason = filter_reason
        self.message = message


def _make_result(
    match_id: str = "2026-03-24_a_b",
    final_status: MatchFinalStatus = MatchFinalStatus.NO_PICK,
    filter_reason: str | None = None,
    qa: str = "full",
    qb: str = "full",
    reason_codes: list | None = None,
) -> MatchRunResult:
    return MatchRunResult(
        match_id=match_id,
        player_a="Player A",
        player_b="Player B",
        profile_quality_a=qa,
        profile_quality_b=qb,
        evaluator_decision=_FakeEvalDecision(filter_reason=filter_reason),
        final_status=final_status,
        reason_codes=reason_codes or [],
        filter_reason=filter_reason,
    )


# ── Test 1: empty diagnostic ─────────────────────────────────────────────────

def test_empty_diagnostic():
    diag = summarize_blocked_matches([])
    assert diag.total_matches == 0
    assert diag.picks == 0
    assert diag.watchlist == 0
    assert diag.blocked == 0
    assert diag.skipped == 0
    assert diag.by_reason == {}


# ── Test 2: correct count per reason ─────────────────────────────────────────

def test_count_by_reason():
    results = [
        _make_result("m1", MatchFinalStatus.NO_PICK, "MODEL PROB 30.3% BELOW FLOOR (40%)"),
        _make_result("m2", MatchFinalStatus.NO_PICK, "MODEL PROB 15.6% BELOW FLOOR (40%)"),
        _make_result("m3", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE \u2014 no bet"),
        _make_result("m4", MatchFinalStatus.NO_PICK, "ODDS @1.38 BELOW MINIMUM (1.5)"),
        _make_result("m5", MatchFinalStatus.NO_PICK, "SUSPICIOUS EDGE 63.1% \u2014 MANUAL REVIEW REQUIRED"),
    ]
    diag = summarize_blocked_matches(results)

    assert diag.total_matches == 5
    assert diag.blocked == 5
    assert diag.by_reason["prob_below_floor"].count == 2
    assert diag.by_reason["low_confidence"].count == 1
    assert diag.by_reason["odds_below_minimum"].count == 1
    assert diag.by_reason["suspicious_edge"].count == 1


# ── Test 3: watchlist counted correctly ──────────────────────────────────────

def test_watchlist_counted():
    results = [
        _make_result("m1", MatchFinalStatus.WATCHLIST, "EVALUATOR_WATCHLIST"),
        _make_result("m2", MatchFinalStatus.WATCHLIST, "Underdog threshold: @2.30 requires edge >= 15%"),
        _make_result("m3", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE \u2014 no bet"),
    ]
    diag = summarize_blocked_matches(results)

    assert diag.watchlist == 2
    assert diag.blocked == 1
    # watchlist entries also get reason breakdown
    assert diag.by_reason["watchlist"].count == 1
    assert diag.by_reason["underdog_threshold"].count == 1
    assert diag.by_reason["low_confidence"].count == 1


# ── Test 4: picks counted correctly ──────────────────────────────────────────

def test_picks_counted():
    results = [
        _make_result("m1", MatchFinalStatus.PICK_ALERT_SENT),
        _make_result("m2", MatchFinalStatus.PICK_DRY_RUN),
        _make_result("m3", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE \u2014 no bet"),
    ]
    diag = summarize_blocked_matches(results)

    assert diag.picks == 2
    assert diag.blocked == 1
    # picks should NOT appear in by_reason
    assert len(diag.by_reason) == 1


# ── Test 5: compat with legacy filter_reason strings ─────────────────────────

def test_legacy_filter_reason_compat():
    assert normalize_filter_reason("MODEL PROB 30.3% BELOW FLOOR (40%)") == "prob_below_floor"
    assert normalize_filter_reason("MODEL PROB 15.6% BELOW FLOOR (40%)") == "prob_below_floor"
    assert normalize_filter_reason("LOW CONFIDENCE \u2014 no bet") == "low_confidence"
    assert normalize_filter_reason("ODDS @1.38 BELOW MINIMUM (1.5)") == "odds_below_minimum"
    assert normalize_filter_reason("ODDS @2.50 BELOW MINIMUM (1.5)") == "odds_below_minimum"
    assert normalize_filter_reason("SUSPICIOUS EDGE 63.1% \u2014 MANUAL REVIEW REQUIRED") == "suspicious_edge"
    assert normalize_filter_reason("LONGSHOT_GUARD") == "longshot_guard"
    assert normalize_filter_reason("Longshot guard: @6.80 odds \u2014 watchlist only") == "longshot_guard"
    assert normalize_filter_reason("EVALUATOR_WATCHLIST") == "watchlist"
    assert normalize_filter_reason("INSUFFICIENT DATA: both players unrecognised") == "insufficient_data"
    assert normalize_filter_reason("WTA DATA GATE: Player=wta_estimated") == "wta_data_gate"
    assert normalize_filter_reason("NO MARKET ODDS") == "no_market_odds"
    assert normalize_filter_reason("Underdog threshold: @2.30 requires edge >= 15%, got 8%") == "underdog_threshold"
    assert normalize_filter_reason("") == "other"
    assert normalize_filter_reason("something completely new") == "other"


# ── Test 6: format_blocked_diagnostic does not crash ─────────────────────────

def test_format_does_not_crash():
    # Empty
    diag = BlockedDiagnostic()
    out = format_blocked_diagnostic(diag)
    assert "BLOCKED DIAGNOSTIC" in out

    # Populated
    diag.total_matches = 12
    diag.picks = 1
    diag.watchlist = 1
    diag.blocked = 10
    diag.by_reason = {
        "prob_below_floor": BlockedBucket(count=4),
        "low_confidence": BlockedBucket(count=3),
    }
    diag.by_tour = {
        "ATP": BlockedBucket(count=7),
        "WTA": BlockedBucket(count=4),
    }
    diag.by_profile_quality = {
        "full": BlockedBucket(count=24),
    }
    out = format_blocked_diagnostic(diag)
    assert "prob_below_floor" in out
    assert "low_confidence" in out
    assert "ATP" in out
    assert "WTA" in out
    assert "full" in out


# ── Test 7: tour breakdown ───────────────────────────────────────────────────

def test_tour_breakdown():
    results = [
        _make_result("m1", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE \u2014 no bet"),
        _make_result("m2", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE \u2014 no bet"),
        _make_result("m3", MatchFinalStatus.NO_PICK, "MODEL PROB 30% BELOW FLOOR (40%)"),
    ]
    tour_map = {"m1": "ATP", "m2": "WTA", "m3": "ATP"}
    diag = summarize_blocked_matches(results, tour_map=tour_map)

    assert diag.by_tour["ATP"].count == 2
    assert diag.by_tour["WTA"].count == 1


# ── Test 8: profile quality breakdown ────────────────────────────────────────

def test_profile_quality_breakdown():
    results = [
        _make_result("m1", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE", qa="full", qb="full"),
        _make_result("m2", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE", qa="full", qb="degraded"),
        _make_result("m3", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE", qa="unknown", qb="full"),
    ]
    diag = summarize_blocked_matches(results)

    assert diag.by_profile_quality["full"].count == 1
    assert diag.by_profile_quality["degraded"].count == 1
    assert diag.by_profile_quality["unknown"].count == 1


# ── Test 9: load from audit JSON ─────────────────────────────────────────────

def test_load_from_audit_json():
    audit = {
        "date": "2026-03-24",
        "matches_scanned": 12,
        "profiles_full": 24,
        "profiles_degraded": 0,
        "profiles_failed": 0,
        "no_pick_reasons": {
            "EVALUATOR_WATCHLIST": 1,
            "MODEL PROB 30.3% BELOW FLOOR (40%)": 1,
            "ODDS @1.38 BELOW MINIMUM (1.5)": 1,
            "LOW CONFIDENCE \u2014 no bet": 3,
        },
        "final_status_breakdown": {
            "PICK_DRY_RUN": 1,
            "WATCHLIST": 1,
            "NO_PICK": 10,
        },
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        audit_dir = os.path.join(tmpdir, "audits")
        os.makedirs(audit_dir)
        audit_path = os.path.join(audit_dir, "2026-03-24.json")
        with open(audit_path, "w") as f:
            json.dump(audit, f)

        with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audit_dir):
            diag = load_and_summarize_blocked("2026-03-24")

    assert diag.total_matches == 12
    assert diag.picks == 1
    assert diag.watchlist == 1
    assert diag.blocked == 10
    assert diag.by_reason["prob_below_floor"].count == 1
    assert diag.by_reason["odds_below_minimum"].count == 1
    assert diag.by_reason["low_confidence"].count == 3
    assert diag.by_reason["watchlist"].count == 1


# ── Test 10: missing audit file ──────────────────────────────────────────────

def test_load_missing_audit():
    with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", "/nonexistent"):
        diag = load_and_summarize_blocked("2099-01-01")

    assert diag.total_matches == 0
    assert len(diag.warnings) >= 1
    assert "not found" in diag.warnings[0].lower()


# ── Test 11: skipped (FAILED) counted correctly ──────────────────────────────

def test_failed_counted_as_skipped():
    results = [
        _make_result("m1", MatchFinalStatus.FAILED),
        _make_result("m2", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE"),
    ]
    diag = summarize_blocked_matches(results)

    assert diag.skipped == 1
    assert diag.blocked == 1
    # FAILED should NOT appear in by_reason
    assert "other" not in diag.by_reason or diag.by_reason.get("other", BlockedBucket()).count == 0


# ── Test 12: match_ids tracked in buckets ────────────────────────────────────

def test_match_ids_tracked():
    results = [
        _make_result("2026-03-24_korda_fritz", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE"),
        _make_result("2026-03-24_djoko_nadal", MatchFinalStatus.NO_PICK, "LOW CONFIDENCE"),
    ]
    diag = summarize_blocked_matches(results)

    bucket = diag.by_reason["low_confidence"]
    assert bucket.count == 2
    assert "2026-03-24_korda_fritz" in bucket.match_ids
    assert "2026-03-24_djoko_nadal" in bucket.match_ids


# ═══════════════════════════════════════════════════════════════════════════════
# DATE RANGE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _write_audit(tmpdir, date_str, matches_scanned, no_pick_reasons, fsb,
                 profiles_full=0, profiles_degraded=0):
    """Helper: write a minimal audit JSON to tmpdir/audits/<date>.json."""
    audit_dir = os.path.join(tmpdir, "audits")
    os.makedirs(audit_dir, exist_ok=True)
    audit = {
        "date": date_str,
        "matches_scanned": matches_scanned,
        "profiles_full": profiles_full,
        "profiles_degraded": profiles_degraded,
        "profiles_failed": 0,
        "no_pick_reasons": no_pick_reasons,
        "final_status_breakdown": fsb,
    }
    with open(os.path.join(audit_dir, f"{date_str}.json"), "w") as f:
        json.dump(audit, f)
    return audit_dir


# ── Test 13: empty range ─────────────────────────────────────────────────────

def test_range_empty():
    with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", "/nonexistent"):
        diag = load_and_summarize_blocked_range("2026-03-20", "2026-03-22")

    assert diag.total_matches == 0
    assert diag.days_missing == 3
    assert len(diag.warnings) >= 1
    assert "no audit data" in diag.warnings[0].lower()


# ── Test 14: multi-day aggregation ───────────────────────────────────────────

def test_range_aggregation():
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_dir = _write_audit(tmpdir, "2026-03-22", 10,
            {"LOW CONFIDENCE \u2014 no bet": 3, "MODEL PROB 30% BELOW FLOOR (40%)": 2},
            {"PICK_DRY_RUN": 1, "NO_PICK": 9},
            profiles_full=20)
        _write_audit(tmpdir, "2026-03-23", 8,
            {"LOW CONFIDENCE \u2014 no bet": 2, "SUSPICIOUS EDGE 50% \u2014 MANUAL REVIEW": 1},
            {"PICK_ALERT_SENT": 2, "NO_PICK": 6},
            profiles_full=16)

        with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audit_dir):
            diag = load_and_summarize_blocked_range("2026-03-22", "2026-03-23")

    assert diag.total_matches == 18
    assert diag.picks == 3
    assert diag.blocked == 15
    assert diag.days_covered == 2
    assert diag.days_missing == 0


# ── Test 15: missing day ignored ─────────────────────────────────────────────

def test_range_missing_day_ignored():
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_dir = _write_audit(tmpdir, "2026-03-20", 6,
            {"LOW CONFIDENCE \u2014 no bet": 2},
            {"PICK_DRY_RUN": 1, "NO_PICK": 5},
            profiles_full=12)
        # 2026-03-21 is missing
        _write_audit(tmpdir, "2026-03-22", 8,
            {"MODEL PROB 30% BELOW FLOOR (40%)": 3},
            {"NO_PICK": 8},
            profiles_full=16)

        with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audit_dir):
            diag = load_and_summarize_blocked_range("2026-03-20", "2026-03-22")

    assert diag.days_covered == 2
    assert diag.days_missing == 1
    assert diag.total_matches == 14
    assert any("1 day" in w for w in diag.warnings)


# ── Test 16: reason sums correct across days ─────────────────────────────────

def test_range_reason_sums():
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_dir = _write_audit(tmpdir, "2026-03-22", 10,
            {"LOW CONFIDENCE \u2014 no bet": 3, "MODEL PROB 30% BELOW FLOOR (40%)": 4},
            {"NO_PICK": 10})
        _write_audit(tmpdir, "2026-03-23", 10,
            {"LOW CONFIDENCE \u2014 no bet": 2, "MODEL PROB 15% BELOW FLOOR (40%)": 1,
             "ODDS @1.38 BELOW MINIMUM (1.5)": 1},
            {"NO_PICK": 10})

        with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audit_dir):
            diag = load_and_summarize_blocked_range("2026-03-22", "2026-03-23")

    assert diag.by_reason["low_confidence"].count == 5
    assert diag.by_reason["prob_below_floor"].count == 5
    assert diag.by_reason["odds_below_minimum"].count == 1


# ── Test 17: format range output does not crash ──────────────────────────────

def test_format_range_does_not_crash():
    diag = BlockedDiagnostic(
        total_matches=30, picks=3, watchlist=2, blocked=25,
        days_covered=3, days_missing=1,
        by_reason={
            "prob_below_floor": BlockedBucket(count=10),
            "low_confidence": BlockedBucket(count=8),
        },
    )
    out = format_blocked_diagnostic(diag)
    assert "BLOCKED DIAGNOSTIC" in out
    assert "Days covered" in out
    assert "Days missing" in out
    assert "prob_below_floor" in out


# ── Test 18: inverted range ──────────────────────────────────────────────────

def test_range_inverted():
    diag = load_and_summarize_blocked_range("2026-03-25", "2026-03-20")
    assert diag.total_matches == 0
    assert any("invalid range" in w.lower() for w in diag.warnings)


# ── Test 19: merge_blocked_diagnostics directly ─────────────────────────────

def test_merge_diagnostics():
    d1 = BlockedDiagnostic(
        total_matches=10, picks=1, watchlist=1, blocked=8,
        by_reason={"low_confidence": BlockedBucket(count=3, match_ids=["a", "b", "c"])},
        by_profile_quality={"full": BlockedBucket(count=20)},
        days_covered=1,
    )
    d2 = BlockedDiagnostic(
        total_matches=8, picks=2, watchlist=0, blocked=6,
        by_reason={
            "low_confidence": BlockedBucket(count=2, match_ids=["d", "e"]),
            "suspicious_edge": BlockedBucket(count=1, match_ids=["f"]),
        },
        by_profile_quality={"full": BlockedBucket(count=16)},
        days_covered=1,
    )

    merged = merge_blocked_diagnostics([d1, d2])

    assert merged.total_matches == 18
    assert merged.picks == 3
    assert merged.watchlist == 1
    assert merged.blocked == 14
    assert merged.days_covered == 2
    assert merged.by_reason["low_confidence"].count == 5
    assert len(merged.by_reason["low_confidence"].match_ids) == 5
    assert merged.by_reason["suspicious_edge"].count == 1
    assert merged.by_profile_quality["full"].count == 36


# ── Test 20: single-day range equals single-day load ─────────────────────────

def test_range_single_day():
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_dir = _write_audit(tmpdir, "2026-03-24", 12,
            {"LOW CONFIDENCE \u2014 no bet": 3},
            {"PICK_DRY_RUN": 1, "WATCHLIST": 1, "NO_PICK": 10},
            profiles_full=24)

        with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audit_dir):
            diag_single = load_and_summarize_blocked("2026-03-24")
            diag_range = load_and_summarize_blocked_range("2026-03-24", "2026-03-24")

    assert diag_range.total_matches == diag_single.total_matches
    assert diag_range.picks == diag_single.picks
    assert diag_range.watchlist == diag_single.watchlist
    assert diag_range.blocked == diag_single.blocked
    assert diag_range.by_reason["low_confidence"].count == diag_single.by_reason["low_confidence"].count
