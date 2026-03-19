"""
tennis_model/hold_break.py
==========================
Converts service / return stats into hold%, set-win probability,
and match-win probability.  Integrated into calculate_probability()
as the "hold_break" component.
"""
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Plausible range for server's point-win probability (pre-surface-adjustment).
# Values outside this range indicate corrupted or proxy-only stats.
_PW_SERVE_MIN = 0.40
_PW_SERVE_MAX = 0.80


def _age_career_decay(age: int) -> float:
    if age >= 41: return 0.25
    if age >= 38: return 0.50
    if age >= 35: return 0.75
    return 1.0


# Surface multipliers applied to the server's raw point-win probability
SURFACE_SERVE_BOOST = {
    "grass":  0.04,
    "hard":   0.00,
    "clay":  -0.03,
}


@dataclass
class ServiceStats:
    hold_pct:         float = 0.65
    first_serve_in:   float = 0.62
    first_serve_won:  float = 0.72
    second_serve_won: float = 0.50
    break_pct:        float = 0.25
    source:           str   = "proxy"        # "real" (Tennis Abstract) or "proxy" (hard_pct heuristic)
    sample_type:      str   = ""             # e.g. "jsfrags_hard", "matchmx_career", "proxy_hard_pct"


def extract_stats(player, surface: str = "hard") -> ServiceStats:
    """
    Extract service stats from player profile.

    Priority:
      1. Real stats from player.serve_stats (scraped from Tennis Abstract matchmx).
         Uses surface-specific stats when n >= 5, falls back to career totals.
      2. Proxy: career hard-court win% → heuristic serve numbers.
         Less accurate; triggers a confidence penalty in confidence.py.
    """
    surf = surface.lower()
    serve = getattr(player, "serve_stats", {})

    _real_sources = ("tennis_abstract", "tennis_abstract_wta")
    if serve and serve.get("source") in _real_sources:
        # Prefer surface-specific stats when n >= 5; fall back to career otherwise
        surf_stats   = serve.get(surf)
        career_stats = serve.get("career")
        real = (surf_stats if surf_stats and surf_stats.get("n", 0) >= 5
                else career_stats or surf_stats)
        if real:
            s1in  = real["first_serve_in"]
            s1won = real["first_serve_won"]
            s2won = real["second_serve_won"]
            p_srv = s1in * s1won + (1 - s1in) * s2won
            if _PW_SERVE_MIN <= p_srv <= _PW_SERVE_MAX:
                return ServiceStats(
                    hold_pct         = round(p_srv, 4),
                    first_serve_in   = s1in,
                    first_serve_won  = s1won,
                    second_serve_won = s2won,
                    break_pct        = round(1.0 - p_srv, 4),
                    source           = "real",
                    sample_type      = real.get("sample_type", f"real_{surf}"),
                )
            log.warning(
                f"extract_stats: point_win_on_serve={p_srv:.3f} out of range "
                f"[{_PW_SERVE_MIN}, {_PW_SERVE_MAX}] for "
                f"{getattr(player, 'short_name', '?')} "
                f"(src={serve.get('source')}) — falling back to proxy"
            )
        else:
            log.warning(
                f"extract_stats: no usable real serve stats for "
                f"{getattr(player, 'short_name', '?')} "
                f"(src={serve.get('source')}) — falling back to proxy"
            )

    # Proxy fallback: derive from career hard-court win%
    hw = getattr(player, "hard_wins",   0)
    hl = getattr(player, "hard_losses", 0)
    total_hard = hw + hl
    raw_hard_pct = hw / total_hard if total_hard > 0 else 0.50
    # Age decay: shrink career hard% towards neutral (0.50) for players 35+
    decay    = _age_career_decay(getattr(player, "age", None) or 26)
    hard_pct = 0.50 + (raw_hard_pct - 0.50) * decay

    # Anchor: tour average hold ≈ 0.65; scale ±0.10 around it.
    hold_pct = 0.55 + hard_pct * 0.20   # range [0.55, 0.75]

    quality          = hard_pct - 0.50  # positive = above average
    first_serve_in   = round(0.62 + quality * 0.10, 4)
    first_serve_won  = round(0.72 + quality * 0.12, 4)
    second_serve_won = round(0.50 + quality * 0.08, 4)
    break_pct        = round(1.0 - hold_pct, 4)

    return ServiceStats(
        hold_pct         = round(hold_pct, 4),
        first_serve_in   = first_serve_in,
        first_serve_won  = first_serve_won,
        second_serve_won = second_serve_won,
        break_pct        = break_pct,
        source           = "proxy",
        sample_type      = "proxy_hard_pct",
    )


