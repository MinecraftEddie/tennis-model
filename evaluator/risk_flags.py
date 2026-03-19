"""
Risk signal detection from MatchPick and match context.
Identifies red flags that reduce alert confidence.
"""


def detect_risk_flags(pick, match_context: dict = None) -> list[str]:
    """
    Detect risk flags from MatchPick and optional live context.
    
    Args:
        pick: MatchPick object from pipeline
        match_context: optional dict with keys like:
            - is_live: bool
            - current_sets: list of set scores (e.g., [6, 1])
            - games_in_current_set: tuple (games_a, games_b)
            
    Returns:
        list of risk flag strings
    """
    flags = []
    match_context = match_context or {}
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Data Quality Risks
    # ──────────────────────────────────────────────────────────────────────────────
    
    if not pick.validation_passed:
        flags.append("validation_failed")
    
    if pick.validation_warnings:
        flags.append("validation_warnings")
    
    # Bad data sources for either player
    bad_sources = ("unknown", "fallback")
    if pick.player_a.data_source in bad_sources or pick.player_b.data_source in bad_sources:
        flags.append("unreliable_data_source")

    # Estimated WTA profile (player not in WTA_PROFILES, built from jsfrags/defaults)
    if pick.player_a.data_source == "wta_estimated" or pick.player_b.data_source == "wta_estimated":
        flags.append("estimated_profile")
    
    # Missing serve stats (proxy serve stats are less reliable)
    _real_sources = ("tennis_abstract", "tennis_abstract_wta")
    for p in [pick.player_a, pick.player_b]:
        if not p.serve_stats or p.serve_stats.get("source") not in _real_sources:
            flags.append("incomplete_serve_stats")
            break

    # WTA clay/grass: surface-specific serve stats absent — career fallback is
    # hard-court biased (jsfrags recent-results are all hard in early season).
    # Source still reads "tennis_abstract_wta", so incomplete_serve_stats won't fire.
    _match_surf = pick.surface.lower()
    if _match_surf in ("clay", "grass"):
        for p in [pick.player_a, pick.player_b]:
            ss = p.serve_stats or {}
            if ss.get("source") == "tennis_abstract_wta":
                if ss.get(_match_surf, {}).get("n", 0) < 5:
                    flags.append("wta_serve_surface_mismatch")
                    break

    # WTA serve sample n < 8: average-of-averages bias is severe at this size
    for p in [pick.player_a, pick.player_b]:
        ss = p.serve_stats or {}
        if ss.get("source") == "tennis_abstract_wta":
            if 0 < ss.get("career", {}).get("n", 0) < 8:
                flags.append("wta_serve_sample_too_small")
                break
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Market / Odd Side Risks
    # ──────────────────────────────────────────────────────────────────────────────
    
    if not pick.market_odds_a or not pick.market_odds_b:
        flags.append("no_market_odds")
    
    # Edge looks too good to be true
    # pick.edge_a/b are stored as percentages (e.g. 14.3); convert to decimal for comparison
    edge_a = (pick.edge_a or 0.0) / 100.0
    edge_b = (pick.edge_b or 0.0) / 100.0
    max_edge = max(edge_a, edge_b)
    if max_edge > 0.40:
        flags.append("suspicious_edge_magnitude")

    # Market moving against model
    if pick.pick_player:
        if pick.pick_player == pick.player_a.short_name:
            edge = (pick.edge_a or 0.0) / 100.0
            model_prob = pick.prob_a
        else:
            edge = (pick.edge_b or 0.0) / 100.0
            model_prob = pick.prob_b

        if model_prob > 0.65 and edge < 0.05:
            flags.append("market_disagrees_with_high_conviction")
    
    # Stale odds
    if pick.odds_source == "manual":
        for w in pick.validation_warnings:
            if "hours old" in w or "STALE" in w:
                flags.append("stale_odds")
                break
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Live Match Risks
    # ──────────────────────────────────────────────────────────────────────────────
    
    is_live = match_context.get("is_live", False)
    if is_live:
        current_sets = match_context.get("current_sets", [])
        games_in_set = match_context.get("games_in_current_set", (0, 0))
        
        # Single dominant set on clay should not trigger auto-send
        if len(current_sets) == 1:
            set_a, set_b = current_sets[0]
            if pick.surface.lower() == "clay":
                if abs(set_a - set_b) >= 5:  # e.g., 6-1
                    flags.append("dominant_single_set_on_clay")
        
        # Extremely early match stages should be cautious
        total_games = sum(games_in_set)
        if total_games < 4:
            flags.append("very_early_match_stage")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Player Status Risks
    # ──────────────────────────────────────────────────────────────────────────────
    
    # Very thin surface stats
    surf = pick.surface.lower()
    for p in [pick.player_a, pick.player_b]:
        raw_n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        n = raw_n if raw_n <= 1500 else 0  # guard against parsing artifacts
        if n < 5:
            flags.append("very_thin_surface_sample")
            break
    
    # Inactive players — only flag when ytd data is known and confirmed 0.
    # ytd_wins/losses = None means not fetched (ATP API failure), not confirmed inactive.
    for p in [pick.player_a, pick.player_b]:
        if p.ytd_wins is not None and p.ytd_wins + p.ytd_losses == 0:
            flags.append("inactive_this_season")
            break
    
    return list(set(flags))  # deduplicate


