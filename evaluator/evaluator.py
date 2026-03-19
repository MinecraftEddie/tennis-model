"""
Main alert evaluator orchestrator.
Reads MatchPick, applies tennis rules, returns alert decision JSON.
"""
import logging
from typing import Optional

from tennis_model.evaluator.rules import (
    evaluate_surface_fit,
    evaluate_serve_consistency,
    evaluate_return_quality,
    evaluate_break_point_likelihood,
    evaluate_fatigue_risk,
    score_tournament_level,
    evaluate_serve_stability,
    evaluate_return_pressure,
    evaluate_break_point_performance,
    evaluate_surface_sample_size,
    evaluate_model_consistency,
)
from tennis_model.evaluator.risk_flags import (
    detect_risk_flags,
    detect_match_context_risks,
    detect_model_sanity_risks,
)
from tennis_model.evaluator.momentum import (
    compute_set_context,
    reweight_confidence_for_live,
    compute_momentum_direction,
    analyze_serve_momentum,
    analyze_rally_dynamics,
    compute_break_point_frequency,
    compute_game_length_trend,
)
from tennis_model.evaluator.formatter import build_alert_decision

log = logging.getLogger(__name__)


def _generate_match_id(pick) -> str:
    """Generate unique match identifier from MatchPick."""
    a_clean = pick.player_a.short_name.lower().replace(" ", "_")
    b_clean = pick.player_b.short_name.lower().replace(" ", "_")
    return f"{a_clean}_vs_{b_clean}_{pick.surface.lower()}"


