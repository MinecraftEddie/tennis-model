"""
tests/test_result_ingestion.py
===============================
Tests for tracking/result_ingestion.py — automatic result ingestion.

Coverage:
  1. Automatic API winners produce correct outcomes
  2. 5 wins / 3 losses example updates summary correctly
  3. Already settled picks are not duplicated
  4. Missing external data falls back to manual_results
  5. No crash if no winners available
  6. Performance summary updates correctly after settlement
  7. Name matching edge cases
  8. Score parsing edge cases
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock

from tennis_model.tracking.result_ingestion import (
    _last_name,
    _names_match,
    _determine_winner_name,
    fetch_completed_results,
    load_or_fetch_winners,
)
from tennis_model.tracking.pick_store import PickRecord, append_jsonl
from tennis_model.tracking.settlement import (
    OutcomeRecord,
    load_outcome_records,
    save_outcome_record,
    settle_pick_record,
)
from tennis_model.tracking.auto_settlement import (
    load_manual_winners,
    settle_unsettled_picks,
)
from tennis_model.tracking.performance import load_and_summarize, summarize_outcomes


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pick_dict(**overrides) -> dict:
    """Build a minimal pick dict with sensible defaults."""
    defaults = dict(
        date="2026-03-24",
        match_id="2026-03-24_sinner_alcaraz",
        player_a="J. Sinner",
        player_b="C. Alcaraz",
        pick_side="A",
        odds=1.80,
        stake_units=1.0,
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_status="PICK",
        final_status="PICK_ALERT_SENT",
        reason_codes=["PICK_APPROVED"],
        confidence="HIGH",
        ev=0.12,
        is_dry_run=False,
        created_at="2026-03-24T10:00:00Z",
    )
    defaults.update(overrides)
    return defaults


def _write_picks(tmp_path, date_str, pick_dicts):
    """Write pick dicts to a JSONL file."""
    picks_dir = os.path.join(str(tmp_path), "picks")
    os.makedirs(picks_dir, exist_ok=True)
    path = os.path.join(picks_dir, f"{date_str}.jsonl")
    for p in pick_dicts:
        append_jsonl(path, p)
    return picks_dir


def _write_winners(tmp_path, date_str, winners_dict):
    """Write a manual winners JSON file."""
    results_dir = os.path.join(str(tmp_path), "manual_results")
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(winners_dict, f)
    return results_dir


def _make_api_event(home, away, score_home, score_away, completed=True):
    """Build a fake Odds API scores event."""
    return {
        "id": f"fake_{home}_{away}",
        "sport_key": "tennis_atp_miami_open",
        "home_team": home,
        "away_team": away,
        "completed": completed,
        "scores": [
            {"name": home, "score": str(score_home)},
            {"name": away, "score": str(score_away)},
        ],
    }


# ── Name matching ────────────────────────────────────────────────────────────

def test_last_name_simple():
    assert _last_name("J. Sinner") == "sinner"


def test_last_name_full():
    assert _last_name("Jannik Sinner") == "sinner"


def test_last_name_single():
    assert _last_name("Sinner") == "sinner"


def test_names_match_positive():
    assert _names_match("Jannik Sinner", "J. Sinner") is True


def test_names_match_negative():
    assert _names_match("Jannik Sinner", "C. Alcaraz") is False


def test_names_match_case_insensitive():
    assert _names_match("JANNIK SINNER", "j. sinner") is True


# ── Score parsing ────────────────────────────────────────────────────────────

def test_determine_winner_name_normal():
    scores = [{"name": "Sinner", "score": "2"}, {"name": "Alcaraz", "score": "1"}]
    assert _determine_winner_name(scores) == "Sinner"


def test_determine_winner_name_reversed():
    scores = [{"name": "Sinner", "score": "0"}, {"name": "Alcaraz", "score": "2"}]
    assert _determine_winner_name(scores) == "Alcaraz"


def test_determine_winner_name_none_on_empty():
    assert _determine_winner_name(None) is None
    assert _determine_winner_name([]) is None


def test_determine_winner_name_none_on_tie():
    scores = [{"name": "A", "score": "1"}, {"name": "B", "score": "1"}]
    assert _determine_winner_name(scores) is None


def test_determine_winner_name_none_on_bad_score():
    scores = [{"name": "A", "score": "abc"}, {"name": "B", "score": "2"}]
    assert _determine_winner_name(scores) is None


def test_determine_winner_name_single_entry():
    scores = [{"name": "A", "score": "2"}]
    assert _determine_winner_name(scores) is None


# ── 1. Automatic API winners produce correct outcomes ────────────────────────

def test_fetch_completed_results_matches_picks(tmp_path):
    """API events match pick records by player last-name and return correct sides."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(
            match_id="2026-03-24_sinner_alcaraz",
            player_a="J. Sinner",
            player_b="C. Alcaraz",
        ),
    ])

    fake_events = [
        _make_api_event("Jannik Sinner", "Carlos Alcaraz", 2, 1),
    ]

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        winners = fetch_completed_results(date_str)

    assert winners == {"2026-03-24_sinner_alcaraz": "A"}


