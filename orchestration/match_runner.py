"""
orchestration/match_runner.py
==============================
P5 — MatchFinalStatus, MatchRunResult, build_final_status()
P6 — run_match_core(): core match evaluation extracted from pipeline.py

Decision chain:
  ProfileQualityResult → EvaluatorDecision → RiskDecision → AlertDecision
                                                           ↓
                                                    MatchRunResult (final_status)

run_match_core() responsibility
--------------------------------
Given pre-fetched profiles, H2H, days_inactive and market context, it:
  - runs validation, probability, EV, evaluator
  - routes to maybe_alert() if PICK
  - builds and returns MatchRunResult
  - never imports from pipeline.py (no circular dependency)

pipeline.py run_match_with_result() responsibility (thin coordinator)
-----------------------------------------------------------------------
  - parses match string
  - fetches live odds
  - fetches profiles (fetch_profile_with_quality)
  - fetches H2H (fetch_h2h, ATP-specific)
  - computes days_inactive (_days_inactive, uses ELO engine)
  - delegates to run_match_core()

Helpers that remain in pipeline.py (P6)
-----------------------------------------
  fetch_h2h()     — uses pipeline's SESSION and ATP/WTA HTTP logic
  _days_inactive()— uses ELO engine + date arithmetic

These can be extracted to a shared module in a future refactor (post-P6).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

# ── Evaluator: optional second-pass filter ────────────────────────────────────
try:
    from tennis_model.evaluator import evaluate as _evaluator_evaluate_fn
    _EVALUATOR_AVAILABLE = True
except ImportError:
    _evaluator_evaluate_fn = None
    _EVALUATOR_AVAILABLE = False


class MatchFinalStatus(str, Enum):
    """
    Normalised terminal status for a single match evaluation run.

    Stable across pipeline versions — safe to store in JSON / audit logs.
    """

    # ── PICK path (EV passed + evaluator approved) ────────────────────────────
    PICK_ALERT_SENT      = "PICK_ALERT_SENT"       # Telegram dispatched & confirmed
    PICK_DRY_RUN         = "PICK_DRY_RUN"           # No Telegram credentials — dry run
    PICK_SUPPRESSED      = "PICK_SUPPRESSED"         # FRAGILE quality — suppressed, not sent
    PICK_FAILED          = "PICK_FAILED"             # Telegram delivery failed (all retries)
    PICK_SKIPPED_UNKNOWN = "PICK_SKIPPED_UNKNOWN"    # UNKNOWN profile quality gate
    PICK_SKIPPED_KELLY   = "PICK_SKIPPED_KELLY"      # Kelly stake <= 0
    PICK_SKIPPED_RISK    = "PICK_SKIPPED_RISK"       # Risk cap hit
    PICK_SKIPPED_DEDUPE  = "PICK_SKIPPED_DEDUPE"     # Already alerted today (dedup)

    # ── Evaluator-blocked (EV passed, evaluator said no) ──────────────────────
    WATCHLIST            = "WATCHLIST"               # Evaluator: watchlist
    BLOCKED_MODEL        = "BLOCKED_MODEL"           # Evaluator: ignore

    # ── EV-blocked ────────────────────────────────────────────────────────────
    NO_PICK              = "NO_PICK"                 # EV filter blocked
    BLOCKED_VALIDATION   = "BLOCKED_VALIDATION"      # EV blocked + validation failed

    # ── Error ─────────────────────────────────────────────────────────────────
    FAILED               = "FAILED"                  # Exception mid-pipeline


# Sets used by scan_today() to classify results without string inspection
ALERT_SENT_STATUSES: frozenset = frozenset({
    MatchFinalStatus.PICK_ALERT_SENT,
    MatchFinalStatus.PICK_DRY_RUN,
    MatchFinalStatus.PICK_SUPPRESSED,
    MatchFinalStatus.PICK_FAILED,
    MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
    MatchFinalStatus.PICK_SKIPPED_KELLY,
    MatchFinalStatus.PICK_SKIPPED_RISK,
    MatchFinalStatus.PICK_SKIPPED_DEDUPE,
})

EVALUATOR_BLOCKED_STATUSES: frozenset = frozenset({
    MatchFinalStatus.WATCHLIST,
    MatchFinalStatus.BLOCKED_MODEL,
})


@dataclass
class MatchRunResult:
    """
    Unified output of a single match evaluation run.

    One object carries everything needed to audit, display, and count
    a match outcome.  Replaces the previous pattern of reading scattered
    MatchPick fields and filter_reason strings.

    Fields
    ------
    match_id            Derived ID (YYYY-MM-DD_lastnameA_lastnameB).
    player_a            short_name of player A.
    player_b            short_name of player B.
    profile_quality_a   "full" | "degraded" | "unknown".
    profile_quality_b   Same for player B.
    evaluator_decision  EvaluatorDecision — always set, even on NO_PICK.
    final_status        MatchFinalStatus — single source of truth for outcome.
    reason_codes        List of ReasonCode strings (machine-readable causes).
    risk_decision       RiskDecision — only set when PICK path was entered.
    alert_decision      AlertDecision — only set when PICK path was entered.
    pick                MatchPick — full model output (None only on FAILED).
    filter_reason       Legacy compat: mirrors pick.filter_reason (or None).

    P6 note: risk_decision is not yet surfaced here (it lives inside
    telegram.maybe_alert()).  A future refactor can expose it directly.
    """

    match_id:           str
    player_a:           str
    player_b:           str
    profile_quality_a:  str    # "full" | "degraded" | "unknown"
    profile_quality_b:  str
    evaluator_decision: object                        # EvaluatorDecision
    final_status:       MatchFinalStatus
    reason_codes:       list  = field(default_factory=list)
    risk_decision:      Optional[object] = None       # RiskDecision | None (P6)
    alert_decision:     Optional[object] = None       # AlertDecision | None
    pick:               Optional[object] = None       # MatchPick | None
    filter_reason:      Optional[str]   = None        # legacy compat
    # ── Audit intermediates (read-only, consumed by audit tools) ───────────
    ev_a:               Optional[object] = None       # EVResult side A
    ev_b:               Optional[object] = None       # EVResult side B
    best_ev_side:       Optional[str]    = None       # "A" or "B"
    days_inactive:      Optional[int]    = None       # max of both players
    validation:         Optional[object] = None       # ValidationResult


# ── Status builder ─────────────────────────────────────────────────────────────

def build_final_status(
    evaluator_decision,
    alert_decision=None,
) -> MatchFinalStatus:
    """
    Map an EvaluatorDecision (+ optional AlertDecision) to a MatchFinalStatus.

    Parameters
    ----------
    evaluator_decision : EvaluatorDecision
        Always present.
    alert_decision : AlertDecision | None
        Only present when EvaluatorDecision.status == PICK.

    Returns
    -------
    MatchFinalStatus
        Never raises.
    """
    from tennis_model.evaluator.evaluator_decision import EvaluatorStatus
    from tennis_model.orchestration.alert_status import AlertStatus

    es = evaluator_decision.status

    if es == EvaluatorStatus.WATCHLIST:
        return MatchFinalStatus.WATCHLIST
    if es == EvaluatorStatus.NO_PICK:
        return MatchFinalStatus.NO_PICK
    if es == EvaluatorStatus.BLOCKED_VALIDATION:
        return MatchFinalStatus.BLOCKED_VALIDATION
    if es == EvaluatorStatus.BLOCKED_MODEL:
        return MatchFinalStatus.BLOCKED_MODEL

    # EvaluatorStatus.PICK — outcome determined by AlertDecision
    if alert_decision is None:
        # Guard: should not happen; log-worthy but not fatal
        return MatchFinalStatus.NO_PICK

    _ALERT_MAP = {
        AlertStatus.SENT:            MatchFinalStatus.PICK_ALERT_SENT,
        AlertStatus.DRY_RUN:         MatchFinalStatus.PICK_DRY_RUN,
        AlertStatus.SUPPRESSED:      MatchFinalStatus.PICK_SUPPRESSED,
        AlertStatus.FAILED:          MatchFinalStatus.PICK_FAILED,
        AlertStatus.SKIPPED_UNKNOWN: MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
        AlertStatus.SKIPPED_KELLY:   MatchFinalStatus.PICK_SKIPPED_KELLY,
        AlertStatus.SKIPPED_RISK:    MatchFinalStatus.PICK_SKIPPED_RISK,
        AlertStatus.SKIPPED_DEDUPE:  MatchFinalStatus.PICK_SKIPPED_DEDUPE,
        AlertStatus.SKIPPED_NO_PICK: MatchFinalStatus.NO_PICK,
        AlertStatus.WATCHLIST:       MatchFinalStatus.WATCHLIST,  # legacy alias
    }
    return _ALERT_MAP.get(alert_decision.status, MatchFinalStatus.NO_PICK)


# ── Core match evaluator (P6) ─────────────────────────────────────────────────

def run_match_core(
    *,
    na: str,
    nb: str,
    pa,                   # PlayerProfile
    pb,                   # PlayerProfile
    _qr_a,                # ProfileQualityResult
    _qr_b,                # ProfileQualityResult
    h2h_a: int,
    h2h_b: int,
    h2h_s: str,
    days_inactive_a: int,
    days_inactive_b: int,
    tournament: str,
    tournament_lvl: str,
    surface: str,
    _tour: str,
    market_odds_a,
    market_odds_b,
    bookmaker: str,
    pick_number: int,
    odds_source: str,
    odds_timestamp: str = "",
    _silent: bool = False,
    _audit=None,
) -> MatchRunResult:
    """
    Core match evaluation: from validated inputs to MatchRunResult.

    Called by pipeline.run_match_with_result() which handles the parse/fetch
    layer.  Does not import from pipeline.py — no circular dependency.

    Responsibilities
    ----------------
    - validate_match
    - calculate_probability + ELO observability
    - market shrink + logit stretch
    - fair_odds + edge + sanity guards
    - confidence classification
    - data gate (ATP / WTA strictness)
    - compute_ev + pick selection
    - build MatchPick
    - evaluator second pass → EvaluatorDecision
    - quality tier
    - format + display (unless _silent)
    - route: maybe_alert() if PICK
    - forward prediction tracking
    - build + return MatchRunResult (with risk_decision from AlertDecision)
    - record to audit via record_match_result()
    """
    from tennis_model.model import calculate_probability, fair_odds, edge_pct
    from tennis_model.probability_adjustments import shrink_toward_market, logit_stretch
    from tennis_model.validation import validate_match
    from tennis_model.confidence import compute_confidence
    from tennis_model.ev import compute_ev, EVResult
    from tennis_model.elo import get_elo_engine, canonical_id
    from tennis_model.models import MatchPick
    from tennis_model.formatter import (
        format_pick_card, format_factor_table, format_value_analysis,
        _quality_tier,
    )
    from tennis_model.telegram import maybe_alert
    from tennis_model.evaluator.evaluator_decision import (
        build_evaluator_decision,
        EvaluatorStatus,
    )

    match_name = f"{na} vs {nb}"
    days_inactive = max(days_inactive_a, days_inactive_b)

    # ── Validation ────────────────────────────────────────────────────────────
    validation = validate_match(
        pa, pb, surface,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
        odds_source=odds_source,
        odds_timestamp=odds_timestamp,
    )
    if not validation.passed:
        log.warning(f"VALIDATION FAILED {match_name}: {validation.errors}")

    # ── Probability ───────────────────────────────────────────────────────────
    prob_a, prob_b, comps = calculate_probability(
        pa, pb, surface, h2h_a, h2h_b, market_odds_a, market_odds_b
    )

    # ELO observability: populate pa.elo / pb.elo for downstream diagnostics
    _elo_engine = get_elo_engine()
    for _prof in (pa, pb):
        _eid   = canonical_id(_prof.full_name or _prof.short_name)
        _entry = _elo_engine.ratings.get(_eid)
        _prof.elo = _entry.overall if (_entry and _entry.matches_played > 0) else None

    # ── Market shrink + logit stretch ─────────────────────────────────────────
    _pa_adj = shrink_toward_market(prob_a, market_odds_a) if market_odds_a else prob_a
    _pb_adj = shrink_toward_market(prob_b, market_odds_b) if market_odds_b else prob_b
    _sa, _sb = logit_stretch(_pa_adj), logit_stretch(_pb_adj)
    _pa_adj, _pb_adj = _sa / (_sa + _sb), _sb / (_sa + _sb)

    fo_a, fo_b = fair_odds(_pa_adj), fair_odds(_pb_adj)
    ea = edge_pct(market_odds_a, fo_a) if market_odds_a else None
    eb = edge_pct(market_odds_b, fo_b) if market_odds_b else None

    # ── Sanity guards ─────────────────────────────────────────────────────────
    if not (0.0 < prob_a < 1.0 and 0.0 < prob_b < 1.0):
        raise RuntimeError(
            f"Probability out of range: prob_a={prob_a}, prob_b={prob_b}"
        )
    if abs((prob_a + prob_b) - 1.0) >= 0.01:
        raise RuntimeError(
            f"Probabilities do not sum to 1.0: {prob_a + prob_b:.6f}"
        )
    if fo_a < 1.0 or fo_b < 1.0:
        raise RuntimeError(
            f"Fair odds below 1.0: fo_a={fo_a}, fo_b={fo_b}"
        )

    # ── Confidence ────────────────────────────────────────────────────────────
    max_edge_dec = max(ea or 0.0, eb or 0.0) / 100.0
    confidence = compute_confidence(
        pa, pb, surface, validation,
        edge=max_edge_dec,
        model_prob=max(prob_a, prob_b),
        days_inactive=days_inactive,
    )
    if days_inactive > 0:
        log.info(f"Days inactive (max of both players): {days_inactive}")

    # ── Data gate ─────────────────────────────────────────────────────────────
    # ATP: block if both players are wta_estimated.
    # WTA: stricter — both must have tennis_abstract_dynamic profiles.
    _gate_reason = None
    if pa.data_source == "wta_estimated" and pb.data_source == "wta_estimated":
        _gate_reason = "INSUFFICIENT DATA: both players unrecognised"
    elif _tour == "wta":
        _bad = [f"{p.short_name}={p.data_source}" for p in [pa, pb]
                if p.data_source != "tennis_abstract_dynamic"]
        if _bad:
            _gate_reason = f"WTA DATA GATE: {', '.join(_bad)}"

    if _gate_reason:
        log.warning(f"PICK BLOCKED — {_gate_reason}")
        _block = EVResult(edge=0.0, is_value=False, filter_reason=_gate_reason)
        ev_a = ev_b = best_ev = _block
    else:
        ev_a = (compute_ev(market_odds_a, fo_a, validation, confidence, days_inactive, tour=_tour)
                if market_odds_a else EVResult(edge=0.0, is_value=False,
                                               filter_reason="NO MARKET ODDS"))
        ev_b = (compute_ev(market_odds_b, fo_b, validation, confidence, days_inactive, tour=_tour)
                if market_odds_b else EVResult(edge=0.0, is_value=False,
                                               filter_reason="NO MARKET ODDS"))
        best_ev = ev_a if ev_a.edge > ev_b.edge else ev_b

    # ── Pick selection ────────────────────────────────────────────────────────
    pick_player = ""
    if ea is not None and eb is not None:
        if ea >= eb and ea > 0:  pick_player = na
        elif eb > 0:             pick_player = nb

    pick_tour = "WTA" if _tour == "wta" else "ATP"

    pick = MatchPick(
        player_a=pa, player_b=pb, surface=surface,
        tournament=tournament, tournament_level=tournament_lvl,
        tour=pick_tour,
        prob_a=prob_a, prob_b=prob_b,
        fair_odds_a=fo_a, fair_odds_b=fo_b,
        market_odds_a=market_odds_a, market_odds_b=market_odds_b,
        edge_a=ea, edge_b=eb,
        pick_player=pick_player, bookmaker=bookmaker,
        h2h_summary=h2h_s, factor_breakdown=comps,
        simulation=comps.get("monte_carlo", {}),
        confidence=confidence,
        validation_passed=validation.passed,
        filter_reason=best_ev.filter_reason or "",
        validation_warnings=validation.warnings,
        odds_source=odds_source,
    )

    # ── Evaluator second pass ─────────────────────────────────────────────────
    eval_result: dict = {}
    if _EVALUATOR_AVAILABLE:
        _match_ctx = {
            "is_live":         False,
            "days_inactive_a": days_inactive_a,
            "days_inactive_b": days_inactive_b,
        }
        try:
            eval_result = _evaluator_evaluate_fn(pick, _match_ctx)
            pick.evaluator_result = eval_result
            log.info(
                f"Evaluator: {eval_result['alert_level'].upper()} — "
                f"{eval_result['recommended_action'].upper()} — "
                f"{eval_result.get('short_message', '')}"
            )
            for flag in eval_result.get("risk_flags", []):
                log.warning(f"RISK FLAG: {flag}")
        except Exception as exc:
            log.warning(f"Evaluator error — skipping second-pass filter: {exc}")
            eval_result = {}

    # ── EvaluatorDecision (single source of truth for routing) ───────────────
    _ed = build_evaluator_decision(best_ev, eval_result, validation.passed)
    if _ed.filter_reason is not None:
        pick.filter_reason = _ed.filter_reason

    # ── Quality tier (output-only) ────────────────────────────────────────────
    pick.quality_tier = _quality_tier(pick)

    # ── Format + display ──────────────────────────────────────────────────────
    card     = format_pick_card(pick, pick_number)
    table    = format_factor_table(pick)
    analysis = format_value_analysis(pick)
    if not _silent:
        print("\n" + card + table + analysis + "\n")

    # ── Route: log outcome, call maybe_alert if PICK ──────────────────────────
    _ad = None  # AlertDecision — set in PICK path only
    if _ed.status == EvaluatorStatus.PICK:
        _ad = maybe_alert(pick, card + "\n" + analysis)
    elif _ed.status == EvaluatorStatus.WATCHLIST:
        log.info(
            f"WATCHLIST: {na} vs {nb} — "
            f"{eval_result.get('reasons', [])}"
        )
        log.warning(
            f"EV passed but evaluator blocked: "
            f"{_ed.recommended_action} — {_ed.message or ''}"
        )
    else:
        # NO_PICK, BLOCKED_VALIDATION, BLOCKED_MODEL
        if _ed.recommended_action not in ("blocked",):
            log.warning(
                f"EV passed but evaluator blocked: "
                f"{_ed.recommended_action} — {_ed.message or ''}"
            )
        else:
            log.info(f"FILTERED {match_name}: {_ed.filter_reason}")

    # ── Forward prediction tracking ───────────────────────────────────────────
    try:
        from tennis_model.tracking.prediction_logger import log_prediction
        log_prediction(
            pick,
            raw_prob_a=prob_a,
            raw_prob_b=prob_b,
            adj_prob_a=_pa_adj,
            adj_prob_b=_pb_adj,
            eval_result=eval_result if eval_result else None,
        )
    except Exception as _log_exc:
        log.warning(f"Forward prediction log skipped: {_log_exc}")

    # ── Build MatchRunResult ──────────────────────────────────────────────────
    _final_status = build_final_status(_ed, _ad)
    _match_id = (
        f"{date.today().isoformat()}"
        f"_{na.split()[-1].lower()}"
        f"_{nb.split()[-1].lower()}"
    )
    _result = MatchRunResult(
        match_id=_match_id,
        player_a=na,
        player_b=nb,
        profile_quality_a=_qr_a.quality.value,
        profile_quality_b=_qr_b.quality.value,
        evaluator_decision=_ed,
        final_status=_final_status,
        reason_codes=[_ed.reason_code],
        # P6: risk_decision extracted from AlertDecision (populated by maybe_alert)
        risk_decision=_ad.risk_decision if _ad is not None else None,
        alert_decision=_ad,
        pick=pick,
        filter_reason=pick.filter_reason or None,
        ev_a=ev_a,
        ev_b=ev_b,
        best_ev_side="A" if ev_a.edge > ev_b.edge else "B",
        days_inactive=days_inactive,
        validation=validation,
    )

    # ── Audit recording ───────────────────────────────────────────────────────
    if _audit is not None:
        try:
            _audit.record_match_result(_result)
        except Exception as _ae:
            log.warning(f"[AUDIT] record failed (non-blocking): {_ae}")

    # ── Pick tracking (Step 1 post-P6) ───────────────────────────────────────
    try:
        from tennis_model.tracking.pick_store import maybe_record_pick
        maybe_record_pick(_result)
    except Exception as _pt_exc:
        log.warning(f"[PICK_TRACK] record failed (non-blocking): {_pt_exc}")

    return _result


# ── Match coordinator (P6) ────────────────────────────────────────────────────

def run_match_with_result(
    match_str:      str,
    tournament:     str            = "ATP Tour",
    tournament_lvl: str            = "ATP 250",
    surface:        str            = "Hard",
    market_odds_a:  Optional[float]= None,
    market_odds_b:  Optional[float]= None,
    bookmaker:      str            = "",
    pick_number:    int            = 1,
    tour:           str            = "",
    odds_timestamp: str            = "",
    _silent:        bool           = False,
    _prefetched:    bool           = False,
    _audit=None,           # Optional[DailyAudit]
) -> MatchRunResult:
    """
    Parse → fetch odds → fetch profiles → H2H → delegate to run_match_core().

    P6: Canonical home of this logic (moved from pipeline.py).
    pipeline.run_match_with_result() is now a thin backward-compat wrapper.

    Responsibilities
    ----------------
    - Parse match string (A vs B)
    - Fetch live odds via odds_feed (when not pre-fetched)
    - Fetch profiles with quality via profile_fetcher
    - Fetch H2H + inactivity days (via lazy import from pipeline — stays there)
    - Delegate to run_match_core() and return MatchRunResult

    Helpers still in pipeline.py (documented P6 constraint)
    --------------------------------------------------------
    fetch_h2h()      — uses pipeline's SESSION + ATP/WTA HTTP logic
    _days_inactive() — uses ELO engine + date arithmetic
    These are imported lazily (inside the function body) to avoid circular deps.
    """
    # Lazy imports — avoids circular dependency at module level.
    # By call-time both modules are fully initialised.
    from tennis_model.odds_feed import get_live_odds
    from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
    from tennis_model.pipeline import fetch_h2h, _days_inactive  # intentional lazy import

    sep   = re.compile(r"\s+vs\.?\s+", re.I)
    parts = sep.split(match_str.strip())
    if len(parts) != 2:
        raise ValueError(f"Cannot parse '{match_str}' — use 'A. Player vs B. Player'")

    na, nb = parts[0].strip(), parts[1].strip()
    log.info(f"\n{'═'*60}\nMATCH: {na} vs {nb}  [{tournament} · {surface}]\n{'═'*60}")

    # Derive tour: use explicit arg, else infer from tournament string
    _tour = (tour or ("wta" if "wta" in tournament.lower() or "wta" in tournament_lvl.lower()
                      else "atp")).lower()

    # --- Live odds ---
    odds_source = "manual"
    if _prefetched:
        if market_odds_a or market_odds_b:
            odds_source = "live"
            log.info(f"Using prefetched odds ({bookmaker}): {market_odds_a}/{market_odds_b}")
    else:
        live = get_live_odds(na, nb, tour=_tour)
        if live:
            market_odds_a  = live["odds_a"]
            market_odds_b  = live["odds_b"]
            bookmaker      = live["bookmaker"]
            odds_timestamp = live["timestamp"]
            odds_source    = "live"
            log.info(f"Live odds from {bookmaker}: {market_odds_a}/{market_odds_b}")
        elif market_odds_a or market_odds_b:
            log.warning("Using manual odds — may be stale")

    # --- Profiles ---
    pa, _qr_a = fetch_profile_with_quality(na, tour=_tour)
    pb, _qr_b = fetch_profile_with_quality(nb, tour=_tour)
    log.info(
        f"[QUALITY] {na}: {_qr_a.quality.value} ({_qr_a.data_source}) | "
        f"{nb}: {_qr_b.quality.value} ({_qr_b.data_source})"
    )

    # --- H2H + inactivity ---
    h2h_a, h2h_b, h2h_s = fetch_h2h(pa, pb)
    days_inactive_a = _days_inactive(pa)
    days_inactive_b = _days_inactive(pb)

    return run_match_core(
        na=na, nb=nb, pa=pa, pb=pb,
        _qr_a=_qr_a, _qr_b=_qr_b,
        h2h_a=h2h_a, h2h_b=h2h_b, h2h_s=h2h_s,
        days_inactive_a=days_inactive_a,
        days_inactive_b=days_inactive_b,
        tournament=tournament,
        tournament_lvl=tournament_lvl,
        surface=surface,
        _tour=_tour,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
        bookmaker=bookmaker,
        pick_number=pick_number,
        odds_source=odds_source,
        odds_timestamp=odds_timestamp,
        _silent=_silent,
        _audit=_audit,
    )
