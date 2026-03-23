"""
tests/test_p3_alert_status.py
==============================
P3 tests — AlertStatus enum + AlertDecision dataclass.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision


# ── AlertStatus values ────────────────────────────────────────────────────────

def test_alert_status_is_str_enum():
    assert isinstance(AlertStatus.SENT, str)
    assert AlertStatus.SENT == "SENT"
    assert AlertStatus.FAILED == "FAILED"
    assert AlertStatus.DRY_RUN == "DRY_RUN"
    assert AlertStatus.SKIPPED_UNKNOWN == "SKIPPED_UNKNOWN"
    assert AlertStatus.SKIPPED_NO_PICK == "SKIPPED_NO_PICK"
    assert AlertStatus.SKIPPED_RISK == "SKIPPED_RISK"
    assert AlertStatus.SKIPPED_DEDUPE == "SKIPPED_DEDUPE"
    assert AlertStatus.SKIPPED_KELLY == "SKIPPED_KELLY"
    assert AlertStatus.SUPPRESSED == "SUPPRESSED"
    assert AlertStatus.WATCHLIST == "WATCHLIST"


def test_alert_decision_construction():
    d = AlertDecision(
        status=AlertStatus.SENT,
        reason_code="TELEGRAM_SEND_OK",
        stake_units=0.02,
        stake_factor=1.0,
        telegram_attempted=True,
        telegram_sent=True,
        message_preview="🎾 ATP ...",
    )
    assert d.status == AlertStatus.SENT
    assert d.stake_units == 0.02
    assert d.telegram_sent is True
    assert d.message_preview == "🎾 ATP ..."


def test_alert_decision_optional_preview():
    d = AlertDecision(
        status=AlertStatus.SKIPPED_NO_PICK,
        reason_code="ALERT_SUPPRESSED_NO_PICK",
        stake_units=None,
        stake_factor=0.0,
        telegram_attempted=False,
        telegram_sent=False,
    )
    assert d.message_preview is None
    assert d.stake_units is None


def test_alert_status_all_members():
    expected = {
        "SENT", "FAILED", "WATCHLIST", "SUPPRESSED", "DRY_RUN",
        "SKIPPED_UNKNOWN", "SKIPPED_NO_PICK", "SKIPPED_RISK",
        "SKIPPED_DEDUPE", "SKIPPED_KELLY",
    }
    actual = {s.value for s in AlertStatus}
    assert actual == expected