def test_fetch_completed_results_side_b_wins(tmp_path):
    """When player B wins, the winner side should be 'B'."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(
            match_id="2026-03-24_sinner_alcaraz",
            player_a="J. Sinner",
            player_b="C. Alcaraz",
        ),
    ])

    fake_events = [
        _make_api_event("Jannik Sinner", "Carlos Alcaraz", 0, 2),
    ]

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        winners = fetch_completed_results(date_str)

    assert winners == {"2026-03-24_sinner_alcaraz": "B"}


# ── 2. 5 wins / 3 losses example updates summary correctly ──────────────────

def test_five_wins_three_losses_summary(tmp_path):
    """8 picks, 5 wins, 3 losses → correct settlement + performance summary."""
    date_str = "2026-03-24"
    picks = []
    api_events = []
    match_names = [
        ("sinner", "alcaraz", "J. Sinner", "C. Alcaraz", "Jannik Sinner", "Carlos Alcaraz"),
        ("djokovic", "nadal", "N. Djokovic", "R. Nadal", "Novak Djokovic", "Rafael Nadal"),
        ("medvedev", "zverev", "D. Medvedev", "A. Zverev", "Daniil Medvedev", "Alexander Zverev"),
        ("rublev", "fritz", "A. Rublev", "T. Fritz", "Andrey Rublev", "Taylor Fritz"),
        ("tsitsipas", "ruud", "S. Tsitsipas", "C. Ruud", "Stefanos Tsitsipas", "Casper Ruud"),
        ("hurkacz", "tiafoe", "H. Hurkacz", "F. Tiafoe", "Hubert Hurkacz", "Frances Tiafoe"),
        ("dimitrov", "paul", "G. Dimitrov", "T. Paul", "Grigor Dimitrov", "Tommy Paul"),
        ("shelton", "draper", "B. Shelton", "J. Draper", "Ben Shelton", "Jack Draper"),
    ]
    # 5 wins (pick_side A, winner A) + 3 losses (pick_side A, winner B)
    for i, (ln_a, ln_b, short_a, short_b, full_a, full_b) in enumerate(match_names):
        mid = f"{date_str}_{ln_a}_{ln_b}"
        picks.append(_make_pick_dict(
            match_id=mid, player_a=short_a, player_b=short_b,
            pick_side="A", odds=1.80, stake_units=1.0,
        ))
        winner_is_a = i < 5  # first 5 win, last 3 lose
        score_a = 2 if winner_is_a else 0
        score_b = 0 if winner_is_a else 2
        api_events.append(_make_api_event(full_a, full_b, score_a, score_b))

    picks_dir = _write_picks(tmp_path, date_str, picks)
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=api_events):
        count = settle_unsettled_picks(date_str)

    assert count == 8

    # Verify outcomes
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)

    assert len(outcomes) == 8
    wins = [o for o in outcomes if o["result"] == "win"]
    losses = [o for o in outcomes if o["result"] == "loss"]
    assert len(wins) == 5
    assert len(losses) == 3

    # Verify performance summary
    summary = summarize_outcomes(outcomes)
    assert summary.settled_picks == 8
    assert summary.wins == 5
    assert summary.losses == 3
    assert summary.win_rate == pytest.approx(5 / 8, abs=1e-4)
    # P&L: 5 * 0.80 (win at 1.80) + 3 * (-1.0) = 4.0 - 3.0 = 1.0
    assert summary.total_profit_units == pytest.approx(1.0, abs=1e-4)
    # ROI: 1.0 / 8.0 = 0.125
    assert summary.roi == pytest.approx(0.125, abs=1e-4)


# ── 3. Already settled picks are not duplicated ──────────────────────────────

def test_no_duplicate_settlement_with_auto(tmp_path):
    """Running settlement twice with the same API data should not create duplicates."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz"),
    ])
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")
    fake_events = [_make_api_event("Jannik Sinner", "Carlos Alcaraz", 2, 1)]

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        count1 = settle_unsettled_picks(date_str)
        count2 = settle_unsettled_picks(date_str)

    assert count1 == 1
    assert count2 == 0

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    assert len(outcomes) == 1


# ── 4. Missing external data falls back to manual_results ────────────────────