# ──────────────────────────────────────────────────────────────────────────────
# MATCH CONTEXT RISKS
# ──────────────────────────────────────────────────────────────────────────────


def detect_match_context_risks(
    pick,
    days_inactive_a: int = 0,
    days_inactive_b: int = 0,
    prev_tournament_surface: str = "",
) -> list[str]:
    """
    Detect risks from match scheduling and context.
    Uses data already available from pipeline (no external sources).
    
    Args:
        pick: MatchPick object
        days_inactive_a: days since last match for player_a (from model)
        days_inactive_b: days since last match for player_b (from model)
        prev_tournament_surface: surface of previous tournament (from config comparison)
        
    Returns:
        list of risk flag strings
    """
    flags = []
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Back-to-back matches (days_inactive <= 1)
    # ──────────────────────────────────────────────────────────────────────────────
    # -1 means unknown (no ELO history) — skip check rather than false-positive
    if (days_inactive_a != -1 and days_inactive_a <= 1) or \
       (days_inactive_b != -1 and days_inactive_b <= 1):
        flags.append("back_to_back_matches")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Tournament surface change (e.g., hard → clay) = travel & adaptation risk
    # ──────────────────────────────────────────────────────────────────────────────
    current_surface = pick.surface.lower()
    prev_surface = prev_tournament_surface.lower() if prev_tournament_surface else ""
    
    if prev_surface and prev_surface != current_surface:
        flags.append("tournament_surface_change")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Very short rest (days_inactive < 0 is impossible, but =0 means same day)
    # ──────────────────────────────────────────────────────────────────────────────
    if days_inactive_a == 0 or days_inactive_b == 0:
        flags.append("same_day_matches")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # TODO: prev_match_duration not available from pipeline yet
    # When we add match duration tracking, flag: <2 days rest after 180+ min match
    # ──────────────────────────────────────────────────────────────────────────────
    
    return flags


def detect_model_sanity_risks(pick) -> list[str]:
    """
    Detect inconsistencies between edge and underlying stats support.
    
    Args:
        pick: MatchPick object
        
    Returns:
        list of risk flags
    """
    flags = []
    
    # pick.edge_a/b are stored as percentages (e.g. 14.3); convert to decimal for comparison
    edge = (pick.edge_a if pick.pick_player == pick.player_a.short_name else (pick.edge_b or 0.0)) / 100.0
    prob = pick.prob_a if pick.pick_player == pick.player_a.short_name else pick.prob_b
    confidence = pick.confidence

    if edge < 0.05:
        return []  # no edge = no sanity check needed
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Case 1: Market disagreement (high edge, uncertain prob, weak conviction)
    # Only flag when additional fragility evidence exists — thin surface sample,
    # missing real serve stats, or validation warnings.
    # "Clean" upset scenarios with good data are legitimate value bets; the market
    # being wrong about an underdog is exactly the opportunity we're looking for.
    # ──────────────────────────────────────────────────────────────────────────────
    if edge >= 0.15 and confidence != "VERY HIGH" and prob < 0.70:
        _real_serve_sources = ("tennis_abstract", "tennis_abstract_wta")
        surf = getattr(pick, "surface", "hard").lower()
        has_thin_sample = False
        for _p in [pick.player_a, pick.player_b]:
            _raw_n = getattr(_p, f"{surf}_wins", 0) + getattr(_p, f"{surf}_losses", 0)
            if (_raw_n if _raw_n <= 1500 else 0) < 10:  # guard against parsing artifacts
                has_thin_sample = True
                break
        has_missing_serves = any(
            (p.serve_stats or {}).get("source") not in _real_serve_sources
            for p in [pick.player_a, pick.player_b]
        )
        has_validation_warnings = bool(getattr(pick, "validation_warnings", []))
        if has_thin_sample or has_missing_serves or has_validation_warnings:
            flags.append("high_edge_misaligned_with_model")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Case 2: Model very confident but market doesn't agree (edge < 7%)
    # ──────────────────────────────────────────────────────────────────────────────
    if confidence == "VERY HIGH" and prob >= 0.70:
        if edge < 0.07:
            flags.append("model_confident_but_market_skeptical")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Case 3: Validation warnings with claimed edge (data quality mismatch)
    # ──────────────────────────────────────────────────────────────────────────────
    if pick.validation_warnings and edge >= 0.12:
        flags.append("edge_with_data_quality_warnings")
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Case 4: Very thin surface sample but strong edge (unreliable)
    # ──────────────────────────────────────────────────────────────────────────────
    for p in [pick.player_a, pick.player_b]:
        surf = pick.surface.lower()
        n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        if n < 3 and edge >= 0.10:
            flags.append("strong_edge_with_minimal_surface_sample")
            break
    
    # ──────────────────────────────────────────────────────────────────────────────
    # Case 5: Proxy serve stats with high conviction (unreliable serves)
    # ──────────────────────────────────────────────────────────────────────────────
    _real_sources = ("tennis_abstract", "tennis_abstract_wta")
    for p in [pick.player_a, pick.player_b]:
        serve_stats = p.serve_stats or {}
        if serve_stats.get("source") not in _real_sources and edge >= 0.15:
            flags.append("high_edge_no_tennis_abstract_serves")
            break
    
    return flags


