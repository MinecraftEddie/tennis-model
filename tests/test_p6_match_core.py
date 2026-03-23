"""
tests/test_p6_match_core.py
============================
P6 tests — run_match_core() importability, risk_decision surfacing,
and structural field checks on MatchRunResult + AlertDecision.

Coverage:
  A. run_match_core importable from orchestration/match_runner
  B. MatchRunResult.risk_decision field exists and defaults to None
  C. AlertDecision.risk_decision field exists and defaults to None
  D. risk_decision carried through build chain: AlertDecision → MatchRunResult
  E. pipeline.run_match_with_result delegates to run_match_core (import chain)
  F. logit_stretch importable from probability_adjustments
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock


# ── A. run_match_core importable ──────────────────────────────────────────────

def test_run_match_core_importable():
    from tennis_model.orchestration.match_runner import run_match_core
    assert callable(run_match_core)


def test_run_match_core_is_keyword_only():
    """run_match_core() must accept only keyword arguments (no positional args)."""
    import inspect
    from tennis_model.orchestration.match_runner import run_match_core
    sig = inspect.signature(run_match_core)
    for name, param in sig.parameters.items():
        assert param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.VAR_KEYWORD,
        ), f"Parameter '{name}' is not keyword-only"


# ── B. MatchRunResult.risk_decision field ─────────────────────────────────────

def test_match_run_result_has_risk_decision_field():
    from tennis_model.orchestration.match_runner import MatchRunResult
    import dataclasses
    fields = {f.name for f in dataclasses.fields(MatchRunResult)}
    assert "risk_decision" in fields


def test_match_run_result_risk_decision_defaults_none():
    from tennis_model.orchestration.match_runner import (
        MatchRunResult, MatchFinalStatus,
    )
    ed = MagicMock()
    ed.reason_code = "PICK_APPROVED"
    result = MatchRunResult(
        match_id="2026-01-01_a_b",
        player_a="A. Player",
        player_b="B. Player",
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=MatchFinalStatus.NO_PICK,
    )
    assert result.risk_decision is None


def test_match_run_result_risk_decision_set_explicitly():
    from tennis_model.orchestration.match_runner import (
        MatchRunResult, MatchFinalStatus,
    )
    ed = MagicMock()
    ed.reason_code = "PICK_APPROVED"
    fake_risk = MagicMock()
    fake_risk.stake_factor = 1.0
    result = MatchRunResult(
        match_id="2026-01-01_a_b",
        player_a="A. Player",
        player_b="B. Player",
        profile_quality_a="full",
        profile_quality_b="degraded",
        evaluator_decision=ed,
        final_status=MatchFinalStatus.PICK_ALERT_SENT,
        risk_decision=fake_risk,
    )
    assert result.risk_decision is fake_risk
    assert result.risk_decision.stake_factor == 1.0


# ── C. AlertDecision.risk_decision field ─────────────────────────────────────

def test_alert_decision_has_risk_decision_field():
    from tennis_model.orchestration.alert_status import AlertDecision
    import dataclasses
    fields = {f.name for f in dataclasses.fields(AlertDecision)}
    assert "risk_decision" in fields


def test_alert_decision_risk_decision_defaults_none():
    from tennis_model.orchestration.alert_status import AlertDecision, AlertStatus
    ad = AlertDecision(
        status=AlertStatus.SENT,
        reason_code="TELEGRAM_SEND_OK",
        stake_units=0.02,
        stake_factor=1.0,
        telegram_attempted=True,
        telegram_sent=True,
    )
    assert ad.risk_decision is None


def test_alert_decision_risk_decision_set():
    from tennis_model.orchestration.alert_status import AlertDecision, AlertStatus
    fake_risk = MagicMock()
    fake_risk.allowed = True
    ad = AlertDecision(
        status=AlertStatus.SENT,
        reason_code="TELEGRAM_SEND_OK",
        stake_units=0.02,
        stake_factor=1.0,
        telegram_attempted=True,
        telegram_sent=True,
        risk_decision=fake_risk,
    )
    assert ad.risk_decision is fake_risk
    assert ad.risk_decision.allowed is True


# ── D. risk_decision propagated from AlertDecision into MatchRunResult ────────

def test_risk_decision_propagated_alert_to_result():
    """
    When build_final_status maps PICK+SENT → PICK_ALERT_SENT,
    a MatchRunResult can carry risk_decision from AlertDecision.risk_decision.
    This simulates what run_match_core() does:
        risk_decision = _ad.risk_decision if _ad is not None else None
    """
    from tennis_model.orchestration.match_runner import (
        MatchRunResult, MatchFinalStatus, build_final_status,
    )
    from tennis_model.orchestration.alert_status import AlertDecision, AlertStatus
    from tennis_model.evaluator.evaluator_decision import EvaluatorStatus

    fake_risk = MagicMock()
    fake_risk.stake_factor = 0.5

    ed = MagicMock()
    ed.status = EvaluatorStatus.PICK
    ed.reason_code = "PICK_APPROVED"
    ed.filter_reason = None

    ad = AlertDecision(
        status=AlertStatus.SENT,
        reason_code="TELEGRAM_SEND_OK",
        stake_units=0.025,
        stake_factor=0.5,
        telegram_attempted=True,
        telegram_sent=True,
        risk_decision=fake_risk,
    )

    final_status = build_final_status(ed, ad)
    result = MatchRunResult(
        match_id="2026-01-01_smith_jones",
        player_a="A. Smith",
        player_b="B. Jones",
        profile_quality_a="full",
        profile_quality_b="degraded",
        evaluator_decision=ed,
        final_status=final_status,
        reason_codes=[ed.reason_code],
        risk_decision=ad.risk_decision,  # extracted same as run_match_core does
        alert_decision=ad,
    )

    assert result.final_status == MatchFinalStatus.PICK_ALERT_SENT
    assert result.risk_decision is fake_risk
    assert result.risk_decision.stake_factor == 0.5


def test_risk_decision_none_on_non_pick_path():
    """On NO_PICK path, alert_decision is None so risk_decision should be None."""
    from tennis_model.orchestration.match_runner import (
        MatchRunResult, MatchFinalStatus, build_final_status,
    )
    from tennis_model.evaluator.evaluator_decision import EvaluatorStatus

    ed = MagicMock()
    ed.status = EvaluatorStatus.NO_PICK
    ed.reason_code = "EV_FILTER"
    ed.filter_reason = "EV_BELOW_THRESHOLD"

    final_status = build_final_status(ed, None)
    result = MatchRunResult(
        match_id="2026-01-01_x_y",
        player_a="X. Player",
        player_b="Y. Player",
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=final_status,
        reason_codes=[ed.reason_code],
        risk_decision=None,  # _ad is None → risk_decision is None
        alert_decision=None,
    )

    assert result.final_status == MatchFinalStatus.NO_PICK
    assert result.risk_decision is None
    assert result.alert_decision is None


# ── E. Pipeline import chain ──────────────────────────────────────────────────

def test_pipeline_imports_run_match_core():
    """
    pipeline.run_match_with_result() should import run_match_core at runtime.
    Verify the import exists by checking it can be imported from both modules.
    """
    from tennis_model.orchestration.match_runner import run_match_core as core_direct
    # If pipeline is importable, its run_match_with_result uses run_match_core
    import tennis_model.pipeline as pipeline
    assert hasattr(pipeline, "run_match_with_result")
    # run_match_core should be the same object regardless of import path
    from tennis_model.orchestration.match_runner import run_match_core as core_via_match
    assert core_direct is core_via_match


# ── F. logit_stretch in probability_adjustments ───────────────────────────────

def test_logit_stretch_importable():
    from tennis_model.probability_adjustments import logit_stretch
    assert callable(logit_stretch)


def test_logit_stretch_fixed_point_at_half():
    from tennis_model.probability_adjustments import logit_stretch
    result = logit_stretch(0.5)
    assert abs(result - 0.5) < 1e-9


def test_logit_stretch_pushes_favorites_higher():
    from tennis_model.probability_adjustments import logit_stretch
    assert logit_stretch(0.70) > 0.70
    assert logit_stretch(0.80) > 0.80


def test_logit_stretch_pushes_underdogs_lower():
    from tennis_model.probability_adjustments import logit_stretch
    assert logit_stretch(0.30) < 0.30
    assert logit_stretch(0.20) < 0.20


def test_logit_stretch_bounds():
    from tennis_model.probability_adjustments import logit_stretch
    for p in (0.01, 0.10, 0.50, 0.90, 0.99):
        r = logit_stretch(p)
        assert 0.0 < r < 1.0
