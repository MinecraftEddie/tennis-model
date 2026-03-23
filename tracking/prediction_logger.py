"""
tennis_model/tracking/prediction_logger.py
==========================================
Forward prediction logger.

Appends one JSONL record per evaluated match to data/forward_predictions.jsonl.
Records are written at prediction time (not result settlement) so every model
evaluation is persisted — both picks that pass and picks that are blocked.
"""
import json
import logging
import os
from datetime import date, datetime
from typing import Optional

from tennis_model.models import MatchPick
from tennis_model.config.runtime_config import (
    MODEL_VERSION, ELO_SHRINK, MARKET_WEIGHT, MC_WEIGHT, PROB_FLOOR,
    LONGSHOT_GUARD_THRESHOLD,
    UNDERDOG_EDGE_THRESHOLD_LOW_ODDS, UNDERDOG_EDGE_THRESHOLD_HIGH_ODDS,
)

log = logging.getLogger(__name__)

_OUTPUT_FILE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "forward_predictions.jsonl")
)

# Frozen at import time — all values sourced from config/runtime_config.py.
_MODEL_SNAPSHOT = {
    "model_version":                     MODEL_VERSION,
    "elo_shrink":                        ELO_SHRINK,
    "market_weight":                     MARKET_WEIGHT,
    "mc_weight":                         MC_WEIGHT,
    "prob_floor":                        PROB_FLOOR,
    "longshot_guard_threshold":          LONGSHOT_GUARD_THRESHOLD,
    "underdog_edge_threshold_low_odds":  UNDERDOG_EDGE_THRESHOLD_LOW_ODDS,
    "underdog_edge_threshold_high_odds": UNDERDOG_EDGE_THRESHOLD_HIGH_ODDS,
}

# Sources classified as "static" (manually maintained or curated snapshots).
_STATIC_SOURCES  = {"static_curated", "wta_static"}
# Sources classified as "dynamic" (live-scraped or API-fetched at run time).
_DYNAMIC_SOURCES = {"tennis_abstract", "tennis_abstract_dynamic",
                    "atp_api", "wta_estimated"}

# Risk flags that indicate structural data problems — same set used by the
# evaluator's hybrid-cautious Step 6b.  Any of these disqualifies a pick
# from the watchlist_plus paper tier.
_HARD_BLOCKERS = {
    "validation_failed", "unreliable_data_source", "estimated_profile",
    "incomplete_serve_stats", "no_market_odds", "stale_odds",
    "suspicious_edge_magnitude", "very_thin_surface_sample",
    "high_edge_no_tennis_abstract_serves", "strong_edge_with_minimal_surface_sample",
}


def _watchlist_plus(pick: MatchPick, picked_side: Optional[str],
                    eval_result: Optional[dict]) -> bool:
    """Return True if this watchlist case qualifies for paper-trade annotation.

    Conditions (ALL must hold):
      - evaluator decided "watchlist"
      - tour = ATP
      - quality_tier = CLEAN
      - confidence in MEDIUM | HIGH
      - edge on the picked side: 5 % <= edge < 7 %  (stored as float percent, e.g. 6.2)
      - no hard-blocker risk flag present
    Does NOT change any live betting decision — annotation only.
    """
    if not eval_result:
        return False
    if eval_result.get("recommended_action") != "watchlist":
        return False
    if (pick.tour or "").upper() != "ATP":
        return False
    if (pick.quality_tier or "").upper() != "CLEAN":
        return False
    if (pick.confidence or "") not in ("MEDIUM", "HIGH"):
        return False
    edge_pct = pick.edge_a if picked_side == "A" else (pick.edge_b if picked_side == "B" else None)
    if edge_pct is None or not (5.0 <= edge_pct < 7.0):
        return False
    if set(eval_result.get("risk_flags") or []) & _HARD_BLOCKERS:
        return False
    return True


