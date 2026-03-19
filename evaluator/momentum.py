"""
Live match context and momentum handling.
Reweights confidence based on match state, set progression, and dominant performances.
"""


def compute_set_context(current_sets: list[tuple[int, int]]) -> dict:
    """
    Analyze completed sets and return context.
    
    Args:
        current_sets: list of (set_a_score, set_b_score) tuples
                       e.g., [(6, 4), (1, 6)]
    
    Returns:
        dict with keys:
        - sets_count: number of sets played
        - lead_player: 'a' or 'b' (who leads in set count)
        - lead_margin: absolute margin in sets
        - is_dominant_opener: bool, first set >4 game margin
        - set_scores: list of individual set scores
    """
    if not current_sets:
        return {
            "sets_count": 0,
            "lead_player": None,
            "lead_margin": 0,
            "is_dominant_opener": False,
            "set_scores": [],
        }
    
    sets_a = sum(1 for s_a, s_b in current_sets if s_a > s_b)
    sets_b = sum(1 for s_a, s_b in current_sets if s_b > s_a)
    
    is_dominant = False
    if len(current_sets) == 1:
        first_a, first_b = current_sets[0]
        is_dominant = abs(first_a - first_b) >= 5
    
    lead_player = 'a' if sets_a > sets_b else ('b' if sets_b > sets_a else None)
    
    return {
        "sets_count": len(current_sets),
        "lead_player": lead_player,
        "lead_margin": abs(sets_a - sets_b),
        "is_dominant_opener": is_dominant,
        "set_scores": current_sets,
    }


def reweight_confidence_for_live(
    base_confidence: float,
    alert_level: str,
    surface: str,
    set_context: dict,
    games_in_current_set: tuple[int, int] = (0, 0),
) -> float:
    """
    Adjust confidence for live match conditions.
    Prevents overreaction to early dominance, especially on clay.
    
    Args:
        base_confidence: original confidence (0.0-1.0)
        alert_level: "low", "medium", or "high"
        surface: "Hard", "Clay", "Grass"
        set_context: output from compute_set_context()
        games_in_current_set: (games_a, games_b) in current set
        
    Returns:
        adjusted confidence (0.0-1.0)
    """
    adjusted = base_confidence
    
    # Single dominant set (e.g., 6-1 clay) → reduce confidence
    if (set_context.get("sets_count", 0) == 1 and 
        set_context.get("is_dominant_opener", False) and
        surface.lower() == "clay"):
        # Don't panic — one dominant set on clay happens
        reduction = 0.20
        adjusted *= (1 - reduction)
    
    # Very early match (< 4 games total) — reduce alert confidence
    total_games = sum(games_in_current_set)
    if total_games < 4:
        reduction = 0.15
        adjusted *= (1 - reduction)
    
    # Player down 1 set but comeback momentum unlikely yet
    if set_context.get("sets_count", 0) == 1:
        first_a, first_b = set_context["set_scores"][0]
        # If leader won by >4 games, too early to assume easy recovery
        if abs(first_a - first_b) >= 5 and alert_level == "high":
            # Slight reduction until we see 2+ sets of adaptation
            reduction = 0.10
            adjusted *= (1 - reduction)
    
    return max(0.0, min(1.0, adjusted))


def compute_momentum_direction(
    set_context: dict,
    games_in_current_set: tuple[int, int],
    model_pick_player: str,
) -> str:
    """
    Detect momentum direction in live match.
    
    Args:
        set_context: from compute_set_context()
        games_in_current_set: (games_a, games_b)
        model_pick_player: 'a' or 'b', the player the model favors
        
    Returns:
        "positive", "negative", or "neutral"
    """
    if set_context.get("sets_count", 0) == 0:
        return "neutral"
    
    pick_letter = 'a' if model_pick_player else 'b'
    games_a, games_b = games_in_current_set
    
    # Picked player ahead in games in current set
    if pick_letter == 'a' and games_a > games_b:
        return "positive"
    elif pick_letter == 'b' and games_b > games_a:
        return "positive"
    
    # Picked player behind games in current set
    if pick_letter == 'a' and games_a < games_b - 1:
        return "negative"
    elif pick_letter == 'b' and games_b < games_a - 1:
        return "negative"
    
    return "neutral"


# ──────────────────────────────────────────────────────────────────────────────
# ADVANCED LIVE MOMENTUM METRICS
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# LIVE MATCH FUNCTIONS (requires real-time point-by-point data feed)
# These functions are prepared for future live match monitoring.
# They require detailed in-match data not currently available via static MatchPick.
# ──────────────────────────────────────────────────────────────────────────────


