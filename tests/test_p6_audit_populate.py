"""
tests/test_p6_audit_populate.py
=================================
P6 tests — DailyAudit P6 additions: reason_code_breakdown field,
record_match_result() profile/pick/alert increments, and the
populate_from_scan_results() early-exit when final_status_breakdown is set.

Coverage:
  A. reason_code_breakdown field exists on DailyAudit
  B. record_match_result() populates profile quality counters
  C. record_match_result() populates reason_code_breakdown
  D. record_match_result() increments picks_generated
  E. record_match_result() increments alerts_eligible/sent/suppressed
  F. record_match_result() populates no_pick_reasons via filter_reason
  G. populate_from_scan_results() skips when final_status_breakdown non-empty
  H. populate_from_scan_results() always increments profiles_failed for skipped
  I. populate_from_scan_results() uses legacy logic when final_status_breakdown empty
  J. save_audit_json() includes reason_code_breakdown
  K. Non-regression: P3/P4/P5 counters still work alongside P6 additions
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from unittest.mock import MagicMock

from tennis_model.orchestration.audit import DailyAudit
from tennis_model.orchestration.match_runner import MatchFinalStatus, ALERT_SENT_STATUSES
from tennis_model.orchestration.alert_status import AlertStatus
from tennis_model.evaluator.evaluator_decision import EvaluatorStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_result(
    final_status: MatchFinalStatus,
    profile_quality_a: str = "full",
    profile_quality_b: str = "full",
    filter_reason: str = None,
    reason_codes: list = None,
    pick_player: str = "",
    alert_status: AlertStatus = None,
):
    """Build a minimal fake MatchRunResult."""
    ed = MagicMock()
    ed.status = (
        EvaluatorStatus.PICK if final_status in ALERT_SENT_STATUSES
        else EvaluatorStatus.NO_PICK
    )
    ed.reason_code = (reason_codes or ["TEST_CODE"])[0]
    ed.filter_reason = filter_reason

    ad = None
    if alert_status is not None:
        ad = MagicMock()
        ad.status = alert_status
        ad.stake_units = 0.02
        ad.stake_factor = 1.0
        ad.risk_decision = None

    pick = MagicMock()
    pick.pick_player = pick_player

    result = MagicMock()
    result.evaluator_decision = ed
    result.alert_decision = ad
    result.final_status = final_status
    result.profile_quality_a = profile_quality_a
    result.profile_quality_b = profile_quality_b
    result.filter_reason = filter_reason
    result.reason_codes = reason_codes or ["TEST_CODE"]
    result.pick = pick
    return result


# ── A. reason_code_breakdown field ────────────────────────────────────────────

def test_reason_code_breakdown_field_exists():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(DailyAudit)}
    assert "reason_code_breakdown" in fields


def test_reason_code_breakdown_defaults_empty():
    audit = DailyAudit()
    assert audit.reason_code_breakdown == {}


# ── B. record_match_result() populates profile quality ────────────────────────

def test_record_match_result_profile_full():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK)
    audit.record_match_result(result)
    assert audit.profiles_full == 2  # both players are "full"
    assert audit.profiles_degraded == 0


def test_record_match_result_profile_mixed():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        profile_quality_a="full",
        profile_quality_b="degraded",
    )
    audit.record_match_result(result)
    assert audit.profiles_full == 1
    assert audit.profiles_degraded == 1


def test_record_match_result_profile_unknown():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        profile_quality_a="unknown",
        profile_quality_b="unknown",
    )
    audit.record_match_result(result)
    assert audit.profiles_full == 0
    assert audit.profiles_degraded == 0
    assert audit.profiles_failed == 2


# ── C. record_match_result() populates reason_code_breakdown ──────────────────

def test_record_match_result_reason_code_breakdown_single():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK, reason_codes=["EV_FILTER"])
    audit.record_match_result(result)
    assert audit.reason_code_breakdown == {"EV_FILTER": 1}


def test_record_match_result_reason_code_breakdown_accumulates():
    audit = DailyAudit()
    for _ in range(3):
        result = _make_result(MatchFinalStatus.NO_PICK, reason_codes=["EV_FILTER"])
        audit.record_match_result(result)
    assert audit.reason_code_breakdown["EV_FILTER"] == 3


def test_record_match_result_reason_code_breakdown_multiple_codes():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        reason_codes=["EV_FILTER", "VALIDATION_FAILED"],
    )
    audit.record_match_result(result)
    assert audit.reason_code_breakdown["EV_FILTER"] == 1
    assert audit.reason_code_breakdown["VALIDATION_FAILED"] == 1


def test_record_match_result_empty_reason_code_skipped():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK, reason_codes=["", None])
    # Empty/None codes should not be added
    result.reason_codes = [""]
    audit.record_match_result(result)
    assert audit.reason_code_breakdown == {}


# ── D. record_match_result() increments picks_generated ──────────────────────

def test_picks_generated_when_pick_player_set():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.PICK_ALERT_SENT,
        pick_player="A. Player",
        alert_status=AlertStatus.SENT,
    )
    audit.record_match_result(result)
    assert audit.picks_generated == 1


def test_picks_generated_not_incremented_when_no_pick_player():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK, pick_player="")
    audit.record_match_result(result)
    assert audit.picks_generated == 0


def test_picks_generated_accumulates():
    audit = DailyAudit()
    for _ in range(5):
        r = _make_result(
            MatchFinalStatus.PICK_ALERT_SENT,
            pick_player="A. Smith",
            alert_status=AlertStatus.SENT,
        )
        audit.record_match_result(r)
    assert audit.picks_generated == 5


# ── E. record_match_result() increments alerts_eligible/sent/suppressed ───────

def test_alerts_eligible_incremented_for_pick_sent():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.PICK_ALERT_SENT,
        pick_player="A. Smith",
        alert_status=AlertStatus.SENT,
    )
    audit.record_match_result(result)
    assert audit.alerts_eligible == 1
    assert audit.alerts_sent == 1
    assert audit.alerts_suppressed == 0


def test_alerts_eligible_incremented_for_dry_run():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.PICK_DRY_RUN,
        pick_player="B. Jones",
        alert_status=AlertStatus.DRY_RUN,
    )
    audit.record_match_result(result)
    assert audit.alerts_eligible == 1
    assert audit.alerts_sent == 0


def test_alerts_suppressed_incremented_for_fragile():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.PICK_SUPPRESSED,
        pick_player="C. Lee",
        alert_status=AlertStatus.SUPPRESSED,
    )
    audit.record_match_result(result)
    assert audit.alerts_eligible == 1
    assert audit.alerts_suppressed == 1
    assert audit.alerts_sent == 0


def test_no_pick_does_not_increment_alerts():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK)
    audit.record_match_result(result)
    assert audit.alerts_eligible == 0
    assert audit.alerts_sent == 0
    assert audit.alerts_suppressed == 0


# ── F. record_match_result() populates no_pick_reasons ───────────────────────

def test_no_pick_reason_populated_from_filter_reason():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        filter_reason="EV_BELOW_THRESHOLD",
    )
    audit.record_match_result(result)
    assert audit.no_pick_reasons.get("EV_BELOW_THRESHOLD") == 1


def test_no_pick_reason_not_added_when_filter_reason_none():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK, filter_reason=None)
    audit.record_match_result(result)
    assert audit.no_pick_reasons == {}


# ── G. populate_from_scan_results() skips when final_status_breakdown set ─────

def test_populate_skips_when_final_status_breakdown_non_empty():
    audit = DailyAudit()
    # Simulate record_match_result() already called
    audit.final_status_breakdown["PICK_ALERT_SENT"] = 2
    audit.profiles_full = 4   # already populated by record_match_result

    # These should be ignored (except profiles_failed for skipped)
    fake_picks = [MagicMock(), MagicMock()]
    audit.populate_from_scan_results(
        picks=fake_picks,
        alerts=[{"pick": True, "quality_tier": "CLEAN"}],
        blocked=[{"reason": "LOW_EV"}],
        skipped=[{"match": "X vs Y"}],
    )

    # profiles_full should not change (skip logic)
    assert audit.profiles_full == 4
    # profiles_failed should increase for skipped
    assert audit.profiles_failed == 2  # 1 skipped × 2


def test_populate_skips_pick_counting_when_breakdown_set():
    audit = DailyAudit()
    audit.final_status_breakdown["NO_PICK"] = 3
    audit.picks_generated = 0  # already at 0 from record_match_result

    audit.populate_from_scan_results(
        picks=[], alerts=[{"pick": True}], blocked=[], skipped=[],
    )
    # picks_generated should NOT be set by populate (legacy path skipped)
    assert audit.picks_generated == 0


# ── H. populate_from_scan_results() always increments profiles_failed ─────────

def test_populate_always_increments_profiles_failed():
    audit = DailyAudit()
    audit.final_status_breakdown["PICK_ALERT_SENT"] = 1  # non-empty
    audit.populate_from_scan_results(
        picks=[], alerts=[], blocked=[],
        skipped=[{"match": "A vs B"}, {"match": "C vs D"}],
    )
    assert audit.profiles_failed == 4  # 2 skipped × 2


def test_populate_profiles_failed_zero_when_no_skipped():
    audit = DailyAudit()
    audit.final_status_breakdown["PICK_ALERT_SENT"] = 1
    audit.populate_from_scan_results(picks=[], alerts=[], blocked=[], skipped=[])
    assert audit.profiles_failed == 0


# ── I. populate_from_scan_results() legacy logic when breakdown empty ──────────

def test_populate_legacy_logic_when_no_breakdown():
    audit = DailyAudit()
    assert audit.final_status_breakdown == {}  # empty — use legacy path

    fake_pick = MagicMock()
    fake_pick.player_a = MagicMock(profile_quality="full")
    fake_pick.player_b = MagicMock(profile_quality="degraded")

    audit.populate_from_scan_results(
        picks=[fake_pick],
        alerts=[
            {"pick": True, "quality_tier": "CLEAN", "qualified_only": False},
            {"pick": True, "quality_tier": "FRAGILE", "qualified_only": False},
        ],
        blocked=[{"reason": "INSUFFICIENT_DATA"}],
        skipped=[{"match": "X vs Y"}],
    )

    assert audit.profiles_full == 1
    assert audit.profiles_degraded == 1
    assert audit.profiles_failed == 2  # 1 skipped × 2
    assert audit.no_pick_reasons == {"INSUFFICIENT_DATA": 1}
    # 2 alerts total: 1 CLEAN (sent_ok) + 1 FRAGILE (suppressed)
    assert audit.picks_generated == 2
    assert audit.alerts_eligible == 1   # sent_ok only
    assert audit.alerts_sent == 1
    assert audit.alerts_suppressed == 1


# ── J. save_audit_json() includes reason_code_breakdown ──────────────────────

def test_save_audit_json_includes_reason_code_breakdown(tmp_path):
    audit = DailyAudit()
    audit.reason_code_breakdown = {"PICK_APPROVED": 2, "EV_FILTER": 5}
    audit.save_audit_json(audits_dir=str(tmp_path))
    saved_path = tmp_path / f"{audit.date}.json"
    assert saved_path.exists()
    with open(saved_path) as f:
        data = json.load(f)
    assert "reason_code_breakdown" in data
    assert data["reason_code_breakdown"] == {"PICK_APPROVED": 2, "EV_FILTER": 5}


# ── K. Non-regression: P3/P4/P5 alongside P6 ─────────────────────────────────

def test_p6_does_not_break_final_status_breakdown():
    """P5 final_status_breakdown still populated alongside P6 additions."""
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        reason_codes=["EV_FILTER"],
        profile_quality_a="full",
        profile_quality_b="full",
    )
    audit.record_match_result(result)
    assert "NO_PICK" in audit.final_status_breakdown
    assert audit.final_status_breakdown["NO_PICK"] == 1


def test_p6_does_not_break_evaluator_status_breakdown():
    """P4 evaluator_status_breakdown still populated by record_evaluator_decision()."""
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.NO_PICK)
    # ed.status is EvaluatorStatus.NO_PICK (set by _make_result)
    audit.record_match_result(result)
    assert "NO_PICK" in audit.evaluator_status_breakdown


def test_multiple_results_all_counters_consistent():
    """Multiple results build up consistent state across all P3/P4/P5/P6 counters."""
    audit = DailyAudit()

    for _ in range(3):
        r = _make_result(
            MatchFinalStatus.PICK_ALERT_SENT,
            pick_player="A. Smith",
            alert_status=AlertStatus.SENT,
            reason_codes=["PICK_APPROVED"],
            profile_quality_a="full",
            profile_quality_b="degraded",
        )
        audit.record_match_result(r)

    for _ in range(2):
        r = _make_result(
            MatchFinalStatus.NO_PICK,
            reason_codes=["EV_FILTER"],
            profile_quality_a="full",
            profile_quality_b="full",
        )
        audit.record_match_result(r)

    assert audit.picks_generated == 3
    assert audit.alerts_eligible == 3
    assert audit.alerts_sent == 3
    assert audit.profiles_full == 3 * 1 + 2 * 2   # 3 degraded matches + 2 full matches
    assert audit.profiles_degraded == 3
    assert audit.reason_code_breakdown["PICK_APPROVED"] == 3
    assert audit.reason_code_breakdown["EV_FILTER"] == 2
    assert audit.final_status_breakdown["PICK_ALERT_SENT"] == 3
    assert audit.final_status_breakdown["NO_PICK"] == 2
