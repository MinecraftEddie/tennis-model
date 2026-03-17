"""
tennis_model/monte_carlo.py
===========================
Monte Carlo match simulator.  Runs point-by-point simulations to produce
win probabilities, 3-set probability, tiebreak probability, and volatility.
Blended into calculate_probability() at 15% weight.
"""

import random
from dataclasses import dataclass
from tennis_model.hold_break import extract_stats, point_win_on_serve


@dataclass
class SimulationResult:
    win_prob_a:      float   # P(A wins match)
    win_prob_b:      float   # P(B wins match)
    three_set_prob:  float   # P(match goes to 3 sets) — bo3 only
    tiebreak_prob:   float   # P(at least one tiebreak)
    avg_sets:        float   # average sets played
    volatility:      float   # std dev of set scores — higher = more uncertain
    simulations:     int     # number of simulations run


def _simulate_game(p_server: float) -> str:
    """Simulate one service game. Returns 'server' or 'returner'."""
    server_points = 0
    returner_points = 0
    while True:
        if random.random() < p_server:
            server_points += 1
        else:
            returner_points += 1
        # Check win conditions
        if server_points >= 4 and server_points - returner_points >= 2:
            return "server"
        if returner_points >= 4 and returner_points - server_points >= 2:
            return "returner"


def _simulate_tiebreak(p_a: float) -> str:
    """Simulate tiebreak. Returns 'a' or 'b'."""
    pts_a = pts_b = 0
    while True:
        if random.random() < p_a:
            pts_a += 1
        else:
            pts_b += 1
        if pts_a >= 7 and pts_a - pts_b >= 2:
            return "a"
        if pts_b >= 7 and pts_b - pts_a >= 2:
            return "b"


def _simulate_set(p_serve_a: float, p_serve_b: float) -> tuple:
    """
    Simulate one set. Returns (games_a, games_b, had_tiebreak).
    p_serve_a = P(A wins a point on A's serve)
    p_serve_b = P(B wins a point on B's serve)
    """
    games_a = games_b = 0
    had_tiebreak = False
    a_serving = random.random() < 0.5  # random first server

    while True:
        if a_serving:
            winner = _simulate_game(p_serve_a)
            if winner == "server":
                games_a += 1
            else:
                games_b += 1
        else:
            winner = _simulate_game(p_serve_b)
            if winner == "server":
                games_b += 1
            else:
                games_a += 1

        a_serving = not a_serving

        # Normal set win
        if games_a >= 6 and games_a - games_b >= 2:
            return games_a, games_b, had_tiebreak
        if games_b >= 6 and games_b - games_a >= 2:
            return games_a, games_b, had_tiebreak

        # Tiebreak at 6-6
        if games_a == 6 and games_b == 6:
            had_tiebreak = True
            p_tb = (p_serve_a + (1 - p_serve_b)) / 2.0
            tb_winner = _simulate_tiebreak(p_tb)
            if tb_winner == "a":
                return 7, 6, had_tiebreak
            else:
                return 6, 7, had_tiebreak


def _simulate_match(p_serve_a: float, p_serve_b: float,
                    best_of: int = 3) -> dict:
    """Simulate one full match. Returns result dict."""
    sets_to_win = best_of // 2 + 1
    sets_a = sets_b = 0
    total_sets = 0
    had_tiebreak = False

    while sets_a < sets_to_win and sets_b < sets_to_win:
        ga, gb, tb = _simulate_set(p_serve_a, p_serve_b)
        if ga > gb:
            sets_a += 1
        else:
            sets_b += 1
        total_sets += 1
        if tb:
            had_tiebreak = True

    return {
        "winner":     "a" if sets_a > sets_b else "b",
        "sets_a":     sets_a,
        "sets_b":     sets_b,
        "total_sets": total_sets,
        "tiebreak":   had_tiebreak,
    }


def run_simulation(pa, pb, surface: str = "Hard",
                   best_of: int = 3,
                   n_simulations: int = 5000) -> SimulationResult:
    """
    Run Monte Carlo simulation for a match.
    Returns SimulationResult with win probs and match statistics.
    """
    stats_a = extract_stats(pa)
    stats_b = extract_stats(pb)

    # Surface adjustment on serve win probability
    surf_adj = {"hard": 0.0, "clay": -0.03, "grass": +0.03}
    adj = surf_adj.get(surface.lower(), 0.0)

    p_serve_a = min(0.85, max(0.35,
                   point_win_on_serve(stats_a) + adj))
    p_serve_b = min(0.85, max(0.35,
                   point_win_on_serve(stats_b) + adj))

    wins_a      = 0
    three_sets  = 0
    tiebreaks   = 0
    sets_played = []

    random.seed(42)  # reproducible results
    for _ in range(n_simulations):
        result = _simulate_match(p_serve_a, p_serve_b, best_of)
        if result["winner"] == "a":
            wins_a += 1
        if result["total_sets"] == 3 and best_of == 3:
            three_sets += 1
        if result["tiebreak"]:
            tiebreaks += 1
        sets_played.append(result["total_sets"])

    win_prob_a = round(wins_a / n_simulations, 4)

    # Volatility = std dev of sets played (higher = more uncertain)
    mean_sets = sum(sets_played) / len(sets_played)
    variance  = sum((s - mean_sets)**2 for s in sets_played) / len(sets_played)
    volatility = round(variance ** 0.5, 4)

    return SimulationResult(
        win_prob_a     = win_prob_a,
        win_prob_b     = round(1 - win_prob_a, 4),
        three_set_prob = round(three_sets / n_simulations, 4),
        tiebreak_prob  = round(tiebreaks / n_simulations, 4),
        avg_sets       = round(mean_sets, 3),
        volatility     = volatility,
        simulations    = n_simulations,
    )
