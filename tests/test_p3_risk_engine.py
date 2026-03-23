"""
tests/test_p3_risk_engine.py
=============================
P3 tests — compute_risk_decision() pure-function scenarios.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

from tennis_model.evaluator.risk_engine import compute_risk_decision, RiskDecision
from tennis_model.quality.reason_codes import ReasonCode


# ── Rule 1: UNKNOWN → hard block ─────────────────────────────────────────────

def test_unknown_player_a_blocks():
    r = compute_risk_decision("unknown", "full", 0.04)
    assert r.allowed is False
    assert r.stake_units == 0.0
    assert r.stake_factor == 0.0
    assert r.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN


def test_unknown_player_b_blocks():
    r = compute_risk_decision("full", "unknown", 0.04)
    assert r.allowed is False
    assert r.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN


def test_both_unknown_blocks():
    r = compute_risk_decision("unknown", "unknown", 0.04)
    assert r.allowed is False


# ── Rule 2: Kelly None / zero → block ────────────────────────────────────────

def test_kelly_none_blocks():
    r = compute_risk_decision("full", "full", None)
    assert r.allowed is False
    assert r.reason_code == ReasonCode.ALERT_SUPPRESSED_KELLY_ZERO


def test_kelly_zero_blocks():
    r = compute_risk_decision("full", "full", 0.0)
    assert r.allowed is False
    assert r.reason_code == ReasonCode.ALERT_SUPPRESSED_KELLY_ZERO


def test_kelly_negative_blocks():
    r = compute_risk_decision("full", "full", -0.01)
    assert r.allowed is False


# ── Rule 3: DEGRADED → stake * 0.5 ──────────────────────────────────────────

def test_degraded_a_halves_stake():
    r = compute_risk_decision("degraded", "full", 0.04)
    assert r.allowed is True
    assert r.stake_factor == 0.5
    assert r.stake_units == pytest.approx(0.02, abs=1e-6)
    assert r.reason_code == ReasonCode.ALERT_DEGRADED_STAKE_REDUCED


def test_degraded_b_halves_stake():
    r = compute_risk_decision("full", "degraded", 0.04)
    assert r.allowed is True
    assert r.stake_factor == 0.5
    assert r.stake_units == pytest.approx(0.02, abs=1e-6)


def test_both_degraded_halves_stake():
    r = compute_risk_decision("degraded", "degraded", 0.04)
    assert r.allowed is True
    assert r.stake_factor == 0.5


# ── Rule 4: FULL → stake unchanged ───────────────────────────────────────────

def test_full_both_keeps_stake():
    r = compute_risk_decision("full", "full", 0.04)
    assert r.allowed is True
    assert r.stake_factor == 1.0
    assert r.stake_units == pytest.approx(0.04, abs=1e-6)
    assert r.reason_code == ReasonCode.PROFILE_FULL


# ── Rule priority: UNKNOWN beats DEGRADED ────────────────────────────────────

def test_unknown_beats_degraded():
    r = compute_risk_decision("unknown", "degraded", 0.04)
    assert r.allowed is False
    assert r.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN


# ── Rule priority: UNKNOWN beats positive Kelly ───────────────────────────────

def test_unknown_beats_positive_kelly():
    r = compute_risk_decision("unknown", "full", 0.05)
    assert r.allowed is False