def _build_reason_codes(pick: MatchPick,
                        picked_side: Optional[str],
                        eval_result: Optional[dict]) -> list:
    """
    Derive a list of short, machine-friendly reason codes from the already-computed
    pick fields.  Pure annotation — no thresholds or decisions are introduced here.

    Codes are ordered from most-general (decision) to most-specific (metadata)
    and are deduplicated while preserving insertion order.
    """
    codes: list = []
    fr  = (pick.filter_reason or "").upper()   # upper-cased for pattern matching
    pa  = pick.player_a
    pb  = pick.player_b

    # ── 1. Primary decision code (exactly one) ────────────────────────────
    if "EVALUATOR" in fr:
        codes.append("EVALUATOR_WATCHLIST" if "WATCHLIST" in fr else "EVALUATOR_BLOCK")
    elif fr:
        codes.append("PICK_BLOCKED" if picked_side else "NO_BET")
    elif picked_side:
        codes.append("PICK_ACCEPTED")
    else:
        codes.append("NO_BET")

    # ── 2. Edge verdict ───────────────────────────────────────────────────
    codes.append("EDGE_FAIL" if fr else "EDGE_PASS")

    # ── 3. Specific filter sub-codes (from ev.py / pipeline.py strings) ──
    # "WTA DATA GATE: ..."  |  "INSUFFICIENT DATA: ..."
    if "DATA GATE" in fr or "INSUFFICIENT DATA" in fr:
        codes.append("HIGH_GATE_BLOCK")
    # "ODDS @X.XX BELOW MINIMUM (1.5)"  |  "NO MARKET ODDS"  |  "INVALID_ODDS"
    if "BELOW MINIMUM" in fr or "NO MARKET ODDS" in fr or "INVALID_ODDS" in fr:
        codes.append("ODDS_BELOW_MIN")
    # "MODEL PROB X.X% BELOW FLOOR (40%)"
    if "BELOW FLOOR" in fr:
        codes.append("PROB_FLOOR_BLOCK")

    # ── 4. Evaluator action (from eval_result, always included when present) ─
    if eval_result:
        action = eval_result.get("recommended_action", "")
        if action == "send":
            codes.append("SEND")
        elif action == "send_with_caution":
            codes.append("SEND_WITH_CAUTION")
        elif action == "watchlist" and "EVALUATOR_WATCHLIST" not in codes:
            codes.append("EVALUATOR_WATCHLIST")
        elif action and action not in ("send", "send_with_caution", "watchlist"):
            if "EVALUATOR_BLOCK" not in codes:
                codes.append("EVALUATOR_BLOCK")

    # ── 5. Confidence tier ────────────────────────────────────────────────
    conf = (pick.confidence or "").upper()
    if conf == "HIGH":      codes.append("CONF_HIGH")
    elif conf == "MEDIUM":  codes.append("CONF_MEDIUM")
    elif conf == "LOW":     codes.append("CONF_LOW")

    # ── 6. Quality tier ───────────────────────────────────────────────────
    qt = (pick.quality_tier or "").upper()
    if qt == "CLEAN":       codes.append("QUALITY_CLEAN")
    elif qt == "CAUTION":   codes.append("QUALITY_CAUTION")
    elif qt == "FRAGILE":   codes.append("QUALITY_FRAGILE")

    # ── 7. Data sources ───────────────────────────────────────────────────
    for p, suffix in ((pa, "A"), (pb, "B")):
        src = p.data_source or ""
        if src in _STATIC_SOURCES:
            codes.append(f"DATA_SOURCE_STATIC_{suffix}")
        elif src in _DYNAMIC_SOURCES:
            codes.append(f"DATA_SOURCE_DYNAMIC_{suffix}")

    # ── 8. ELO availability ───────────────────────────────────────────────
    if pa.elo is not None:
        codes.append("ELO_AVAILABLE_A")
    if pb.elo is not None:
        codes.append("ELO_AVAILABLE_B")
    # When ELO is missing for either player, market odds are the primary shrink anchor
    if (pa.elo is None or pb.elo is None) and (pick.market_odds_a or pick.market_odds_b):
        codes.append("NO_ELO_FALLBACK_MARKET")

    # ── 9. Tour ───────────────────────────────────────────────────────────
    tour = (pick.tour or "").upper()
    if tour == "ATP":    codes.append("TOUR_ATP")
    elif tour == "WTA":  codes.append("TOUR_WTA")

    # ── 10. Favorite / underdog on picked side ────────────────────────────
    if picked_side == "A" and pick.market_odds_a:
        codes.append("FAVORITE_SIDE" if pick.market_odds_a < 2.00 else "UNDERDOG_SIDE")
    elif picked_side == "B" and pick.market_odds_b:
        codes.append("FAVORITE_SIDE" if pick.market_odds_b < 2.00 else "UNDERDOG_SIDE")

    # ── 11. Transformations applied ───────────────────────────────────────
    # Market shrink is conditional on odds being available; logit stretch always runs.
    if pick.market_odds_a or pick.market_odds_b:
        codes.append("MARKET_SHRINK_APPLIED")
    codes.append("LOGIT_STRETCH_APPLIED")

    # ── 12. Qualifier flag (not tracked in current pipeline) ──────────────
    # Included here for future use; skipped when not set.
    # if pick.qualifier: codes.append("QUALIFIER_MATCH")

    # Deduplicate while preserving insertion order
    seen: set = set()
    result: list = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def log_prediction(
    pick: MatchPick,
    *,
    raw_prob_a: float,
    raw_prob_b: float,
    adj_prob_a: float,
    adj_prob_b: float,
    eval_result: Optional[dict] = None,
) -> None:
    """Append a forward-tracking record for one evaluated match.

    Called at the end of run_match(), after quality_tier is set.
    Never raises — failures are logged as warnings so the pipeline is unaffected.
    """
    pa = pick.player_a
    pb = pick.player_b

    today = date.today().isoformat()
    last_a = pa.short_name.split(".")[-1].strip().lower().replace(" ", "_")
    last_b = pb.short_name.split(".")[-1].strip().lower().replace(" ", "_")
    match_id = f"{today}_{last_a}_{last_b}"

    mkt_p_a = round(1.0 / pick.market_odds_a, 4) if pick.market_odds_a else None
    mkt_p_b = round(1.0 / pick.market_odds_b, 4) if pick.market_odds_b else None

    picked_side = None
    if pick.pick_player == pa.short_name:
        picked_side = "A"
    elif pick.pick_player == pb.short_name:
        picked_side = "B"

    evaluator_decision = eval_result.get("recommended_action") if eval_result else None

    is_wlp = _watchlist_plus(pick, picked_side, eval_result)
    reason_codes = _build_reason_codes(pick, picked_side, eval_result)
    if is_wlp:
        reason_codes.append("WATCHLIST_PLUS_PAPER")

    record = {
        # Core identifiers
        "timestamp":          datetime.utcnow().isoformat() + "Z",
        "date":               today,
        "match_id":           match_id,
        "player_a":           pa.short_name,
        "player_b":           pb.short_name,
        "tour":               pick.tour,
        "tournament":         pick.tournament,
        "surface":            pick.surface,
        "round":              pick.round_name or None,
        "qualifier":          None,
        # Market + model
        "odds_a":             pick.market_odds_a,
        "odds_b":             pick.market_odds_b,
        "market_prob_a":      mkt_p_a,
        "market_prob_b":      mkt_p_b,
        "raw_prob_a":         round(raw_prob_a, 4),
        "raw_prob_b":         round(raw_prob_b, 4),
        "adjusted_prob_a":    round(adj_prob_a, 4),
        "adjusted_prob_b":    round(adj_prob_b, 4),
        "edge_a":             round(pick.edge_a, 4) if pick.edge_a is not None else None,
        "edge_b":             round(pick.edge_b, 4) if pick.edge_b is not None else None,
        # Decision fields
        "picked_side":        picked_side,
        "is_pick":            picked_side is not None,
        "stake_units":        getattr(pick, "stake_units", None),
        "confidence":         pick.confidence,
        "quality_tier":       pick.quality_tier or None,
        "evaluator_decision": evaluator_decision,
        "blocked_reason":     pick.filter_reason or None,
        "paper_tier":         "watchlist_plus" if is_wlp else None,
        "is_paper_pick":      is_wlp,
        "reason_codes":       reason_codes,
        # Useful metadata
        "data_source_a":      pa.data_source,
        "data_source_b":      pb.data_source,
        "elo_available_a":    pa.elo is not None,
        "elo_available_b":    pb.elo is not None,
        "model_version":      MODEL_VERSION,
        "model_snapshot":     _MODEL_SNAPSHOT,
    }

    os.makedirs(os.path.dirname(_OUTPUT_FILE), exist_ok=True)
    try:
        with open(_OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        log.debug(f"Forward record written: {match_id}")
    except OSError as exc:
        log.warning(f"Forward prediction log write failed: {exc}")
