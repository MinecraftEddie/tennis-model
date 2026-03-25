"""
scripts/audit_match.py
=======================
Targeted match audit tool.

Re-runs a single match through the full pipeline, captures every intermediate
value, and produces a structured diagnostic report including a counterfactual
("what if the favorite-odds minimum were disabled?").

Usage (from parent of tennis_model/):

    python tennis_model/scripts/audit_match.py \
        --match "M. Landaluce vs J. Lehecka" \
        --market_odds 3.30 1.41 \
        --surface Hard \
        --tournament "Miami Open" \
        --level "ATP 1000" \
        --tour atp

Also invocable via cli.py:

    python tennis_model/cli.py --audit-match "M. Landaluce vs J. Lehecka" \
        --market_odds 3.30 1.41 --surface Hard --tour atp

No model changes.  No threshold changes.  Audit only.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import textwrap

# Ensure project root is on sys.path when run as a script
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if os.path.dirname(_ROOT) not in sys.path:
    sys.path.insert(0, os.path.dirname(_ROOT))

log = logging.getLogger(__name__)


# ── All-gates checker (audit only — no decision changes) ─────────────────────

def _check_all_gates(
    market_odds: float,
    fair_odds: float,
    validation,
    confidence: str,
    days_inactive: int,
    tour: str,
) -> list[str]:
    """Return names of ALL gates that would fail, not just the first.

    Gate names mirror the filter_reason prefixes in ev.compute_ev.
    Order matches compute_ev exactly.  No thresholds are changed.
    """
    from tennis_model.config.runtime_config import (
        PROB_FLOOR, SUSPICIOUS_EDGE_THRESHOLD, MIN_ODDS, MAX_ODDS,
    )
    from tennis_model.ev import _min_edge_for_odds

    failed: list[str] = []

    # 1. Invalid odds
    if not market_odds or not fair_odds or market_odds <= 1.0 or fair_odds <= 1.0:
        failed.append("INVALID_ODDS")
        return failed  # can't compute edge — remaining gates are meaningless

    edge = round((market_odds / fair_odds) - 1, 4)

    # 2. Suspicious edge
    if edge > SUSPICIOUS_EDGE_THRESHOLD:
        failed.append("SUSPICIOUS_EDGE")

    # 3. Validation
    if not validation.passed:
        failed.append("VALIDATION_FAILED")

    # 4. Inactivity
    if days_inactive != -1 and days_inactive > 60:
        failed.append("INACTIVE")

    # 5. Probability floor
    model_prob = 1.0 / fair_odds
    if model_prob < PROB_FLOOR:
        failed.append("PROB_FLOOR")

    # 6. Min odds
    if market_odds < MIN_ODDS:
        failed.append("MIN_ODDS")

    # 7. Low confidence
    if confidence == "LOW":
        failed.append("LOW_CONFIDENCE")

    # 8. Edge threshold
    _min = _min_edge_for_odds(market_odds, tour)
    if edge < _min:
        failed.append("EDGE_THRESHOLD")

    return failed


# ── Verbose gate trace (audit only — no decision changes) ────────────────────

def _format_gate_trace(
    market_odds: float,
    fair_odds: float,
    validation,
    confidence: str,
    days_inactive: int,
    tour: str,
) -> list[str]:
    """Format detailed gate-by-gate trace for the verbose audit.

    Same gates, same order, same thresholds as ev.compute_ev.
    Shows OK/FAIL + the actual values and thresholds used for each gate.
    """
    from tennis_model.config.runtime_config import (
        PROB_FLOOR, SUSPICIOUS_EDGE_THRESHOLD, MIN_ODDS, MAX_ODDS,
    )
    from tennis_model.ev import _min_edge_for_odds

    out: list[str] = []

    # Gate 1: INVALID_ODDS
    invalid = (not market_odds or not fair_odds
               or market_odds <= 1.0 or fair_odds <= 1.0)
    out.append(f"  GATE 1: INVALID_ODDS          -> {'FAIL' if invalid else 'OK'}")
    if invalid:
        out.append(f"    market_odds = {market_odds}")
        out.append(f"    fair_odds   = {fair_odds}")
        return out  # can't compute edge — remaining gates are meaningless

    edge = round((market_odds / fair_odds) - 1, 4)

    # Gate 2: SUSPICIOUS_EDGE
    suspicious = edge > SUSPICIOUS_EDGE_THRESHOLD
    out.append(f"  GATE 2: SUSPICIOUS_EDGE       -> {'FAIL' if suspicious else 'OK'}")
    if suspicious:
        out.append(f"    edge      = {edge:.4f}")
        out.append(f"    threshold = {SUSPICIOUS_EDGE_THRESHOLD}")

    # Gate 3: VALIDATION_FAILED
    val_fail = not validation.passed
    out.append(f"  GATE 3: VALIDATION_FAILED     -> {'FAIL' if val_fail else 'OK'}")
    if val_fail:
        out.append(f"    errors = {validation.errors}")

    # Gate 4: INACTIVE
    inactive = days_inactive != -1 and days_inactive > 60
    out.append(f"  GATE 4: INACTIVE              -> {'FAIL' if inactive else 'OK'}")
    if inactive:
        out.append(f"    days_inactive = {days_inactive}")
        out.append(f"    max_allowed   = 60")

    # Gate 5: PROB_FLOOR (always show values — key debugging target)
    model_prob = 1.0 / fair_odds
    prob_fail = model_prob < PROB_FLOOR
    out.append(f"  GATE 5: PROB_FLOOR            -> {'FAIL' if prob_fail else 'OK'}")
    out.append(f"    model_prob = {model_prob:.3f}")
    out.append(f"    floor      = {PROB_FLOOR}")

    # Gate 6: MIN_ODDS (always show values — common debugging target)
    min_fail = market_odds < MIN_ODDS
    out.append(f"  GATE 6: MIN_ODDS              -> {'FAIL' if min_fail else 'OK'}")
    out.append(f"    market_odds = {market_odds:.2f}")
    out.append(f"    min_odds    = {MIN_ODDS}")

    # Gate 7: LOW_CONFIDENCE
    low_conf = confidence == "LOW"
    out.append(f"  GATE 7: LOW_CONFIDENCE        -> {'FAIL' if low_conf else 'OK'}")
    if low_conf:
        out.append(f"    confidence = {confidence}")

    # Gate 8: EDGE_THRESHOLD (always show values)
    _min = _min_edge_for_odds(market_odds, tour)
    edge_fail = edge < _min
    out.append(f"  GATE 8: EDGE_THRESHOLD        -> {'FAIL' if edge_fail else 'OK'}")
    out.append(f"    edge     = {edge:.4f}")
    out.append(f"    required = {_min}")

    return out


# ── Report formatter ─────────────────────────────────────────────────────────

def _sec(title: str) -> str:
    return f"\n{'═' * 70}\n  {title}\n{'═' * 70}"


def _row(label: str, value, width: int = 34) -> str:
    return f"  {label:<{width}} {value}"


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:.1%}" if isinstance(v, float) and abs(v) < 10 else f"{v}"


def _odds(v) -> str:
    return f"@{v:.2f}" if v else "—"


# ── Core audit logic ─────────────────────────────────────────────────────────

def run_audit(
    match_str: str,
    market_odds_a: float,
    market_odds_b: float,
    surface: str = "Hard",
    tournament: str = "ATP Tour",
    tournament_lvl: str = "ATP 250",
    tour: str = "atp",
    bookmaker: str = "",
    verbose: bool = False,
) -> str:
    """Run the full pipeline, capture all data, run counterfactual, return report."""

    from tennis_model.orchestration.match_runner import run_match_with_result
    from tennis_model.ev import compute_ev, _min_edge_for_odds
    from tennis_model.config.runtime_config import MIN_ODDS, PROB_FLOOR
    from tennis_model.evaluator.evaluator import evaluate as evaluator_evaluate
    from tennis_model.evaluator.evaluator_decision import build_evaluator_decision

    # ── 1. Run the real pipeline silently ────────────────────────────────────
    result = run_match_with_result(
        match_str,
        tournament=tournament,
        tournament_lvl=tournament_lvl,
        surface=surface,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
        bookmaker=bookmaker,
        tour=tour,
        _silent=True,
    )

    pick = result.pick
    if pick is None:
        return "AUDIT FAILED: pipeline returned no pick object (FAILED status)."

    pa = pick.player_a
    pb = pick.player_b

    # ── 2. Extract intermediate values from the pipeline result ────────────
    # The pipeline may have fetched live odds, overriding CLI values.
    # Use the actual values that the pipeline used (stored in the pick).
    actual_odds_a = pick.market_odds_a or market_odds_a
    actual_odds_b = pick.market_odds_b or market_odds_b
    odds_source = getattr(pick, "odds_source", "manual")
    odds_changed = (actual_odds_a != market_odds_a or actual_odds_b != market_odds_b)

    prob_a = pick.prob_a
    prob_b = pick.prob_b
    fo_a = pick.fair_odds_a
    fo_b = pick.fair_odds_b
    ea = pick.edge_a          # percentage, e.g. 7.1
    eb = pick.edge_b
    confidence = pick.confidence
    mc = pick.simulation or {}
    comps = pick.factor_breakdown or {}

    # Edge as decimal
    ea_dec = ea / 100.0 if ea is not None else None
    eb_dec = eb / 100.0 if eb is not None else None

    # ── 3. Read pipeline EV results (same objects the scanner used) ────────
    ev_a_result = result.ev_a
    ev_b_result = result.ev_b
    best_side = result.best_ev_side   # "A" or "B" — production logic
    days_inactive = result.days_inactive or 0
    validation = result.validation    # real ValidationResult from pipeline

    # ── 3b. All-gates audit (both sides) ────────────────────────────────────
    failed_gates_a = _check_all_gates(
        actual_odds_a, fo_a, validation, confidence, days_inactive, tour,
    )
    failed_gates_b = _check_all_gates(
        actual_odds_b, fo_b, validation, confidence, days_inactive, tour,
    )

    # ── 4. Counterfactual: disable MIN_ODDS only ────────────────────────────
    # Re-runs compute_ev with patched MIN_ODDS — inherently a what-if.
    # Uses the REAL validation + days_inactive captured from the pipeline.
    import tennis_model.config.runtime_config as _rc
    import tennis_model.ev as _ev_mod
    _saved_rc = _rc.MIN_ODDS
    _saved_ev = _ev_mod.MIN_ODDS
    try:
        _rc.MIN_ODDS = 1.0   # patch the config source
        _ev_mod.MIN_ODDS = 1.0  # patch the local binding in ev.py
        cf_ev_a = compute_ev(actual_odds_a, fo_a, validation, confidence, days_inactive, tour=tour)
        cf_ev_b = compute_ev(actual_odds_b, fo_b, validation, confidence, days_inactive, tour=tour)
        cf_best = cf_ev_a if cf_ev_a.edge > cf_ev_b.edge else cf_ev_b
        cf_best_side = "A" if cf_ev_a.edge > cf_ev_b.edge else "B"

        # If counterfactual passes EV, run evaluator
        cf_eval_result = {}
        cf_eval_decision = None
        cf_recommended = "—"
        if cf_best.is_value and pick is not None:
            try:
                cf_eval_result = evaluator_evaluate(pick, {"is_live": False})
                cf_eval_decision = build_evaluator_decision(cf_best, cf_eval_result, validation.passed)
                cf_recommended = cf_eval_decision.status.value
            except Exception as exc:
                cf_recommended = f"evaluator error: {exc}"
        elif cf_best.is_value:
            cf_recommended = "PICK (evaluator unavailable)"
        else:
            cf_recommended = f"STILL BLOCKED: {cf_best.filter_reason}"
    finally:
        _rc.MIN_ODDS = _saved_rc
        _ev_mod.MIN_ODDS = _saved_ev

    # ── 5. Risk flags (from pipeline evaluator — no re-run) ──────────────
    eval_result_real = getattr(pick, "evaluator_result", None) or {}

    risk_flags = eval_result_real.get("risk_flags", [])
    eval_reasons = eval_result_real.get("reasons", [])
    eval_action = eval_result_real.get("recommended_action", "—")
    eval_confidence = eval_result_real.get("confidence", None)

    # ── 6. Build the report ─────────────────────────────────────────────────
    lines = []
    w = lines.append

    w(_sec("MATCH AUDIT REPORT"))
    w(_row("Match", f"{pa.short_name} vs {pb.short_name}"))
    w(_row("Tournament", f"{tournament} ({tournament_lvl})"))
    w(_row("Surface", surface))
    w(_row("Tour", tour.upper()))
    w(_row("Odds A (CLI input)", _odds(market_odds_a)))
    w(_row("Odds B (CLI input)", _odds(market_odds_b)))
    if odds_changed:
        w(_row("Odds A (live, used)", _odds(actual_odds_a)))
        w(_row("Odds B (live, used)", _odds(actual_odds_b)))
        w(_row("Odds source", f"LIVE (overrode CLI values)"))
    else:
        w(_row("Odds source", odds_source))
    w(_row("Bookmaker", pick.bookmaker or bookmaker or "—"))
    w(_row("Final status", result.final_status.value))
    w(_row("Filter reason", pick.filter_reason or "—"))

    # ── PROFILES ────────────────────────────────────────────────────────────
    w(_sec("PROFILES"))
    for label, p, qual in [
        ("Player A", pa, result.profile_quality_a),
        ("Player B", pb, result.profile_quality_b),
    ]:
        w(f"\n  --- {label}: {p.short_name} ---")
        w(_row("  Profile quality", qual))
        w(_row("  Data source", p.data_source or "—"))
        w(_row("  Identity source", getattr(p, "identity_source", "—")))
        w(_row("  Ranking", p.ranking or "—"))
        w(_row("  ELO", f"{p.elo:.0f}" if p.elo else "—"))
        surf_l = surface.lower()
        sw = getattr(p, f"{surf_l}_wins", 0)
        sl = getattr(p, f"{surf_l}_losses", 0)
        w(_row(f"  {surface} record", f"{sw}W-{sl}L ({sw+sl} matches)"))
        w(_row("  YTD", f"{p.ytd_wins or 0}W-{p.ytd_losses or 0}L"))
        w(_row("  Recent form", "".join(p.recent_form[-10:]) if p.recent_form else "—"))
        ss = p.serve_stats or {}
        w(_row("  Serve stats source", ss.get("source", "—")))
        if ss.get("source") in ("tennis_abstract", "tennis_abstract_wta"):
            from tennis_model.evaluator.serve_utils import _get_serve_metric
            w(_row("  Serve win %", _pct(_get_serve_metric(ss, "serve_win_pct"))))
            w(_row("  1st serve in %", _pct(_get_serve_metric(ss, "first_in_pct"))))
            w(_row("  Hold %", _pct(_get_serve_metric(ss, "hold_pct"))))
            w(_row("  Break saved %", _pct(_get_serve_metric(ss, "break_saved_pct"))))
            w(_row("  Break %", _pct(_get_serve_metric(ss, "break_pct"))))
            n = ss.get("career", {}).get("n", ss.get("n", "—"))
            w(_row("  Sample size (n)", n))
        else:
            w(_row("  Serve stats", "proxy/unavailable"))

    # ── MODEL ───────────────────────────────────────────────────────────────
    w(_sec("MODEL"))
    w(_row("Analytical prob A", _pct(prob_a)))
    w(_row("Analytical prob B", _pct(prob_b)))
    if mc:
        w(_row("MC sim prob A", _pct(mc.get("win_prob_a"))))
        w(_row("MC sim prob B", _pct(mc.get("win_prob_b"))))
        w(_row("MC 3-set prob", _pct(mc.get("three_set_prob"))))
        w(_row("MC tiebreak prob", _pct(mc.get("tiebreak_prob"))))
        w(_row("MC volatility", _pct(mc.get("volatility"))))

    w(f"\n  {'─' * 50}")
    w(_row("Fair odds A", _odds(fo_a)))
    w(_row("Fair odds B", _odds(fo_b)))
    w(_row("Market odds A", _odds(market_odds_a)))
    w(_row("Market odds B", _odds(market_odds_b)))
    w(_row("Edge A", f"{ea:+.1f}%" if ea is not None else "—"))
    w(_row("Edge B", f"{eb:+.1f}%" if eb is not None else "—"))
    w(_row("Best edge side", f"{best_side} ({pa.short_name if best_side == 'A' else pb.short_name})"))

    w(f"\n  {'─' * 50}")
    w(_row("Confidence", confidence))
    w(_row("Validation passed", str(pick.validation_passed)))
    if pick.validation_warnings:
        for vw in pick.validation_warnings:
            w(_row("  Warning", vw))

    # Factor breakdown
    if comps:
        w(f"\n  Factor breakdown:")
        for k, v in comps.items():
            if k == "monte_carlo":
                continue
            if isinstance(v, tuple) and len(v) == 2:
                w(f"    {k:<25} A={v[0]:.3f}  B={v[1]:.3f}")

    # ── PROBABILITY DEBUG TRACE ──────────────────────────────────────────
    w(_sec("PROBABILITY DEBUG TRACE"))
    w(f"  Shows every transformation from raw model → PROB_FLOOR input.")
    w(f"  If raw_model_prob ≠ final_prob_used_for_filter, the gap is from")
    w(f"  shrink_toward_market + logit_stretch + fair_odds rounding.\n")

    from tennis_model.probability_adjustments import shrink_toward_market, logit_stretch, SHRINK_ALPHA
    from tennis_model.model import fair_odds as _fair_odds_fn

    for _side, _prob_raw, _mo, _fo, _mc_key in [
        ("A", prob_a, actual_odds_a, fo_a, "win_prob_a"),
        ("B", prob_b, actual_odds_b, fo_b, "win_prob_b"),
    ]:
        _name = pa.short_name if _side == "A" else pb.short_name
        w(f"  --- Side {_side} ({_name}) ---")

        # Step 1: raw model probability
        w(_row("  raw_model_prob", _pct(_prob_raw)))

        # Step 2: market-implied probability
        _market_implied = 1.0 / _mo if _mo and _mo > 1.0 else None
        w(_row("  market_implied_prob", _pct(_market_implied)))

        # Step 3: after shrink toward market
        _shrunk = shrink_toward_market(_prob_raw, _mo) if _mo else _prob_raw
        w(_row("  after_shrink", f"{_pct(_shrunk)}  (α={SHRINK_ALPHA})"))

        # Step 4: after logit stretch (before renorm)
        _stretched = logit_stretch(_shrunk)
        w(_row("  after_logit_stretch", _pct(_stretched)))

        # Step 5: after renormalization (= adjusted_model_prob)
        # Recompute both sides to get the normalizing denominator
        _shrunk_a = shrink_toward_market(prob_a, actual_odds_a) if actual_odds_a else prob_a
        _shrunk_b = shrink_toward_market(prob_b, actual_odds_b) if actual_odds_b else prob_b
        _sa = logit_stretch(_shrunk_a)
        _sb = logit_stretch(_shrunk_b)
        _adjusted = _sa / (_sa + _sb) if _side == "A" else _sb / (_sa + _sb)
        w(_row("  adjusted_model_prob", f"{_pct(_adjusted)}  (after renorm)"))

        # Step 6: fair_odds rounding effect
        _fo_computed = _fair_odds_fn(_adjusted)
        _prob_from_fo = 1.0 / _fo_computed if _fo_computed > 1.0 else 0.0
        w(_row("  fair_odds", f"{_odds(_fo_computed)}  → 1/fo = {_pct(_prob_from_fo)}"))

        # Step 7: what compute_ev actually uses for PROB_FLOOR
        # (1.0 / fair_odds as passed to compute_ev — i.e. pick.fair_odds_X)
        _prob_for_filter = 1.0 / _fo if _fo > 1.0 else 0.0
        w(_row("  final_prob_used_for_filter", _pct(_prob_for_filter)))

        # Step 8: MC simulation prob (if available)
        _mc_prob = mc.get(_mc_key) if mc else None
        w(_row("  live_model_prob (MC sim)", _pct(_mc_prob) if _mc_prob is not None else "—"))

        # Delta: highlight the gap
        _delta = _prob_for_filter - _prob_raw if _prob_raw else None
        if _delta is not None:
            w(_row("  Δ (filter − raw)", f"{_delta:+.1%}"))
        w("")

    # ── DECISION TRACE ─────────────────────────────────────────────────────
    w(_sec("DECISION TRACE"))

    sel_side = best_side
    sel_name = pa.short_name if sel_side == "A" else pb.short_name
    sel_prob = prob_a if sel_side == "A" else prob_b
    sel_fo = fo_a if sel_side == "A" else fo_b
    sel_mo = actual_odds_a if sel_side == "A" else actual_odds_b
    sel_edge = ea if sel_side == "A" else eb
    sel_ev = ev_a_result if sel_side == "A" else ev_b_result
    other_ev = ev_b_result if sel_side == "A" else ev_a_result

    w(_row("selected_side", f"{sel_side} ({sel_name})"))
    w(_row("model_prob", f"{sel_prob:.3f}" if sel_prob is not None else "—"))
    w(_row("fair_odds", _odds(sel_fo)))
    w(_row("market_odds", _odds(sel_mo)))
    w(_row("edge", f"{sel_edge:+.1f}%" if sel_edge is not None else "—"))
    w(_row("filter_reason (selected)", sel_ev.filter_reason or "PASS"))
    w(_row("filter_reason (other)",    other_ev.filter_reason or "PASS"))
    sel_gates = failed_gates_a if sel_side == "A" else failed_gates_b
    w(_row("first_failed_gate", sel_gates[0] if sel_gates else "NONE"))
    w(_row("all_failed_gates", f"[{', '.join(sel_gates)}]" if sel_gates else "[]"))
    w(_row("days_inactive", str(days_inactive)))

    # ── EV FILTER DETAIL (BOTH SIDES) ──────────────────────────────────────
    w(_sec("EV FILTER DETAIL"))
    w(f"\n  Side A ({pa.short_name} {_odds(actual_odds_a)}):")
    w(_row("  Edge", f"{ev_a_result.edge*100:+.1f}%"))
    w(_row("  is_value", str(ev_a_result.is_value)))
    w(_row("  filter_reason", ev_a_result.filter_reason or "PASS"))
    w(_row("  first_failed_gate", failed_gates_a[0] if failed_gates_a else "NONE"))
    w(_row("  all_failed_gates", f"[{', '.join(failed_gates_a)}]" if failed_gates_a else "[]"))
    _min_a = _min_edge_for_odds(actual_odds_a, tour)
    w(_row("  min_edge_required", f"{_min_a*100:.0f}%"))
    _model_prob_a = 1.0 / fo_a if fo_a > 1.0 else 0.0
    w(_row("  model_prob", _pct(_model_prob_a)))
    w(_row("  prob_floor", _pct(PROB_FLOOR)))

    w(f"\n  Side B ({pb.short_name} {_odds(actual_odds_b)}):")
    w(_row("  Edge", f"{ev_b_result.edge*100:+.1f}%"))
    w(_row("  is_value", str(ev_b_result.is_value)))
    w(_row("  filter_reason", ev_b_result.filter_reason or "PASS"))
    w(_row("  first_failed_gate", failed_gates_b[0] if failed_gates_b else "NONE"))
    w(_row("  all_failed_gates", f"[{', '.join(failed_gates_b)}]" if failed_gates_b else "[]"))
    _min_b = _min_edge_for_odds(actual_odds_b, tour)
    w(_row("  min_edge_required", f"{_min_b*100:.0f}%"))
    _model_prob_b = 1.0 / fo_b if fo_b > 1.0 else 0.0
    w(_row("  model_prob", _pct(_model_prob_b)))
    w(_row("  prob_floor", _pct(PROB_FLOOR)))

    # ── RISK / EVALUATOR ────────────────────────────────────────────────────
    w(_sec("RISK / EVALUATOR"))
    w(_row("Evaluator action (real)", eval_action))
    w(_row("Evaluator confidence", f"{eval_confidence:.2f}" if eval_confidence is not None else "—"))
    if eval_reasons:
        w(f"  Evaluator reasons:")
        for r in eval_reasons:
            w(f"    • {r}")
    if risk_flags:
        w(f"  Risk flags:")
        for f_ in risk_flags:
            w(f"    ⚠ {f_}")
    else:
        w(_row("Risk flags", "none"))

    # ── COUNTERFACTUAL ──────────────────────────────────────────────────────
    w(_sec("COUNTERFACTUAL: disable favorite odds < 1.5 only"))

    w(f"\n  Side A without MIN_ODDS gate:")
    w(_row("  Edge", f"{cf_ev_a.edge*100:+.1f}%"))
    w(_row("  is_value", str(cf_ev_a.is_value)))
    w(_row("  filter_reason", cf_ev_a.filter_reason or "PASS"))

    w(f"\n  Side B without MIN_ODDS gate:")
    w(_row("  Edge", f"{cf_ev_b.edge*100:+.1f}%"))
    w(_row("  is_value", str(cf_ev_b.is_value)))
    w(_row("  filter_reason", cf_ev_b.filter_reason or "PASS"))

    w(f"\n  Best side (counterfactual):  {cf_best_side} ({pa.short_name if cf_best_side == 'A' else pb.short_name})")
    w(_row("  Counterfactual outcome", cf_recommended))

    if cf_eval_result:
        cf_action = cf_eval_result.get("recommended_action", "—")
        cf_flags = cf_eval_result.get("risk_flags", [])
        cf_reasons = cf_eval_result.get("reasons", [])
        w(_row("  Evaluator action (CF)", cf_action))
        if cf_reasons:
            for r in cf_reasons:
                w(f"    • {r}")
        if cf_flags:
            for f_ in cf_flags:
                w(f"    ⚠ {f_}")

    # ── CONCLUSION ──────────────────────────────────────────────────────────
    w(_sec("CONCLUSION"))

    # Determine the underdog side
    if actual_odds_a > actual_odds_b:
        ud_name = pa.short_name
        ud_odds = actual_odds_a
        ud_edge = ea
        ud_ev = ev_a_result
        ud_cf_ev = cf_ev_a
    else:
        ud_name = pb.short_name
        ud_odds = actual_odds_b
        ud_edge = eb
        ud_ev = ev_b_result
        ud_cf_ev = cf_ev_b

    w(f"  Underdog: {ud_name} {_odds(ud_odds)}")
    w(f"  Edge on underdog: {ud_edge:+.1f}%" if ud_edge is not None else "  Edge on underdog: —")

    if ud_ev.is_value:
        w(f"  → Underdog PASSES EV filter with current rules")
    else:
        w(f"  → Underdog BLOCKED by: {ud_ev.filter_reason}")

    if ud_cf_ev.is_value:
        w(f"  → Without MIN_ODDS gate, underdog PASSES EV filter")
    else:
        w(f"  → Without MIN_ODDS gate, underdog STILL BLOCKED: {ud_cf_ev.filter_reason}")

    # Is this a real value candidate masked by the favorite block?
    blocked_by_fav_min = (
        pick.filter_reason
        and "BELOW MINIMUM" in pick.filter_reason
        and best_side != ("A" if actual_odds_a > actual_odds_b else "B")
    )
    if blocked_by_fav_min:
        w(f"\n  ⚡ The final filter_reason ({pick.filter_reason}) is from the FAVORITE side.")
        w(f"     This means best_ev was selected from the favorite, not the underdog.")
        w(f"     The underdog was NOT separately considered because the favorite had higher edge.")
        if ud_edge is not None and ud_edge > 0:
            w(f"     However, the underdog has positive edge ({ud_edge:+.1f}%).")
            w(f"     Review the underdog EV detail above for the full picture.")
        else:
            w(f"     The underdog has {'negative' if ud_edge and ud_edge < 0 else 'zero'} edge — not a value candidate.")
    else:
        if ud_edge is not None and ud_edge > 0 and not ud_ev.is_value:
            w(f"\n  ⚡ Underdog has positive edge but is blocked by: {ud_ev.filter_reason}")
        elif ud_edge is not None and ud_edge <= 0:
            w(f"\n  ✗ Model sees no value on the underdog ({ud_edge:+.1f}% edge).")

    # ── VERBOSE PIPELINE TRACE ────────────────────────────────────────────
    if verbose:
        from tennis_model.probability_adjustments import shrink_toward_market as _shrink, logit_stretch as _stretch

        w(f"\n{'═' * 70}")
        w(f"  VERBOSE PIPELINE TRACE")
        w(f"{'═' * 70}")

        # ── V1. INPUTS ────────────────────────────────────────────────────
        w(f"\n  ── 1. INPUTS ──")
        w(_row("  match", f"{pa.short_name} vs {pb.short_name}"))
        w(_row("  player_a", pa.short_name))
        w(_row("  player_b", pb.short_name))
        w(_row("  market_odds_a", _odds(actual_odds_a)))
        w(_row("  market_odds_b", _odds(actual_odds_b)))
        w(_row("  surface", surface))
        w(_row("  tour", tour))
        w(_row("  tournament", f"{tournament} ({tournament_lvl})"))
        w(_row("  bookmaker", pick.bookmaker or bookmaker or "—"))
        w(_row("  odds_source", odds_source))
        w(_row("  confidence", confidence))
        w(_row("  days_inactive", str(days_inactive)))
        w(_row("  validation_passed", str(validation.passed)))
        if validation.errors:
            w(_row("  validation_errors", str(validation.errors)))

        # ── V2. MODEL OUTPUTS ─────────────────────────────────────────────
        w(f"\n  ── 2. MODEL OUTPUTS ──")

        # Recompute adjustment chain (same math as run_match_core lines 302-307)
        _shrunk_a = _shrink(prob_a, actual_odds_a) if actual_odds_a else prob_a
        _shrunk_b = _shrink(prob_b, actual_odds_b) if actual_odds_b else prob_b
        _sa = _stretch(_shrunk_a)
        _sb = _stretch(_shrunk_b)
        _adj_a = _sa / (_sa + _sb)
        _adj_b = _sb / (_sa + _sb)

        w(f"  {'':34} {'A':>12} {'B':>12}")
        w(f"  {'raw_model_prob':<34} {prob_a:>12.4f} {prob_b:>12.4f}")
        w(f"  {'adjusted_prob (shrink+stretch)':<34} {_adj_a:>12.4f} {_adj_b:>12.4f}")
        w(f"  {'fair_odds':<34} {fo_a:>12.2f} {fo_b:>12.2f}")
        _edge_a_dec = ev_a_result.edge
        _edge_b_dec = ev_b_result.edge
        w(f"  {'edge (decimal)':<34} {_edge_a_dec:>+12.4f} {_edge_b_dec:>+12.4f}")

        # ── V3. SELECTION ─────────────────────────────────────────────────
        w(f"\n  ── 3. SELECTION ──")
        w(_row("  best_ev_side", f"{best_side} ({pa.short_name if best_side == 'A' else pb.short_name})"))
        _edge_sel = ev_a_result.edge if best_side == "A" else ev_b_result.edge
        _edge_oth = ev_b_result.edge if best_side == "A" else ev_a_result.edge
        w(_row("  reason", f"edge {best_side} ({_edge_sel:+.4f}) > edge {'B' if best_side == 'A' else 'A'} ({_edge_oth:+.4f})"))

        # ── V4. GATE TRACE (selected side) ────────────────────────────────
        w(f"\n  ── 4. GATE TRACE (side {best_side}) ──")
        _sel_mo = actual_odds_a if best_side == "A" else actual_odds_b
        _sel_fo = fo_a if best_side == "A" else fo_b
        gate_lines = _format_gate_trace(
            _sel_mo, _sel_fo, validation, confidence, days_inactive, tour,
        )
        lines.extend(gate_lines)

        # ── V5. FINAL DECISION ────────────────────────────────────────────
        w(f"\n  ── 5. FINAL DECISION ──")
        sel_ev_v = ev_a_result if best_side == "A" else ev_b_result
        sel_gates_v = failed_gates_a if best_side == "A" else failed_gates_b
        w(_row("  filter_reason", sel_ev_v.filter_reason or "PASS"))
        w(_row("  first_failed_gate", sel_gates_v[0] if sel_gates_v else "NONE"))
        w(_row("  all_failed_gates", f"[{', '.join(sel_gates_v)}]" if sel_gates_v else "[]"))
        w(_row("  final_decision", "PASS" if sel_ev_v.is_value else "BLOCK"))

    w(f"\n{'═' * 70}\n")

    return "\n".join(lines)


# ── CLI entrypoint ───────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        handlers=[logging.StreamHandler()],
    )

    p = argparse.ArgumentParser(description="Targeted match audit")
    p.add_argument("--match", required=True, type=str,
                   help="'A. Player vs B. Player'")
    p.add_argument("--market_odds", type=float, nargs=2, metavar=("OA", "OB"),
                   required=True)
    p.add_argument("--surface", type=str, default="Hard")
    p.add_argument("--tournament", type=str, default="ATP Tour")
    p.add_argument("--level", type=str, default="ATP 250")
    p.add_argument("--tour", type=str, default="atp")
    p.add_argument("--bookmaker", type=str, default="")
    p.add_argument("--verbose", action="store_true",
                   help="Print full pipeline trace for debugging")
    args = p.parse_args()

    report = run_audit(
        match_str=args.match,
        market_odds_a=args.market_odds[0],
        market_odds_b=args.market_odds[1],
        surface=args.surface,
        tournament=args.tournament,
        tournament_lvl=args.level,
        tour=args.tour,
        bookmaker=args.bookmaker,
        verbose=args.verbose,
    )
    print(report)


if __name__ == "__main__":
    main()