def evaluate(pick, match_context: Optional[dict] = None) -> dict:
    """
    Main evaluator: assess MatchPick and return alert decision.
    
    Args:
        pick: MatchPick object from pipeline
        match_context: optional dict with keys:
            - is_live: bool (default False)
            - current_sets: list of (a_score, b_score) tuples
            - games_in_current_set: (games_a, games_b)
            - days_inactive_a: int (days since last match for player_a)
            - days_inactive_b: int (days since last match for player_b)
            - prev_tournament_surface: str (surface of previous tournament, if changed)
            
    Returns:
        Alert decision dict:
        {
            "match_id": str,
            "alert_level": "low" | "medium" | "high",
            "confidence": float 0.0-1.0,
            "recommended_action": "send" | "send_with_caution" | "watchlist" | "ignore",
            "reasons": [list of strings],
            "risk_flags": [list of strings],
            "short_message": str
        }
    """
    match_context = match_context or {}
    
    match_id = _generate_match_id(pick)
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 1: Check if there's an edge to evaluate
    # ──────────────────────────────────────────────────────────────────────────────
    
    edge_a = pick.edge_a or 0.0
    edge_b = pick.edge_b or 0.0
    pick_player = None
    if pick.pick_player:
        pick_player = pick.pick_player
        edge = edge_a if pick_player == pick.player_a.short_name else edge_b
    elif edge_a > edge_b:
        edge = edge_a
        pick_player = pick.player_a.short_name
    else:
        edge = edge_b
        pick_player = pick.player_b.short_name
    
    # No edge? Ignore
    # pick.edge_a/b are stored as percentages (e.g. 7.1 = 7.1%); convert to decimal
    edge_dec = (edge / 100.0) if edge is not None else None
    if edge_dec is None or edge_dec < 0.01:
        return build_alert_decision(
            match_id=match_id,
            alert_level="low",
            confidence=0.0,
            recommended_action="ignore",
            reasons=["No meaningful edge"],
            risk_flags=[],
            short_message="No edge — ignore"
        )

    # Hard-block: implausibly large edge magnitude usually indicates mismatched
    # profiles/participants or broken inputs (e.g., missing profile resolved to
    # a generic player). Keep this early and explicit so it can't "pass" via
    # confidence blending.
    if edge_dec > 0.50:
        _pp = pick.player_a if pick_player == pick.player_a.short_name else pick.player_b
        _op = pick.player_b if pick_player == pick.player_a.short_name else pick.player_a
        _mo = (pick.market_odds_a if pick_player == pick.player_a.short_name
               else pick.market_odds_b) or 0.0
        _fo = (pick.fair_odds_a if pick_player == pick.player_a.short_name
               else pick.fair_odds_b) or 0.0
        _prob = (pick.prob_a if pick_player == pick.player_a.short_name
                 else pick.prob_b) or 0.0
        log.warning(
            f"SUSPICIOUS EDGE HARD-BLOCK | match={match_id} | "
            f"pick={pick_player} | edge={edge:+.1f}% (dec={edge_dec:.3f}) | "
            f"prob={_prob:.3f} | market=@{_mo:.2f} fair=@{_fo:.2f} | "
            f"src_pick={_pp.data_source} src_opp={_op.data_source}"
        )
        return build_alert_decision(
            match_id=match_id,
            alert_level="low",
            confidence=0.0,
            recommended_action="ignore",
            reasons=[f"Suspicious edge magnitude ({edge:+.1f}%)"],
            risk_flags=["suspicious_edge_magnitude"],
            short_message=f"Suspicious edge ({edge:+.1f}%) — ignore",
        )

    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 2: Evaluate tennis rules (basic + advanced)
    # ──────────────────────────────────────────────────────────────────────────────
    
    reasons = []
    rule_scores = []
    
    # Surface fit
    surf_score, surf_reason = evaluate_surface_fit(pick, pick.surface)
    rule_scores.append(surf_score)
    if surf_score >= 0.70:
        reasons.append(surf_reason)
    
    # Surface reliability (informational only — confidence.py handles penalties)
    # Note: Do NOT append to rule_scores to avoid double-penalty with confidence.py
    surf_sample_score, surf_sample_reason = evaluate_surface_sample_size(pick, pick.surface)
    if surf_sample_score < 0.80:
        reasons.append(f"Surface sample: {surf_sample_reason}")
    
    # Basic serve consistency (to be enhanced by stability)
    serve_score, serve_reason = evaluate_serve_consistency(pick)
    rule_scores.append(serve_score)
    if serve_score >= 0.70:
        reasons.append(serve_reason)
    
    # Advanced serve stability (first serve %, hold %, trend)
    serve_stable_score, serve_stable_reason = evaluate_serve_stability(pick)
    rule_scores.append(serve_stable_score)
    if serve_stable_score >= 0.75:
        reasons.append(f"Serve stability: {serve_stable_reason}")
    
    # Return quality
    return_score, return_reason = evaluate_return_quality(pick)
    rule_scores.append(return_score)
    if return_score >= 0.70:
        reasons.append(return_reason)
    
    # Advanced return pressure (break points, opponent serve weakness)
    return_press_score, return_press_reason = evaluate_return_pressure(pick)
    rule_scores.append(return_press_score)
    if return_press_score >= 0.75:
        reasons.append(f"Return: {return_press_reason}")
    
    # Break point clutch performance
    bp_clutch_score, bp_clutch_reason = evaluate_break_point_performance(pick)
    rule_scores.append(bp_clutch_score)
    if bp_clutch_score >= 0.75:
        reasons.append(f"Break points: {bp_clutch_reason}")
    
    # Break point likelihood (surface-specific)
    bp_score, bp_reason = evaluate_break_point_likelihood(pick, pick.surface)
    rule_scores.append(bp_score)
    if bp_score >= 0.70:
        reasons.append(bp_reason)
    
    # Fatigue
    fatigue_score, fatigue_reason = evaluate_fatigue_risk(pick)
    rule_scores.append(fatigue_score)
    if fatigue_score >= 0.80:
        reasons.append(fatigue_reason)
    
    # Tournament level
    tourney_score, tourney_reason = score_tournament_level(pick)
    rule_scores.append(tourney_score)
    
    # Model consistency check (high edge should match stats)
    consistency_score, consistency_reason = evaluate_model_consistency(pick)
    rule_scores.append(consistency_score)
    if "sanity" in consistency_reason.lower():
        reasons.append(consistency_reason)
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 3: Synthesize confidence
    # ──────────────────────────────────────────────────────────────────────────────
    
    # Average of rule scores
    avg_rule_score = sum(rule_scores) / len(rule_scores) if rule_scores else 0.50
    
    # Blend with model confidence + edge
    model_confidence_boost = 0.0
    if pick.confidence == "VERY HIGH":
        model_confidence_boost = 0.20
    elif pick.confidence == "HIGH":
        model_confidence_boost = 0.10
    elif pick.confidence == "MEDIUM":
        model_confidence_boost = 0.05
    
    edge_boost = min(0.15, edge_dec * 0.5)  # cap edge boost (edge_dec is decimal)

    base_confidence = (avg_rule_score + model_confidence_boost + edge_boost) / 2.0
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 4: Adjust for live match context
    # ──────────────────────────────────────────────────────────────────────────────
    
    is_live = match_context.get("is_live", False)
    current_sets = match_context.get("current_sets", [])
    games_in_set = match_context.get("games_in_current_set", (0, 0))
    
    if is_live and current_sets:
        set_context = compute_set_context(current_sets)
        
        # Classify alert level provisionally
        if base_confidence >= 0.80:
            prov_alert_level = "high"
        elif base_confidence >= 0.60:
            prov_alert_level = "medium"
        else:
            prov_alert_level = "low"
        
        # Reweight for live conditions
        base_confidence = reweight_confidence_for_live(
            base_confidence,
            prov_alert_level,
            pick.surface,
            set_context,
            games_in_set,
        )
        
        momentum = compute_momentum_direction(set_context, games_in_set, pick_player)
        if momentum == "positive":
            reasons.append(f"Positive momentum in current set")
        elif momentum == "negative":
            reasons.append(f"Negative momentum in current set")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 5: Detect risk flags (data quality, context, model sanity)
    # ──────────────────────────────────────────────────────────────────────────────
    
    # Basic data quality risks
    risk_flags = detect_risk_flags(pick, match_context)
    
    # Match context risks (back-to-back, tournament surface change, same-day matches)
    # Extracts data from match_context (days_inactive, prev_tournament_surface)
    days_inactive_a = match_context.get("days_inactive_a", 2) if match_context else 2
    days_inactive_b = match_context.get("days_inactive_b", 2) if match_context else 2
    prev_surface = match_context.get("prev_tournament_surface", "") if match_context else ""
    context_risks = detect_match_context_risks(pick, days_inactive_a, days_inactive_b, prev_surface)
    risk_flags.extend(context_risks)
    
    # Model sanity risks (edge-stats misalignment)
    sanity_risks = detect_model_sanity_risks(pick)
    risk_flags.extend(sanity_risks)
    
    risk_flags = list(set(risk_flags))  # deduplicate
    
    # Risk flags reduce confidence (5% per flag, capped)
    confidence_penalty = min(0.40, len(risk_flags) * 0.05)
    final_confidence = max(0.0, base_confidence - confidence_penalty)
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 6: Classify alert level and recommend action (with advanced risk context)
    # ──────────────────────────────────────────────────────────────────────────────
    
    if final_confidence >= 0.80:
        alert_level = "high"
    elif final_confidence >= 0.60:
        alert_level = "medium"
    else:
        alert_level = "low"
    
    # Critical sanity risks that hard-block sending regardless of confidence.
    # high_edge_misaligned_with_model is intentionally excluded — it is a soft
    # signal that demotes "send" → "send_with_caution" but never hard-blocks.
    # suspicious_edge_magnitude (40–50% range) is included: the ev.py hard-block
    # fires at >50% but the risk flag fires at >40%; this closes the gap window.
    has_critical_sanity_risk = (
        any(
            flag in sanity_risks
            for flag in [
                "strong_edge_with_minimal_surface_sample",
                "high_edge_no_tennis_abstract_serves",
            ]
        )
        or "suspicious_edge_magnitude" in risk_flags
    )
    if has_critical_sanity_risk:
        recommended_action = "watchlist"

    # Check for critical fatigue risks
    has_critical_context_risk = any(
        flag in context_risks
        for flag in [
            "back_to_back_matches",
            "short_rest_after_long_match_a",
            "short_rest_after_long_match_b",
            "significant_travel_fatigue",
        ]
    )

    # Standard decision logic (edge_dec is decimal: 0.07 = 7%)
    if not has_critical_sanity_risk:
        if edge_dec < 0.07:
            # Weak edge → always ignore or watchlist
            recommended_action = "ignore" if final_confidence < 0.70 else "watchlist"
        elif alert_level == "high" and edge_dec >= 0.10:
            # High confidence + decent edge + no context risk
            if has_critical_context_risk:
                recommended_action = "watchlist"
            else:
                recommended_action = "send"
        elif alert_level == "medium" and edge_dec >= 0.12:
            # Medium confidence + strong edge
            if ("dominant_single_set_on_clay" in risk_flags or has_critical_context_risk):
                recommended_action = "watchlist"
            else:
                recommended_action = "send"
        elif alert_level == "medium" and edge_dec >= 0.08:
            # Medium confidence + decent edge but watchlist for uncertainty
            recommended_action = "watchlist"
        else:
            # Default to ignore for weak confidence + weak edge
            recommended_action = "ignore"

    # Soft downgrade: high_edge_misaligned_with_model demotes "send" → "send_with_caution"
    # This enforces Policy A: market disagreement + fragile data = caution, never auto-send.
    if "high_edge_misaligned_with_model" in sanity_risks and recommended_action == "send":
        recommended_action = "send_with_caution"

    # WTA clay/grass surface serve mismatch: hard-biased career stats used as clay/grass proxy.
    # Even if confidence is sufficient, this is not a premium setup — cap at send_with_caution.
    # Mirrors the high_edge_misaligned_with_model pattern: structural data gap ≠ auto-send.
    if "wta_serve_surface_mismatch" in risk_flags and recommended_action == "send":
        recommended_action = "send_with_caution"

    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 6b: Hybrid cautious mode
    # When the evaluator would otherwise ignore/watchlist with no hard data-quality
    # blockers, but the edge is real, allow through at a reduced stake tier.
    # Soft flags (high_edge_misaligned_with_model, validation_warnings, etc.)
    # do NOT count as hard blockers here — only true data/integrity failures do.
    # ──────────────────────────────────────────────────────────────────────────────
    _HARD_BLOCKERS = {
        "validation_failed", "unreliable_data_source", "estimated_profile",
        "incomplete_serve_stats", "no_market_odds", "stale_odds",
        "suspicious_edge_magnitude", "very_thin_surface_sample",
        "high_edge_no_tennis_abstract_serves", "strong_edge_with_minimal_surface_sample",
    }
    no_hard_blockers = not any(f in risk_flags for f in _HARD_BLOCKERS)
    if (recommended_action in ("ignore", "watchlist")
            and 0.07 <= edge_dec <= 0.35
            and no_hard_blockers
            and final_confidence > 0.30):
        recommended_action = "send_with_caution"

    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 7: Build short message
    # ──────────────────────────────────────────────────────────────────────────────
    
    if pick_player:
        short_msg = f"{pick_player} vs {pick.player_b.short_name if pick_player == pick.player_a.short_name else pick.player_a.short_name}"
        short_msg += f": {edge:+.1f}% edge"  # edge already in % (e.g. 7.1)
        short_msg += f", {int(final_confidence*100)}% confidence"
    else:
        short_msg = f"{pick.tournament}: {edge:+.1f}% edge"  # edge already in %
    
    # ──────────────────────────────────────────────────────────────────────────────
    # STEP 8: Return decision
    # ──────────────────────────────────────────────────────────────────────────────
    # NOTE: Watchlist logging happens in pipeline.py via evaluator.log_watchlist_item()
    # Only "send" recommendation goes to Telegram (legacy behavior).
    # "watchlist" recommendation is logged to data/watchlist.json for manual review.
    # ──────────────────────────────────────────────────────────────────────────────
    
    return build_alert_decision(
        match_id=match_id,
        alert_level=alert_level,
        confidence=final_confidence,
        recommended_action=recommended_action,
        reasons=reasons,
        risk_flags=risk_flags,
        short_message=short_msg,
    )