# ──────────────────────────────────────────────────────────────────────────────
# TEST FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────


def test_risk_flags():
    """
    Test risk flag detection functions.
    Run via: python -m tennis_model.evaluator.risk_flags --test
    """
    from tennis_model.pipeline import PlayerProfile, MatchPick
    
    print("\n" + "="*80)
    print("RISK_FLAGS.PY TEST SUITE")
    print("="*80)
    
    # Create sample players
    player_a = PlayerProfile(
        short_name="Djokovic",
        data_source="static_curated",
        hard_wins=120,
        hard_losses=20,
        ytd_wins=12,
        ytd_losses=2,
        serve_stats={"source": "tennis_abstract", "serve_win_pct": 0.75}
    )
    
    player_b = PlayerProfile(
        short_name="Zverev",
        data_source="atp_api",
        hard_wins=80,
        hard_losses=40,
        ytd_wins=8,
        ytd_losses=5,
        serve_stats={"source": "tennis_abstract", "serve_win_pct": 0.68}
    )
    
    # Test 1: Basic risk flags with good data
    print("\n1. Basic Risk Flags (good data, no warnings)")
    pick_good = MatchPick(
        player_a=player_a,
        player_b=player_b,
        surface="Hard",
        tournament="Australian Open",
        validation_passed=True,
        validation_warnings=[],
        market_odds_a=1.52,
        market_odds_b=2.50,
        edge_a=10.0,   # 10% edge (stored as percentage)
        confidence="HIGH"
    )
    flags = detect_risk_flags(pick_good)
    print(f"   Flags: {flags if flags else 'None'}")
    
    # Test 2: Risk flags with validation warning
    print("\n2. With Validation Warning")
    pick_warn = MatchPick(
        player_a=player_a,
        player_b=player_b,
        surface="Hard",
        tournament="Australian Open",
        validation_passed=True,
        validation_warnings=["Thin clay sample (8 matches)"],
        market_odds_a=1.52,
        market_odds_b=2.50,
        edge_a=10.0,   # 10% edge (stored as percentage)
        confidence="HIGH"
    )
    flags = detect_risk_flags(pick_warn)
    print(f"   Flags: {flags}")
    
    # Test 3: Match context risks (back-to-back)
    print("\n3. Match Context Risks (back-to-back, surface change)")
    context_risks = detect_match_context_risks(pick_good, days_inactive_a=1, days_inactive_b=2, prev_tournament_surface="Clay")
    print(f"   Flags: {context_risks}")
    
    # Test 4: Model sanity risks (high edge, low confidence)
    print("\n4. Model Sanity Risks (edge=15%, confidence=MEDIUM)")
    pick_sanity = MatchPick(
        player_a=player_a,
        player_b=player_b,
        surface="Hard",
        tournament="Australian Open",
        validation_passed=True,
        validation_warnings=[],
        market_odds_a=1.52,
        market_odds_b=2.50,
        edge_a=15.0,   # 15% edge (stored as percentage)
        prob_a=0.65,
        confidence="MEDIUM",
        pick_player=player_a.short_name
    )
    sanity_flags = detect_model_sanity_risks(pick_sanity)
    print(f"   Flags: {sanity_flags if sanity_flags else 'None'}")
    
    # Test 5: Live match with dominant clay set
    print("\n5. Live Match (dominant 6-1 on clay)")
    live_context = {
        "is_live": True,
        "current_sets": [(6, 1)],
        "games_in_current_set": (2, 1)
    }
    pick_live = MatchPick(
        player_a=player_a,
        player_b=player_b,
        surface="Clay",
        tournament="Roland Garros",
        validation_passed=True,
        validation_warnings=[],
        market_odds_a=1.52,
        market_odds_b=2.50,
        edge_a=10.0,   # 10% edge (stored as percentage)
        confidence="HIGH"
    )
    flags_live = detect_risk_flags(pick_live, live_context)
    print(f"   Flags: {flags_live}")
    
    print("\n" + "="*80)
    print("RISK_FLAGS TEST COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test_risk_flags()
    else:
        print("Run tests with: python -m tennis_model.evaluator.risk_flags --test")