def point_win_on_serve(stats: ServiceStats) -> float:
    """P(server wins a rally point) = first_in*first_won + (1-first_in)*second_won."""
    return (stats.first_serve_in  * stats.first_serve_won
            + (1 - stats.first_serve_in) * stats.second_serve_won)


def hold_probability(server_stats: ServiceStats,
                     returner_stats: ServiceStats,
                     surface: str) -> float:
    """
    Exact game-win probability for the server using the standard
    tennis-game Markov formula.

    p = server's effective point-win probability (surface-adjusted).
    P(hold) = p^4*(1 + 4q + 10q^2) + 20*(pq)^3 * p^2/(p^2 + q^2)
    """
    p_serve  = point_win_on_serve(server_stats)
    p_return = point_win_on_serve(returner_stats)

    # Blend: server's own strength vs returner's effectiveness
    p_raw = 0.70 * p_serve + 0.30 * (1.0 - p_return)

    # Surface adjustment
    boost = SURFACE_SERVE_BOOST.get(surface.lower(), 0.0)
    p = min(max(p_raw + boost, 0.01), 0.99)
    q = 1.0 - p

    # Standard tennis game formula (exact, including deuce)
    pre_deuce  = p**4 * (1 + 4*q + 10*q**2)
    at_deuce   = 20 * (p * q)**3
    post_deuce = p**2 / (p**2 + q**2)

    return round(pre_deuce + at_deuce * post_deuce, 6)


def set_win_probability(hold_a: float, hold_b: float) -> float:
    """
    Markov-chain P(A wins a tiebreak set).
    hold_a = P(A wins a game on A's serve)
    hold_b = P(B wins a game on B's serve)  →  P(A breaks B) = 1 - hold_b
    Dynamic programming over game scores 0–7 × 0–7.
    """
    break_a = 1.0 - hold_b   # P(A wins a game on B's serve)

    memo: dict = {}

    def _p(ga: int, gb: int, a_serving: int) -> float:
        key = (ga, gb, a_serving)
        if key in memo:
            return memo[key]
        if ga == 7:
            return 1.0
        if gb == 7:
            return 0.0
        if ga == 6 and gb == 6:
            # Tiebreak: approximate as single game with averaged win prob
            val = (hold_a + break_a) / 2
            memo[key] = val
            return val
        p_game   = hold_a if a_serving else break_a
        next_srv = 1 - a_serving
        val = p_game * _p(ga+1, gb, next_srv) + (1-p_game) * _p(ga, gb+1, next_srv)
        memo[key] = val
        return val

    return round(_p(0, 0, 1), 6)   # A serves first


def match_win_probability(p_set: float, best_of: int = 3) -> float:
    """
    P(A wins match) given p_set = P(A wins a set).
    best_of=3 → first to 2 sets.
    best_of=5 → first to 3 sets.
    """
    p = p_set
    q = 1.0 - p
    if best_of == 5:
        return round(p**3 * (1 + 3*q + 6*q**2), 6)
    # default: best_of=3
    return round(p**2 * (1 + 2*q), 6)


def compute_hold_break_prob(pa, pb, surface: str,
                            best_of: int = 3) -> dict:
    """
    Full pipeline: extract stats → hold probs → set prob → match prob.
    Returns a dict including final (prob_a, prob_b).
    """
    stats_a = extract_stats(pa, surface)
    stats_b = extract_stats(pb, surface)

    hold_a = hold_probability(stats_a, stats_b, surface)
    hold_b = hold_probability(stats_b, stats_a, surface)

    p_set_a   = set_win_probability(hold_a, hold_b)
    p_match_a = match_win_probability(p_set_a, best_of)
    p_match_b = round(1.0 - p_match_a, 6)

    return {
        "stats_a":   stats_a,
        "stats_b":   stats_b,
        "hold_a":    hold_a,
        "hold_b":    hold_b,
        "p_set_a":   p_set_a,
        "p_match_a": p_match_a,
        "prob_a":    p_match_a,
        "prob_b":    p_match_b,
    }