def analyze_serve_momentum(
    serve_stats_start: dict,
    serve_stats_current: dict,
) -> dict:
    """
    Detect changes in serve performance during match.
    
    REQUIRES: Live data feed with periodic serve stat snapshots.
    Not currently called — requires infrastructure to capture serve stats at:
      - match start
      - after each set
      - periodically during set
    
    Args:
        serve_stats_start: serve stats at match start (dict with keys:
                          "first_serve_pct", "hold_serve_pct")
        serve_stats_current: serve stats at current match point (same format)
        
    Returns:
        dict with keys:
        - first_serve_change: float (percentage points)
        - hold_pct_change: float (percentage points)  
        - direction: "improving" | "declining" | "stable"
        - magnitude: "sharp" | "moderate" | "small"
        
        OR None if no data provided
    """
    if not serve_stats_start or not serve_stats_current:
        return None
    
    fs_change = (serve_stats_current.get("first_serve_pct", 0.60) - 
                 serve_stats_start.get("first_serve_pct", 0.60))
    hold_change = (serve_stats_current.get("hold_serve_pct", 0.85) - 
                   serve_stats_start.get("hold_serve_pct", 0.85))
    
    avg_change = (fs_change + hold_change) / 2
    
    if avg_change > 0.05:
        direction = "improving"
    elif avg_change < -0.05:
        direction = "declining"
    else:
        direction = "stable"
    
    magnitude = "sharp" if abs(avg_change) > 0.10 else ("moderate" if abs(avg_change) > 0.05 else "small")
    
    return {
        "first_serve_change": round(fs_change, 3),
        "hold_pct_change": round(hold_change, 3),
        "direction": direction,
        "magnitude": magnitude,
    }


def analyze_rally_dynamics(
    match_stats: dict,
) -> dict:
    """
    Analyze rally length trends, winners vs errors.
    
    REQUIRES: Point-by-point match stats from live feed.
    Not currently called — requires live data source with:
      - winner/error counts per game
      - average rally lengths per game
    
    Args:
        match_stats: dict with optional keys:
        - winners_ratio_change: float (ratio of winners in current vs opening)
        - errors_ratio_change: float (ratio of errors in current vs opening)
        - avg_rally_length_current: str/float (trend in rally lengths)
        
    Returns:
        dict with rally analysis (keys: winner_trend, error_trend, rally_length_trend)
        OR None if no data provided
    """
    if not match_stats:
        return None
    
    winners_change = match_stats.get("winners_ratio_change", 0.0)
    errors_change = match_stats.get("errors_ratio_change", 0.0)
    rally_trend = match_stats.get("avg_rally_length_trend", "stable")
    
    # More winners = aggressive play
    winner_trend = "aggressive" if winners_change > 0.10 else ("passive" if winners_change < -0.10 else "neutral")
    
    # More errors = struggling
    error_trend = "struggling" if errors_change > 0.10 else ("solid" if errors_change < -0.10 else "neutral")
    
    return {
        "winner_trend": winner_trend,
        "error_trend": error_trend,
        "rally_length_trend": rally_trend,
    }


