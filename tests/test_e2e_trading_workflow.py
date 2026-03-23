"""
tests/test_e2e_trading_workflow.py
===================================
End-to-end integration test for the live trading workflow.

Covers the full pick-generation → alerting → settlement → reporting cycle:

  1. Mock odds fetch / slate (no real API calls)
  2. Run pipeline on a match (fetch_player_profile patched)
  3. Generate an alertable pick via maybe_alert
  4. Verify prediction is stored in predictions.json
  5. Simulate settlement via record_result
  6. Verify ELO is updated after settlement
  7. Verify generate_report reads the result correctly

Isolation:
  - All HTTP calls (Tennis Abstract, ATP API, Telegram) are patched out.
  - predictions.json and elo_ratings.json are redirected to a temp dir.
  - Module-level paths and singletons are restored on teardown.

Run:
    PYTHONPATH=<parent-of-tennis_model> python tests/test_e2e_trading_workflow.py
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tennis_model.backtest as _backtest
import tennis_model.elo as _elo
import tennis_model.pipeline as _pipeline
import tennis_model.telegram as _telegram
from tennis_model.models import MatchPick, PlayerProfile


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_profile(short_name, full_name="", ranking=50,
                  hard_wins=80, hard_losses=40,
                  ytd_wins=10, ytd_losses=5,
                  data_source="static_curated"):
    """Realistic PlayerProfile with all model-required fields populated.

    full_name defaults to short_name so that store_prediction's "player_a"
    field matches the "pick" field (both come from short_name when full_name is
    empty).  Using separate full_name / short_name values causes the P&L check
    in record_result to fail (it compares pred["pick"] == pred["winner"] via
    exact string equality).
    """
    return PlayerProfile(
        short_name=short_name,
        full_name=full_name or short_name,
        ranking=ranking,
        hard_wins=hard_wins,
        hard_losses=hard_losses,
        clay_wins=30,
        clay_losses=30,
        grass_wins=15,
        grass_losses=15,
        ytd_wins=ytd_wins,
        ytd_losses=ytd_losses,
        recent_form=["W", "W", "L", "W", "W"],
        data_source=data_source,
        serve_stats={
            "source": "tennis_abstract",
            "career": {
                "first_serve_in":    0.64,
                "first_serve_won":   0.72,
                "second_serve_won":  0.52,
                "serve_win_pct":     0.65,
                "n":                 80,
                "sample_type":       "matchmx_career",
            },
        },
    )


def _make_alertable_pick():
    """
    MatchPick that clears all maybe_alert guards:
      - pick_player is set
      - prob_a / prob_b both valid
      - market_odds present on picked side
      - quality_tier = CLEAN (not FRAGILE)
      - stake_units pre-set (skip Kelly calculation)
    """
    # No separate full_name: store_prediction stores short_name as both
    # "player_a" and "pick", so pred["pick"] == pred["winner"] on settlement.
    pa = _make_profile("J. Alcaraz", ranking=20)
    pb = _make_profile("C. Moreno",  ranking=55, hard_wins=50, hard_losses=55)
    return MatchPick(
        player_a=pa,
        player_b=pb,
        surface="Hard",
        tournament="Test Open",
        tournament_level="ATP 250",
        tour="ATP",
        prob_a=0.62,
        prob_b=0.38,
        fair_odds_a=1.61,
        fair_odds_b=2.63,
        market_odds_a=2.10,   # market underestimates A → positive edge for A
        market_odds_b=1.75,
        edge_a=30.4,          # stored in percentage points (30.4 %)
        edge_b=None,
        pick_player="J. Alcaraz",
        bookmaker="Pinnacle",
        stake_units=0.05,     # pre-set: skips Kelly gate inside maybe_alert
        confidence="HIGH",
        quality_tier="CLEAN",
    )


class _TempWorkflow:
    """
    Context manager that redirects live data files to a temporary directory
    and patches Telegram to a no-op message collector.

    Patches applied:
      backtest.DATA_DIR, backtest.PREDICTIONS_FILE  → tmp dir
      elo.ELO_FILE, elo._elo_engine                 → tmp file + reset singleton
      telegram.send_telegram                         → captures messages, no HTTP

    All patches are reversed on __exit__.
    """

    def __init__(self):
        self.telegram_calls: list[str] = []

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        pred_file = os.path.join(self._tmpdir, "predictions.json")
        elo_file  = os.path.join(self._tmpdir, "elo_ratings.json")

        # Empty predictions store
        with open(pred_file, "w") as f:
            json.dump({"predictions": []}, f)

        # Redirect backtest file paths
        self._orig_data_dir   = _backtest.DATA_DIR
        self._orig_pred_file  = _backtest.PREDICTIONS_FILE
        _backtest.DATA_DIR          = self._tmpdir
        _backtest.PREDICTIONS_FILE  = pred_file

        # Redirect ELO file; reset singleton so it re-initialises from the temp file
        self._orig_elo_file = _elo.ELO_FILE
        self._orig_engine   = _elo._elo_engine
        _elo.ELO_FILE       = elo_file
        _elo._elo_engine    = None

        # Capture Telegram messages without touching the real API
        self._orig_send_telegram = _telegram.send_telegram
        calls = self.telegram_calls

        def _capture(msg: str) -> bool:
            calls.append(msg)
            return True

        _telegram.send_telegram = _capture

        self.pred_file = pred_file
        self.elo_file  = elo_file
        return self

    def __exit__(self, *_):
        _backtest.DATA_DIR          = self._orig_data_dir
        _backtest.PREDICTIONS_FILE  = self._orig_pred_file
        _elo.ELO_FILE               = self._orig_elo_file
        _elo._elo_engine            = self._orig_engine
        _telegram.send_telegram     = self._orig_send_telegram

    def load_predictions(self) -> list:
        with open(self.pred_file, encoding="utf-8") as f:
            return json.load(f)["predictions"]


def _silent_report() -> dict:
    """Call generate_report() with stdout suppressed; return the report dict."""
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        return _backtest.generate_report()
    finally:
        sys.stdout = old


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: full workflow — store → settle (win) → report
# ──────────────────────────────────────────────────────────────────────────────

def test_full_trading_workflow():
    """
    End-to-end happy path:
      maybe_alert fires → prediction stored → result recorded → report correct.
    """
    with _TempWorkflow() as env:
        pick = _make_alertable_pick()

        # ── Step 1: fire alert ─────────────────────────────────────────────
        _telegram.maybe_alert(pick, "")

        # Verify: one prediction persisted
        preds = env.load_predictions()
        assert len(preds) == 1, f"Expected 1 stored prediction, got {len(preds)}"
        pred    = preds[0]
        pred_id = pred["id"]

        assert pred["pick"]       == "J. Alcaraz", f"pick player: {pred['pick']}"
        assert pred["pick_odds"]  == 2.10,          f"pick odds: {pred['pick_odds']}"
        assert pred["confidence"] == "HIGH",         f"confidence: {pred['confidence']}"
        assert pred["surface"]    == "Hard"
        assert pred["tour"]       == "ATP"
        assert pred["result"]     is None,           "result must be null (pending)"
        assert pred["profit_loss"] is None,          "P&L must be null (pending)"
        # edge_a stored as decimal fraction (30.4% → 0.304)
        assert abs(pred["edge_a"] - 0.304) < 0.001, f"edge_a: {pred['edge_a']}"

        # Verify: Telegram alert captured
        assert len(env.telegram_calls) == 1, "Expected exactly one Telegram message"
        assert "Alcaraz" in env.telegram_calls[0], "Alert must mention pick player"

        # ── Step 2: record result (A wins) ─────────────────────────────────
        settled = _backtest.record_result(pred_id, "Alcaraz")

        assert settled["result"]      == "A_WIN",         f"result: {settled['result']}"
        assert "Alcaraz" in settled["winner"],             f"winner: {settled['winner']}"
        assert abs(settled["profit_loss"] - 1.10) < 0.001, \
            f"P&L: {settled['profit_loss']}"

        # Verify persistence
        preds_after = env.load_predictions()
        assert preds_after[0]["result"]      == "A_WIN"
        assert preds_after[0]["profit_loss"] is not None

        # ── Step 3: generate report ─────────────────────────────────────────
        report = _silent_report()

        assert report["total_bets"] == 1,   f"total_bets: {report['total_bets']}"
        assert report["wins"]       == 1,   f"wins: {report['wins']}"
        assert report["losses"]     == 0,   f"losses: {report['losses']}"
        assert report["win_rate"]   == 1.0, f"win_rate: {report['win_rate']}"
        assert report["roi"]        >  0,   f"roi: {report['roi']}"
        assert report["pending"]    == 0,   f"pending: {report['pending']}"

    print("PASS  test_full_trading_workflow")


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: duplicate prediction suppression
# ──────────────────────────────────────────────────────────────────────────────

def test_duplicate_alert_suppressed():
    """
    Firing maybe_alert twice for the same match on the same day must produce
    exactly one stored prediction (dedup by prediction_id).
    """
    with _TempWorkflow() as env:
        pick = _make_alertable_pick()

        _telegram.maybe_alert(pick, "")
        _telegram.maybe_alert(pick, "")   # same match, same day

        preds = env.load_predictions()
        assert len(preds) == 1, (
            f"Duplicate alert must be suppressed — expected 1, got {len(preds)}"
        )

    print("PASS  test_duplicate_alert_suppressed")


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: settlement loss path
# ──────────────────────────────────────────────────────────────────────────────

def test_settlement_loss_records_negative_pl():
    """
    When the opponent wins, result = B_WIN and profit_loss = -1.0.
    The daily report must show 0 wins, negative ROI.
    """
    with _TempWorkflow() as env:
        pick = _make_alertable_pick()
        _telegram.maybe_alert(pick, "")

        pred_id = env.load_predictions()[0]["id"]
        settled = _backtest.record_result(pred_id, "Moreno")   # B wins

        assert settled["result"]      == "B_WIN", f"result: {settled['result']}"
        assert settled["profit_loss"] == -1.0,    f"P&L: {settled['profit_loss']}"

        report = _silent_report()
        assert report["wins"]   == 0
        assert report["losses"] == 1
        assert report["roi"]    <  0

    print("PASS  test_settlement_loss_records_negative_pl")


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: ELO ratings updated after settlement
# ──────────────────────────────────────────────────────────────────────────────

def test_elo_updated_after_settlement():
    """
    record_result calls elo.update() which persists to elo_ratings.json.
    Winner ELO must increase; loser ELO must decrease.
    """
    with _TempWorkflow():
        pick = _make_alertable_pick()
        _telegram.maybe_alert(pick, "")
        pred_id = _backtest._load()["predictions"][0]["id"]

        # Initialise ELO engine (reads from empty temp file)
        engine     = _elo.get_elo_engine()
        # canonical_id must match pred["winner"] = pred["player_a"].
        # store_prediction sets player_a = full_name or short_name.
        # _make_alertable_pick passes no separate full_name, so both equal short_name.
        winner_id  = _elo.canonical_id("J. Alcaraz")   # canonical: "j_alcaraz"
        loser_id   = _elo.canonical_id("C. Moreno")    # canonical: "c_moreno"

        # Capture baseline ELO values (floats, not object refs)
        elo_w_before = engine.get_or_init(winner_id, ranking=20).overall
        elo_l_before = engine.get_or_init(loser_id,  ranking=55).overall

        _backtest.record_result(pred_id, "Alcaraz")

        # Same singleton, updated in-place by elo.update()
        elo_w_after = engine.ratings[winner_id].overall
        elo_l_after = engine.ratings[loser_id].overall

        assert elo_w_after > elo_w_before, (
            f"Winner ELO must increase: {elo_w_before} -> {elo_w_after}"
        )
        assert elo_l_after < elo_l_before, (
            f"Loser ELO must decrease: {elo_l_before} -> {elo_l_after}"
        )

        # Verify persistence to disk
        assert os.path.exists(_elo.ELO_FILE), "elo_ratings.json must be written"
        with open(_elo.ELO_FILE) as f:
            persisted = json.load(f)
        assert winner_id in persisted, "Winner ELO must be in persisted file"

    print("PASS  test_elo_updated_after_settlement")


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: FRAGILE pick suppressed — nothing stored
# ──────────────────────────────────────────────────────────────────────────────

def test_fragile_pick_not_stored():
    """
    maybe_alert must suppress picks with quality_tier == FRAGILE.
    No entry must be written to predictions.json.
    """
    with _TempWorkflow() as env:
        pick = _make_alertable_pick()
        pick.quality_tier = "FRAGILE"   # force suppression

        _telegram.maybe_alert(pick, "")

        preds = env.load_predictions()
        assert len(preds) == 0, (
            f"FRAGILE pick must not be stored — found {len(preds)} entry(ies)"
        )
        assert len(env.telegram_calls) == 0, "No Telegram message for FRAGILE pick"

    print("PASS  test_fragile_pick_not_stored")


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: pipeline run_match returns a structurally valid MatchPick
# ──────────────────────────────────────────────────────────────────────────────

def test_pipeline_run_produces_valid_pick():
    """
    run_match() with mocked profile fetch and no live odds returns a MatchPick
    with valid probabilities and fields.  No real HTTP calls are made.
    """
    pa = _make_profile("T. Alpha", "Test Alpha", ranking=15)
    pb = _make_profile("T. Beta",  "Test Beta",  ranking=40,
                       hard_wins=55, hard_losses=60)

    orig_fetch = _pipeline.fetch_player_profile
    orig_h2h   = _pipeline.fetch_h2h
    orig_odds  = _pipeline.get_live_odds
    orig_eval  = _pipeline.EVALUATOR_AVAILABLE

    def _mock_profile(short_name, tour=""):
        return pa if "Alpha" in short_name else pb

    try:
        _pipeline.fetch_player_profile = _mock_profile
        _pipeline.fetch_h2h            = lambda a, b: (1, 2, "Beta leads H2H 2-1")
        _pipeline.get_live_odds        = lambda *a, **kw: None
        _pipeline.EVALUATOR_AVAILABLE  = False

        # run_match prints the formatted pick card (may include emoji);
        # suppress stdout to avoid cp1252 encoding failures on Windows.
        with _TempWorkflow():
            _orig_stdout, sys.stdout = sys.stdout, io.StringIO()
            try:
                pick = _pipeline.run_match(
                    "T. Alpha vs T. Beta",
                    tournament="Test Open",
                    tournament_lvl="ATP 250",
                    surface="Hard",
                    market_odds_a=2.05,
                    market_odds_b=1.85,
                    tour="atp",
                )
            finally:
                sys.stdout = _orig_stdout

    finally:
        _pipeline.fetch_player_profile = orig_fetch
        _pipeline.fetch_h2h            = orig_h2h
        _pipeline.get_live_odds        = orig_odds
        _pipeline.EVALUATOR_AVAILABLE  = orig_eval

    # ── Structural assertions ──────────────────────────────────────────────
    assert isinstance(pick, MatchPick), f"Expected MatchPick, got {type(pick)}"

    assert 0.0 < pick.prob_a < 1.0, f"prob_a out of range: {pick.prob_a}"
    assert 0.0 < pick.prob_b < 1.0, f"prob_b out of range: {pick.prob_b}"
    assert abs(pick.prob_a + pick.prob_b - 1.0) < 0.01, (
        f"Probs must sum to 1.0: {pick.prob_a + pick.prob_b:.6f}"
    )

    assert pick.fair_odds_a >= 1.0, f"fair_odds_a < 1.0: {pick.fair_odds_a}"
    assert pick.fair_odds_b >= 1.0, f"fair_odds_b < 1.0: {pick.fair_odds_b}"

    assert pick.confidence in ("LOW", "MEDIUM", "HIGH", "VERY HIGH"), (
        f"Invalid confidence value: {pick.confidence!r}"
    )

    assert pick.surface         == "Hard",     f"surface: {pick.surface}"
    assert pick.tour            == "ATP",      f"tour: {pick.tour}"
    assert pick.market_odds_a   == 2.05,       f"market_odds_a: {pick.market_odds_a}"
    assert pick.market_odds_b   == 1.85,       f"market_odds_b: {pick.market_odds_b}"

    print(
        f"PASS  test_pipeline_run_produces_valid_pick  "
        f"[prob_a={pick.prob_a:.3f}  conf={pick.confidence}  "
        f"pick={pick.pick_player or '(none)'}]"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_full_trading_workflow,
        test_duplicate_alert_suppressed,
        test_settlement_loss_records_negative_pl,
        test_elo_updated_after_settlement,
        test_fragile_pick_not_stored,
        test_pipeline_run_produces_valid_pick,
    ]

    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"FAIL  {t.__name__}: {exc}")
            failed.append(t.__name__)
        except Exception as exc:
            import traceback
            print(f"ERROR {t.__name__}: {exc}")
            traceback.print_exc()
            failed.append(t.__name__)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)
