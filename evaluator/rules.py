"""
Tennis-specific evaluation rules.
Surface fit, serve consistency, return quality, break points, fatigue, etc.
"""

from tennis_model.evaluator.serve_utils import _get_serve_metric


def evaluate_surface_fit(pick, surface: str) -> tuple[float, str]:
    """
    Score surface fit for the pick player.
    
    Args:
        pick: MatchPick object
        surface: "Hard", "Clay", or "Grass"
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    surf = surface.lower()
    wins = getattr(player, f"{surf}_wins", 0)
    losses = getattr(player, f"{surf}_losses", 0)
    total = wins + losses
    
    if total == 0:
        return 0.50, f"No {surface} record"
    
    win_pct = wins / total if total > 0 else 0.0
    
    # Score based on win percentage
    if win_pct >= 0.65:
        score = 0.90
        reason = f"Strong {surface} record ({int(win_pct*100)}%)"
    elif win_pct >= 0.55:
        score = 0.75
        reason = f"Solid {surface} record ({int(win_pct*100)}%)"
    elif win_pct >= 0.50:
        score = 0.60
        reason = f"Neutral {surface} record ({int(win_pct*100)}%)"
    else:
        score = 0.40
        reason = f"Below-average {surface} record ({int(win_pct*100)}%)"
    
    return score, reason


def evaluate_serve_consistency(pick) -> tuple[float, str]:
    """
    Score serve consistency/reliability.
    Uses serve stats if available, else estimates from win patterns.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    serve_stats = player.serve_stats or {}

    _real_sources = ("tennis_abstract", "tennis_abstract_wta")
    # Real Tennis Abstract serve stats (ATP or WTA)
    if serve_stats.get("source") in _real_sources:
        serve_pct = serve_stats.get("serve_win_pct", None)
        if serve_pct:
            if serve_pct >= 0.65:
                return 0.85, f"Strong serve ({int(serve_pct*100)}%)"
            elif serve_pct >= 0.55:
                return 0.70, f"Solid serve ({int(serve_pct*100)}%)"
            else:
                return 0.55, f"Weak serve ({int(serve_pct*100)}%)"
    
    # Fallback: proxy from recent form
    recent = player.recent_form or []
    if len(recent) >= 5:
        recent_wins = sum(1 for r in recent[-5:] if r == 'W')
        serve_proxy = recent_wins / 5
        if serve_proxy >= 0.80:
            return 0.75, "Strong recent form (proxy serve)"
        elif serve_proxy >= 0.60:
            return 0.60, "Decent recent form (proxy serve)"
        else:
            return 0.50, "Weak recent form (proxy serve)"
    
    return 0.55, "Insufficient serve data"


def evaluate_return_quality(pick) -> tuple[float, str]:
    """
    Score return quality (ability to break serve).
    Estimated from break point stats and win patterns off serve.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
        opponent = pick.player_b
    else:
        player = pick.player_b
        opponent = pick.player_a
    
    # If opponent has weak serve, return is more valuable
    opp_serve_stats = opponent.serve_stats or {}
    opp_serve_pct = opp_serve_stats.get("serve_win_pct", 0.60)
    
    # Estimate return from recent results
    recent = player.recent_form or []
    if len(recent) >= 5:
        recent_wins = sum(1 for r in recent[-5:] if r == 'W')
        return_proxy = recent_wins / 5
        if return_proxy >= 0.80:
            return 0.75, "Strong break potential"
        elif return_proxy >= 0.60:
            return 0.65, "Good break potential"
        else:
            return 0.50, "Limited break potential"
    
    return 0.55, "Insufficient return data"


def evaluate_break_point_likelihood(pick, surface: str) -> tuple[float, str]:
    """
    Estimate likelihood of clinch break points.
    Based on surface characteristics and player styles.
    
    Args:
        pick: MatchPick object
        surface: "Hard", "Clay", or "Grass"
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    # Hard courts: fewer breaks, tighter holds
    # Clay: more breaks, longer rallies
    # Grass: significant break opportunities
    
    surface_break_baseline = {
        "hard": 0.60,
        "clay": 0.75,
        "grass": 0.70,
    }
    
    baseline = surface_break_baseline.get(surface.lower(), 0.60)
    
    # Adjust for player aggressive styles (estimated from titles/ranking)
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    # Higher ranking = tighter, fewer breaks
    if player.ranking and player.ranking < 50:
        baseline *= 0.90
    
    return baseline, f"Break point likelihood on {surface}"