def compute_break_point_frequency(
    games_in_set: tuple[int, int],
    pick_player: str,
) -> dict:
    """
    Estimate break point frequency in current set.
    Closely contested sets generate more break chances.
    
    REQUIRES: Live in-match game scores.
    
    Args:
        games_in_set: (games_a, games_b) in current set
        pick_player: 'a' or 'b'
        
    Returns:
        dict with break frequency estimate (keys: frequency, score, total_games_played, expected_break_chances)
        OR None if empty games tuple
    """
    if not games_in_set or sum(games_in_set) == 0:
        return None
    
    games_a, games_b = games_in_set
    total_games = games_a + games_b
    deficit = abs(games_a - games_b)
    
    # Games getting tighter = more breaks likely
    if deficit == 0:
        frequency = "high"
        frequency_score = 0.85
    elif deficit == 1:
        frequency = "high"
        frequency_score = 0.80
    elif deficit == 2:
        frequency = "moderate"
        frequency_score = 0.75
    elif deficit >= 3:
        frequency = "low"
        frequency_score = 0.60
    else:
        frequency = "unknown"
        frequency_score = 0.50
    
    return {
        "frequency": frequency,
        "score": frequency_score,
        "total_games_played": total_games,
        "expected_break_chances": max(1, total_games // 3),
    }


def compute_game_length_trend(
    historical_game_lengths: list[int],
) -> dict:
    """
    Detect if games are getting longer or shorter (sign of increased competition).
    
    REQUIRES: Point counts for each game in the match (live detailed data).
    Not currently called — requires point-level tracking throughout match.
    
    Args:
        historical_game_lengths: list of point counts in recent games
                                (e.g., [8, 10, 7, 9, 11])
        
    Returns:
        dict with trend analysis (keys: trend, game_lengths_increasing, avg_length, recent_avg)
        OR None if insufficient data
    """
    if not historical_game_lengths or len(historical_game_lengths) < 2:
        return None
    
    early_avg = sum(historical_game_lengths[:len(historical_game_lengths)//2]) / max(1, len(historical_game_lengths)//2)
    recent_avg = sum(historical_game_lengths[len(historical_game_lengths)//2:]) / max(1, len(historical_game_lengths)//2)
    
    if recent_avg > early_avg * 1.15:
        trend = "lengthening"
        increasing = True
    elif recent_avg < early_avg * 0.85:
        trend = "shortening"
        increasing = False
    else:
        trend = "stable"
        increasing = False
    
    return {
        "trend": trend,
        "game_lengths_increasing": increasing,
        "avg_length": int(early_avg),
        "recent_avg": int(recent_avg),
    }


# ──────────────────────────────────────────────────────────────────────────────
# TEST FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────


def test_momentum():
    """
    Test momentum and set context functions.
    Run via: python -m tennis_model.evaluator.momentum --test
    """
    print("\n" + "="*80)
    print("MOMENTUM.PY TEST SUITE")
    print("="*80)
    
    # Test 1: Set context
    print("\n1. Compute Set Context (Djokovic ahead 1 set, dominant opener)")
    current_sets = [(6, 1), (3, 2)]
    context = compute_set_context(current_sets)
    print(f"   Sets: {context['set_scores']}")
    print(f"   Lead: Player {context['lead_player']} by {context['lead_margin']} set")
    print(f"   Dominant opener: {context['is_dominant_opener']}")
    
    # Test 2: Momentum direction
    print("\n2. Compute Momentum Direction (player_a ahead 4-2 in current set)")
    momentum = compute_momentum_direction(context, (4, 2), "player_a")
    print(f"   Direction: {momentum}")
    
    # Test 3: Reweight confidence for live clay match
    print("\n3. Reweight Confidence (clay, dominant opener 6-1)")
    base_conf = 0.80
    adjusted = reweight_confidence_for_live(base_conf, "high", "Clay", context, (3, 2))
    print(f"   Base confidence: {base_conf:.2f}")
    print(f"   Adjusted (clay +6-1): {adjusted:.2f}")
    print(f"   Reduction: {(base_conf - adjusted):.2f}")
    
    # Test 4: Orphaned live functions with no data return None
    print("\n4. Orphaned Live Functions (should return None)")
    result_serve = analyze_serve_momentum(None, None)
    result_rally = analyze_rally_dynamics(None)
    result_bp = compute_break_point_frequency((0, 0), "a")
    result_length = compute_game_length_trend([])
    
    print(f"   analyze_serve_momentum(): {result_serve}")
    print(f"   analyze_rally_dynamics(): {result_rally}")
    print(f"   compute_break_point_frequency(): {result_bp}")
    print(f"   compute_game_length_trend(): {result_length}")
    
    # Test 5: Break point frequency with data
    print("\n5. Break Point Frequency (tight 3-3 in current set)")
    bp_freq = compute_break_point_frequency((3, 3), "a")
    print(f"   Frequency: {bp_freq['frequency']} (score {bp_freq['score']:.2f})")
    print(f"   Expected breaks: {bp_freq['expected_break_chances']}")
    
    # Test 6: Game length trend
    print("\n6. Game Length Trend (games lengthening 8,9,7 → 10,11,12)")
    trend = compute_game_length_trend([8, 9, 7, 10, 11, 12])
    print(f"   Trend: {trend['trend']}")
    print(f"   Early avg: {trend['avg_length']}, Recent avg: {trend['recent_avg']}")
    print(f"   Growing: {trend['game_lengths_increasing']}")
    
    print("\n" + "="*80)
    print("MOMENTUM TEST COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test_momentum()
    else:
        print("Run tests with: python -m tennis_model.evaluator.momentum --test")