def test_fallback_to_manual_when_api_unavailable(tmp_path):
    """When the API key is missing, manual results should still work."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz"),
    ])
    results_dir = _write_winners(tmp_path, date_str, {"m1": "A"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value=None):
        count = settle_unsettled_picks(date_str)

    assert count == 1
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    assert outcomes[0]["result"] == "win"


def test_manual_overrides_auto(tmp_path):
    """Manual results override automatic results for the same match_id."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz"),
    ])
    # API says A won, manual says B won → manual should win
    fake_events = [_make_api_event("Jannik Sinner", "Carlos Alcaraz", 2, 1)]
    results_dir = _write_winners(tmp_path, date_str, {"m1": "B"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        count = settle_unsettled_picks(date_str)

    assert count == 1
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    # Manual override → B wins, so pick_side=A → loss
    assert outcomes[0]["winner"] == "B"
    assert outcomes[0]["result"] == "loss"


# ── 5. No crash if no winners available ──────────────────────────────────────

def test_no_crash_no_api_no_manual(tmp_path):
    """No API key, no manual file → 0 settled, no crash."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1"),
    ])
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value=None):
        count = settle_unsettled_picks(date_str)

    assert count == 0


def test_no_crash_api_exception(tmp_path):
    """API throws an exception → graceful fallback, no crash."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1"),
    ])
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    def _exploding_fetch(*args, **kwargs):
        raise RuntimeError("API connection failed")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir), \
         patch("tennis_model.odds_feed._get_api_key", side_effect=_exploding_fetch):
        count = settle_unsettled_picks(date_str)

    assert count == 0


def test_no_crash_empty_picks(tmp_path):
    """No pick records → 0 settled, no crash."""
    date_str = "2026-03-24"
    picks_dir = os.path.join(str(tmp_path), "picks")
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        count = settle_unsettled_picks(date_str)

    assert count == 0


# ── 6. Performance summary updates correctly after settlement ────────────────

def test_performance_summary_after_settlement(tmp_path):
    """Settle 3 picks (2W/1L) and verify the performance summary."""
    date_str = "2026-03-24"
    picks = [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz",
                        pick_side="A", odds=2.00, stake_units=1.0),
        _make_pick_dict(match_id="m2", player_a="N. Djokovic", player_b="R. Nadal",
                        pick_side="A", odds=1.50, stake_units=1.0),
        _make_pick_dict(match_id="m3", player_a="D. Medvedev", player_b="A. Zverev",
                        pick_side="B", odds=2.20, stake_units=1.0),
    ]
    picks_dir = _write_picks(tmp_path, date_str, picks)
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    fake_events = [
        _make_api_event("Jannik Sinner", "Carlos Alcaraz", 2, 0),     # A wins → m1 win
        _make_api_event("Novak Djokovic", "Rafael Nadal", 2, 1),      # A wins → m2 win
        _make_api_event("Daniil Medvedev", "Alexander Zverev", 2, 0), # A wins → m3 loss (picked B)
    ]
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        count = settle_unsettled_picks(date_str)

    assert count == 3

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        summary = load_and_summarize(date_str)

    assert summary.settled_picks == 3
    assert summary.wins == 2
    assert summary.losses == 1
    # P&L: 1.0*(2.00-1) + 1.0*(1.50-1) + (-1.0) = 1.0 + 0.5 - 1.0 = 0.5
    assert summary.total_profit_units == pytest.approx(0.5, abs=1e-4)
    assert summary.roi == pytest.approx(0.5 / 3.0, abs=1e-4)


# ── 7. Unmatched API events are ignored ──────────────────────────────────────

def test_unmatched_api_events_ignored(tmp_path):
    """API events that don't match any pick should be silently skipped."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz"),
    ])

    fake_events = [
        # This event doesn't match the pick
        _make_api_event("Roger Federer", "Andy Murray", 2, 0),
    ]

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        winners = fetch_completed_results(date_str)

    assert winners == {}


# ── 8. Incomplete scores are handled ─────────────────────────────────────────

def test_incomplete_scores_skipped(tmp_path):
    """Events with missing/incomplete scores should be skipped, not crash."""
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", player_a="J. Sinner", player_b="C. Alcaraz"),
    ])

    fake_events = [{
        "id": "fake",
        "sport_key": "tennis_atp_miami_open",
        "home_team": "Jannik Sinner",
        "away_team": "Carlos Alcaraz",
        "completed": True,
        "scores": None,  # scores not yet available
    }]

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.odds_feed._get_api_key", return_value="fake-key"), \
         patch("tennis_model.tracking.result_ingestion._fetch_completed_events", return_value=fake_events):
        winners = fetch_completed_results(date_str)

    assert winners == {}