def evaluate_fatigue_risk(pick) -> tuple[float, str]:
    """
    Estimate fatigue/rest impact.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str) where 1.0 = low fatigue risk
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    # Recent activity (YTD matches)
    # ytd_wins/losses may be None for ATP players (matchmx does not provide YTD).
    # Treat unknown load as neutral — do not penalize or reward.
    if player.ytd_wins is None and player.ytd_losses is None:
        return 0.80, "Normal match load"
    ytd = (player.ytd_wins or 0) + (player.ytd_losses or 0)
    
    if ytd <= 3:
        return 0.95, "Fresh, low match load"
    elif ytd <= 8:
        return 0.85, "Normal match load"
    elif ytd <= 15:
        return 0.75, "Moderate fatigue risk"
    else:
        return 0.60, "High fatigue risk"


def score_tournament_level(pick) -> tuple[float, str]:
    """
    Score favorability of tournament level for the pick.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    level = pick.tournament_level.upper()

    # Derive experience proxy from pick player's ranking (MatchPick has no tournament_exp attr)
    is_player_a = pick.pick_player == pick.player_a.short_name
    ranking = pick.player_a.ranking if is_player_a else pick.player_b.ranking
    if ranking <= 20:
        careerexp = 0.80
    elif ranking <= 50:
        careerexp = 0.65
    else:
        careerexp = 0.45

    if "GRAND" in level or "1000" in level or "ATP" in level and "250" not in level:
        if careerexp > 0.65:
            return 0.85, "Strong at high-level tournaments"
        else:
            return 0.65, "Limited high-level experience"
    elif "250" in level or "500" in level:
        return 0.75, "Familiar tournament level"
    else:
        return 0.70, "Secondary tournament level"


# ──────────────────────────────────────────────────────────────────────────────
# ADVANCED SERVE & RETURN RULES
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_serve_stability(pick) -> tuple[float, str]:
    """
    Comprehensive serve evaluation: first serve %, hold %, trend.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    serve_stats = player.serve_stats or {}
    _real_sources = ("tennis_abstract", "tennis_abstract_wta")

    if serve_stats.get("source") in _real_sources:
        first_serve = _get_serve_metric(serve_stats, "first_in_pct") \
                   or _get_serve_metric(serve_stats, "first_serve_in") \
                   or 0.60
        hold_pct = _get_serve_metric(serve_stats, "serve_win_pct") or 0.85
        
        # Stability score: high first serve + high hold = stable
        stability = (first_serve * 0.4 + hold_pct * 0.6)
        
        if stability >= 0.80:
            return 0.90, f"Excellent serve stability (FS {first_serve*100:.0f}%, Hold {hold_pct*100:.0f}%)"
        elif stability >= 0.70:
            return 0.75, f"Strong serve stability (FS {first_serve*100:.0f}%, Hold {hold_pct*100:.0f}%)"
        elif stability >= 0.60:
            return 0.65, f"Adequate serve stability (FS {first_serve*100:.0f}%, Hold {hold_pct*100:.0f}%)"
        else:
            return 0.50, f"Weak serve stability (FS {first_serve*100:.0f}%, Hold {hold_pct*100:.0f}%)"
    
    # Fallback: infer stability from recent form
    recent = player.recent_form or []
    if len(recent) >= 10:
        recent_wins = sum(1 for r in recent[-10:] if r == 'W')
        if recent_wins >= 8:
            return 0.80, "Strong recent trend (8+ wins in last 10)"
        elif recent_wins >= 6:
            return 0.70, "Solid recent trend (6-7 wins in last 10)"
        elif recent_wins >= 4:
            return 0.60, "Variable recent trend (4-5 wins in last 10)"
        else:
            return 0.50, "Weak recent trend (< 4 wins in last 10)"
    
    return 0.60, "Baseline serve stability"


def evaluate_return_pressure(pick) -> tuple[float, str]:
    """
    Return pressure evaluation: break points created, return won %, opponent difficulty.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
        opponent = pick.player_b
    else:
        player = pick.player_b
        opponent = pick.player_a
    
    # Opponent serve weakness = more pressure opportunities
    # WTA hard-court hold% avg ~0.65; ATP ~0.82 — use tour-appropriate default
    opp_serve_stats = opponent.serve_stats or {}
    tour = getattr(pick, "tour", "wta").lower()
    default_hold = 0.65 if tour == "wta" else 0.82
    opp_hold_pct = _get_serve_metric(opp_serve_stats, "hold_pct")
    if opp_hold_pct is None:
        opp_hold_pct = _get_serve_metric(opp_serve_stats, "hold_serve_pct")
    if opp_hold_pct is None:
        opp_hold_pct = default_hold
    opp_break_opportunity = 1.0 - opp_hold_pct
    
    # Player return effectiveness (from recent wins against tough opponents)
    recent = player.recent_form or []
    recent_wins = sum(1 for r in recent[-8:] if r == 'W') if recent else 0
    
    # Pressure score: opponent weakness + player consistency
    pressure_score = (opp_break_opportunity * 0.5 + (recent_wins / 8 if recent else 0.5) * 0.5)
    
    if pressure_score >= 0.65:
        return 0.80, f"High return pressure ({opp_break_opportunity*100:.0f}% break chances)"
    elif pressure_score >= 0.55:
        return 0.70, f"Moderate return pressure ({opp_break_opportunity*100:.0f}% break chances)"
    else:
        return 0.60, f"Limited return pressure ({opp_break_opportunity*100:.0f}% break chances)"


