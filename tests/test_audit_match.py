"""
tests/test_audit_match.py
==========================
Basic non-crash tests for scripts/audit_match.py.

These tests mock the pipeline to avoid network calls and verify
the audit report is generated without errors.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass, field
from typing import Optional


# ── Minimal stubs ─────────────────────────────────────���──────────────────────

@dataclass
class _FakeProfile:
    short_name: str = "Player"
    full_name: str = "Test Player"
    ranking: int = 50
    elo: Optional[float] = None
    data_source: str = "tennis_abstract"
    identity_source: str = "map"
    hard_wins: int = 60
    hard_losses: int = 40
    clay_wins: int = 20
    clay_losses: int = 15
    grass_wins: int = 10
    grass_losses: int = 5
    ytd_wins: Optional[int] = 10
    ytd_losses: Optional[int] = 5
    recent_form: list = field(default_factory=lambda: ["W", "L", "W", "W", "L"])
    serve_stats: dict = field(default_factory=lambda: {"source": "tennis_abstract", "career": {"serve_win_pct": 0.63, "n": 100}})
    profile_quality: str = "full"


@dataclass
class _FakePick:
    player_a: _FakeProfile = field(default_factory=lambda: _FakeProfile(short_name="A. Player", full_name="Alpha Player"))
    player_b: _FakeProfile = field(default_factory=lambda: _FakeProfile(short_name="B. Player", full_name="Beta Player"))
    prob_a: float = 0.60
    prob_b: float = 0.40
    fair_odds_a: float = 1.67
    fair_odds_b: float = 2.50
    market_odds_a: float = 1.80
    market_odds_b: float = 2.10
    edge_a: float = 7.8
    edge_b: float = -5.0
    confidence: str = "MEDIUM"
    validation_passed: bool = True
    validation_warnings: list = field(default_factory=list)
    filter_reason: str = ""
    pick_player: str = "A. Player"
    bookmaker: str = "TestBook"
    odds_source: str = "manual"
    simulation: dict = field(default_factory=lambda: {"win_prob_a": 0.58, "win_prob_b": 0.42, "three_set_prob": 0.45, "tiebreak_prob": 0.30, "volatility": 0.48})
    factor_breakdown: dict = field(default_factory=lambda: {"ranking": (0.6, 0.4), "surface_form": (0.55, 0.45)})
    surface: str = "Hard"
    tournament: str = "Test Open"
    tournament_level: str = "ATP 250"
    tour: str = "ATP"
    evaluator_result: dict = field(default_factory=dict)
    quality_tier: str = "CLEAN"
    h2h_summary: str = "No prior meetings"
    stake_units: Optional[float] = None
    ev_a: Optional[float] = None
    ev_b: Optional[float] = None
    round_name: str = ""
    best_of: int = 3


class _FakeEvalDecision:
    status = MagicMock(value="NO_PICK")
    eval_result = {}
    filter_reason = "test block"
    reason_code = "PICK_NO_EDGE"


class _FakeEVResult:
    def __init__(self, edge=0.0, is_value=False, filter_reason=None):
        self.edge = edge
        self.is_value = is_value
        self.filter_reason = filter_reason


class _FakeValidation:
    passed = True
    warnings = []
    errors = []
    confidence_penalty = 0.0


class _FakeMatchRunResult:
    def __init__(self):
        self.pick = _FakePick()
        self.final_status = MagicMock(value="NO_PICK")
        self.profile_quality_a = "full"
        self.profile_quality_b = "full"
        self.evaluator_decision = _FakeEvalDecision()
        self.filter_reason = ""
        # Audit intermediates (mirror MatchRunResult fields)
        self.ev_a = _FakeEVResult(edge=0.078, is_value=True)
        self.ev_b = _FakeEVResult(edge=-0.05, is_value=False, filter_reason="EDGE -5.0% BELOW THRESHOLD (4% at @2.10 [ATP])")
        self.best_ev_side = "A"
        self.days_inactive = 0
        self.validation = _FakeValidation()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_run_audit_produces_report():
    """run_audit returns a non-empty string report without crashing."""
    from tennis_model.scripts.audit_match import run_audit

    fake_result = _FakeMatchRunResult()

    with patch("tennis_model.orchestration.match_runner.run_match_with_result", return_value=fake_result), \
         patch("tennis_model.evaluator.evaluator.evaluate", return_value={
             "recommended_action": "ignore",
             "confidence": 0.40,
             "reasons": ["test reason"],
             "risk_flags": [],
             "alert_level": "low",
             "short_message": "test",
         }):
        report = run_audit(
            match_str="A. Player vs B. Player",
            market_odds_a=1.80,
            market_odds_b=2.10,
            surface="Hard",
            tournament="Test Open",
            tournament_lvl="ATP 250",
            tour="atp",
        )

    assert isinstance(report, str)
    assert len(report) > 100
    assert "MATCH AUDIT REPORT" in report
    assert "PROFILES" in report
    assert "MODEL" in report
    assert "EV FILTER DETAIL" in report
    assert "COUNTERFACTUAL" in report
    assert "CONCLUSION" in report


def test_run_audit_no_pick_object():
    """run_audit handles None pick gracefully."""
    from tennis_model.scripts.audit_match import run_audit

    fake_result = _FakeMatchRunResult()
    fake_result.pick = None

    with patch("tennis_model.orchestration.match_runner.run_match_with_result", return_value=fake_result):
        report = run_audit(
            match_str="A vs B",
            market_odds_a=1.80,
            market_odds_b=2.10,
        )

    assert "AUDIT FAILED" in report
