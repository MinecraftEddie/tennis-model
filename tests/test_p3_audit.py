"""
tests/test_p3_audit.py
=======================
P3 tests — DailyAudit.record_alert_decision() and enriched JSON export.
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.orchestration.audit import DailyAudit
from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision
from tennis_model.quality.reason_codes import ReasonCode


def _decision(status: AlertStatus, stake_factor: float = 1.0,
              stake_units: float = 0.04) -> AlertDecision:
    return AlertDecision(
        status=status,
        reason_code="TEST",
        stake_units=stake_units if stake_factor > 0 else None,
        stake_factor=stake_factor,
        telegram_attempted=(status in (AlertStatus.SENT, AlertStatus.FAILED)),
        telegram_sent=(status == AlertStatus.SENT),
    )


# ── record_alert_decision increments P3 counters ─────────────────────────────

def test_record_dry_run_increments():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.DRY_RUN, stake_factor=0.0, stake_units=None))
    assert audit.alerts_dry_run == 1
    assert audit.alert_status_breakdown.get("DRY_RUN") == 1


def test_record_skipped_unknown_increments():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SKIPPED_UNKNOWN, stake_factor=0.0, stake_units=None))
    assert audit.alerts_skipped_unknown == 1


def test_record_skipped_risk_increments():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SKIPPED_RISK, stake_factor=0.0, stake_units=None))
    assert audit.alerts_skipped_risk == 1


def test_record_skipped_kelly_increments():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SKIPPED_KELLY, stake_factor=0.0, stake_units=None))
    assert audit.alerts_skipped_kelly == 1


def test_record_stake_reduced_counts():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SENT, stake_factor=0.5, stake_units=0.02))
    assert audit.stake_reduced_count == 1


def test_record_full_stake_not_counted_as_reduced():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SENT, stake_factor=1.0, stake_units=0.04))
    assert audit.stake_reduced_count == 0


def test_record_multiple_decisions_breakdown():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.SENT))
    audit.record_alert_decision(_decision(AlertStatus.DRY_RUN, stake_factor=0.0, stake_units=None))
    audit.record_alert_decision(_decision(AlertStatus.DRY_RUN, stake_factor=0.0, stake_units=None))
    audit.record_alert_decision(_decision(AlertStatus.SKIPPED_UNKNOWN, stake_factor=0.0, stake_units=None))
    assert audit.alert_status_breakdown["SENT"] == 1
    assert audit.alert_status_breakdown["DRY_RUN"] == 2
    assert audit.alert_status_breakdown["SKIPPED_UNKNOWN"] == 1
    assert audit.alerts_dry_run == 2
    assert audit.alerts_skipped_unknown == 1


# ── JSON export includes P3 fields ───────────────────────────────────────────

def test_audit_json_includes_p3_fields():
    audit = DailyAudit()
    audit.record_alert_decision(_decision(AlertStatus.DRY_RUN, stake_factor=0.0, stake_units=None))
    audit.record_alert_decision(_decision(AlertStatus.SENT, stake_factor=0.5, stake_units=0.02))

    with tempfile.TemporaryDirectory() as tmp:
        audit.save_audit_json(audits_dir=tmp)
        path = os.path.join(tmp, f"{audit.date}.json")
        with open(path) as f:
            data = json.load(f)

    assert "alerts_dry_run" in data
    assert "alerts_skipped_unknown" in data
    assert "alerts_skipped_risk" in data
    assert "alerts_skipped_kelly" in data
    assert "stake_reduced_count" in data
    assert "alert_status_breakdown" in data
    assert data["alerts_dry_run"] == 1
    assert data["stake_reduced_count"] == 1
    assert data["alert_status_breakdown"]["DRY_RUN"] == 1
    assert data["alert_status_breakdown"]["SENT"] == 1