def evaluate_break_point_performance(pick) -> tuple[float, str]:
    """
    Break point conversion and save performance.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (score 0.0-1.0, reason_str)
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    serve_stats = player.serve_stats or {}

    # Break point stats — break_saved_pct present in WTA jsfrags data;
    # break_conv_pct rarely stored, fall back to tour average (0.42 WTA, 0.44 ATP)
    tour = getattr(pick, "tour", "wta").lower()
    default_conv  = 0.42 if tour == "wta" else 0.44
    default_saved = 0.60 if tour == "wta" else 0.65
    break_conv  = _get_serve_metric(serve_stats, "break_conv_pct")  or default_conv
    break_saved = _get_serve_metric(serve_stats, "break_saved_pct") or default_saved
    
    # Clutch score: higher conversion + higher saved = reliable
    clutch = (break_conv * 0.5 + break_saved * 0.5)
    
    if clutch >= 0.65:
        return 0.85, f"Excellent clutch play ({break_conv*100:.0f}% conv, {break_saved*100:.0f}% saved)"
    elif clutch >= 0.55:
        return 0.72, f"Good clutch play ({break_conv*100:.0f}% conv, {break_saved*100:.0f}% saved)"
    elif clutch >= 0.50:
        return 0.62, f"Average clutch play ({break_conv*100:.0f}% conv, {break_saved*100:.0f}% saved)"
    else:
        return 0.50, f"Weak clutch play ({break_conv*100:.0f}% conv, {break_saved*100:.0f}% saved)"


def evaluate_surface_sample_size(pick, surface: str) -> tuple[float, str]:
    """
    Check surface sample reliability.
    NOTE: confidence.py already applies -0.15 penalty for proxy serve stats.
    This function flags low samples but does NOT penalize (to avoid double-penalty).
    
    Args:
        pick: MatchPick object
        surface: "Hard", "Clay", or "Grass"
        
    Returns:
        (reliability_score 0.0-1.0, reason_str)
        Score is INFORMATIONAL ONLY — not included in rule_scores avg.
        Low sample should be flagged in risk_flags instead.
    """
    if pick.pick_player == pick.player_a.short_name:
        player = pick.player_a
    else:
        player = pick.player_b
    
    surf = surface.lower()
    wins = getattr(player, f"{surf}_wins", 0)
    losses = getattr(player, f"{surf}_losses", 0)
    total = wins + losses
    
    # Return reliability score (informational)
    if total >= 30:
        return 1.0, f"Strong sample on {surface} ({total} matches)"
    elif total >= 20:
        return 0.95, f"Good sample on {surface} ({total} matches)"
    elif total >= 10:
        return 0.85, f"Moderate sample on {surface} ({total} matches)"
    elif total >= 5:
        return 0.70, f"Thin sample on {surface} ({total} matches) — flagged in risk_flags"
    elif total > 0:
        return 0.50, f"Very thin sample on {surface} ({total} matches) — flagged in risk_flags"
    else:
        return 0.30, f"No record on {surface} — flagged in risk_flags"


def evaluate_model_consistency(pick) -> tuple[float, str]:
    """
    Sanity check: are edge and underlying stats aligned?
    High edge with weak stats = risk. Strong stats with low edge = opportunity.
    
    Args:
        pick: MatchPick object
        
    Returns:
        (consistency_score 0.0-1.0, reason_str)
    """
    # pick.edge_a/b are stored as percentages (e.g. 7.1 = 7.1%); convert to decimal
    edge_raw = pick.edge_a if pick.pick_player == pick.player_a.short_name else pick.edge_b
    edge = (edge_raw / 100.0) if edge_raw is not None else None
    prob = pick.prob_a if pick.pick_player == pick.player_a.short_name else pick.prob_b
    confidence = pick.confidence

    if edge is None or edge < 0.05:
        return 0.70, "Low edge — model uncertainty"

    # High edge should correlate with high model confidence and probability
    if edge >= 0.15:  # strong edge (≥15%)
        if confidence == "VERY HIGH" or prob >= 0.70:
            return 0.95, "Edge well-supported by model"
        elif confidence == "HIGH" or prob >= 0.60:
            return 0.80, "Edge moderately supported by model"
        else:
            return 0.60, "Edge high but model uncertain (sanity check)"
    elif edge >= 0.10:  # decent edge (≥10%)
        if confidence in ("HIGH", "VERY HIGH"):
            return 0.85, "Solid edge with good model conviction"
        elif confidence == "MEDIUM":
            return 0.75, "Decent edge, medium confidence"
        else:
            return 0.65, "Edge adequate but model hesitant"
    else:  # marginal edge (<10%)
        if confidence == "VERY HIGH":
            return 0.75, "Small edge but very high model conviction (watchlist signal)"
        else:
            return 0.70, "Small edge, low threshold for send"
    
    return 0.70, "Model edge-confidence check"


# ──────────────────────────────────────────────────────────────────────────────
# TEST FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────


def test_rules():
    """
    Test all rule evaluation functions with sample data.
    Run via: python -m tennis_model.evaluator.rules --test
    """
    from tennis_model.models import PlayerProfile, MatchPick
    
    print("\n" + "="*80)
    print("RULES.PY TEST SUITE")
    print("="*80)
    
    # Create sample players
    player_a = PlayerProfile(
        short_name="Djokovic",
        full_name="Novak Djokovic",
        ranking=1,
        hard_wins=120,
        hard_losses=20,
        clay_wins=100,
        clay_losses=15,
        grass_wins=50,
        grass_losses=10,
        ytd_wins=12,
        ytd_losses=2,
        recent_form=['W', 'W', 'W', 'L', 'W', 'W', 'W', 'W', 'W', 'W'],
        serve_stats={"source": "tennis_abstract", "serve_win_pct": 0.75, "first_serve_pct": 0.68, "hold_serve_pct": 0.92, "break_conv_pct": 0.35, "break_saved_pct": 0.88}
    )
    
    player_b = PlayerProfile(
        short_name="Zverev",
        full_name="Alexander Zverev",
        ranking=9,
        hard_wins=80,
        hard_losses=40,
        clay_wins=50,
        clay_losses=30,
        grass_wins=20,
        grass_losses=15,
        ytd_wins=8,
        ytd_losses=5,
        recent_form=['W', 'L', 'W', 'W', 'L', 'W', 'L', 'W', 'L', 'W'],
        serve_stats={"source": "tennis_abstract", "serve_win_pct": 0.68, "first_serve_pct": 0.64, "hold_serve_pct": 0.87, "break_conv_pct": 0.28, "break_saved_pct": 0.82}
    )
    
    # Create sample match pick
    pick = MatchPick(
        player_a=player_a,
        player_b=player_b,
        surface="Hard",
        tournament="Australian Open",
        tournament_level="Grand Slam",
        tour="ATP",
        prob_a=0.72,
        prob_b=0.28,
        edge_a=12.0,   # 12% edge (stored as percentage)
        edge_b=-12.0,
        confidence="HIGH",
        pick_player=player_a.short_name
    )
    
    # Test all rules
    print("\n1. Surface Fit")
    score, reason = evaluate_surface_fit(pick, "Hard")
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n2. Serve Consistency")
    score, reason = evaluate_serve_consistency(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n3. Serve Stability")
    score, reason = evaluate_serve_stability(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n4. Return Quality")
    score, reason = evaluate_return_quality(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n5. Return Pressure")
    score, reason = evaluate_return_pressure(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n6. Break Point Likelihood")
    score, reason = evaluate_break_point_likelihood(pick, "Hard")
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n7. Break Point Performance")
    score, reason = evaluate_break_point_performance(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n8. Fatigue Risk")
    score, reason = evaluate_fatigue_risk(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n9. Surface Sample Size")
    score, reason = evaluate_surface_sample_size(pick, "Hard")
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n10. Tournament Level")
    score, reason = score_tournament_level(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n11. Model Consistency")
    score, reason = evaluate_model_consistency(pick)
    print(f"   Score: {score:.2f}, Reason: {reason}")
    
    print("\n" + "="*80)
    print("RULES TEST COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test_rules()
    else:
        print("Run tests with: python -m tennis_model.evaluator.rules --test")
