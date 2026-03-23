import logging
import math
from datetime import date

from tennis_model.profiles import STATIC_PROFILES, WTA_PROFILES  # noqa: F401 (available for pipeline)
from tennis_model.elo import get_elo_engine, canonical_id
from tennis_model.hold_break import compute_hold_break_prob, _age_career_decay
from tennis_model.monte_carlo import run_simulation

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# MODEL WEIGHTS  (must sum to 1.0)
# ──────────────────────────────────────────────────────────────────────────────

WEIGHTS = {
    "ranking":            0.20,
    "surface_form":       0.20,
    "recent_form":        0.20,  # +0.05: serve stats already captured by hold_break; form signal more independent
    "h2h":                0.10,
    "tournament_exp":     0.10,
    "career_surface_pct": 0.05,
    "physical":           0.05,
    "rest":               0.05,
    "hold_break":         0.05,  # -0.05: serve stats also feed MC; halved to reduce double-counting
}

from tennis_model.config.runtime_config import ELO_SHRINK, MARKET_WEIGHT, MC_WEIGHT  # noqa: E402
# Note: 30% was too high — it deflated all computed edges by ~1.8% on a 6% vig ATP market,
# requiring ~8.8% true model edge to clear the 7% threshold. 15% preserves genuine signal
# while still anchoring against wildly mispriced profiles.

# ──────────────────────────────────────────────────────────────────────────────
# SCORING HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _norm(a: float, b: float) -> tuple[float, float]:
    t = a + b
    return (a/t, b/t) if t > 0 else (0.5, 0.5)


# _age_career_decay is defined in hold_break.py (canonical) and imported above.
# Applied to: career win%, career surface win%, derived proxy serve stats.
# Does NOT affect recent form or ranking factors.
#   35-37 → 0.75,  38-40 → 0.50,  41+ → 0.25,  <35 → 1.0

def _surface_form_score(pa, pb, surf, prior_a=0.5, prior_b=0.5):
    """Surface-adjusted recent form: 60% recent L10 win% + 40% career surface win%.
    Both components anchor toward the ELO/ranking prior rather than toward 0.50:
    - recent_pct: 70% signal + 30% ELO prior (damps extreme streaks).
    - surface_pct: alpha=min(n/20, 1.0) sample weight — small samples pull heavily
      toward ELO prior; ≥20 matches allow full signal. Age decay for 35+ is applied
      around the ELO prior (not around neutral 0.50)."""
    def f(pl, prior):
        decay = _age_career_decay(pl.age or 0)
        fm = pl.recent_form[-10:]
        raw_recent = fm.count("W") / len(fm) if fm else prior
        recent_pct = 0.70 * raw_recent + 0.30 * prior
        w = getattr(pl, f"{surf}_wins",   0)
        l = getattr(pl, f"{surf}_losses", 0)
        n = w + l
        raw_surface_pct = w / n if n > 0 else prior
        alpha = min(n / 20.0, 1.0)          # sample weight: n=0 → prior only; n≥20 → full signal
        surface_pct = prior + alpha * (raw_surface_pct - prior) * decay
        return 0.6 * recent_pct + 0.4 * surface_pct
    return _norm(f(pa, prior_a), f(pb, prior_b))


def _surface_score(pa, pb, surf, prior_a=0.5, prior_b=0.5):
    """Career all-time win% on this surface, age-decayed towards 0.50 for players 35+."""
    def p(pl, prior):
        decay = _age_career_decay(pl.age or 0)
        w = getattr(pl, f"{surf}_wins",   0)
        l = getattr(pl, f"{surf}_losses", 0)
        raw = w / (w + l) if (w + l) > 0 else prior
        return 0.50 + (raw - 0.50) * decay
    return _norm(p(pa, prior_a), p(pb, prior_b))

def _form_score(pa, pb, prior_a=0.5, prior_b=0.5):
    """Recent form with recency weighting: last 3 matches 3x, matches 4-7 2x, matches 8-10 1x."""
    def f(pl, prior):
        fm = pl.recent_form[-10:]
        if not fm:
            return prior
        total_w = weighted_wins = 0
        for i, result in enumerate(reversed(fm)):  # i=0 is most recent
            w = 3 if i < 3 else (2 if i < 7 else 1)
            total_w += w
            if result == "W":
                weighted_wins += w
        raw = weighted_wins / total_w
        return 0.70 * raw + 0.30 * prior  # inner shrink toward ELO prior (not 0.50)
    ra, rb = _norm(f(pa, prior_a), f(pb, prior_b))
    return 0.70 * ra + 0.30 * prior_a, 0.70 * rb + 0.30 * prior_b  # outer shrink toward ELO prior (beta=0.70)

def _h2h_score(aw, bw, prior_a=0.5, prior_b=0.5):
    return _norm(float(aw), float(bw)) if (aw+bw)>0 else (prior_a, prior_b)

def _exp_score(pa, pb):
    """Career experience, age-decayed then log-scaled.
    log1p compresses large career-win totals so a veteran with 400 wins
    does not get a 4x advantage over a player with 100 wins.
    400 wins → log1p(400)≈6.0, 100 wins → log1p(100)≈4.6  (1.3x ratio, not 4x)."""
    def effective(p):
        raw = max((p.career_wins or 0) * _age_career_decay(p.age or 0), 1.0)
        return math.log1p(raw)
    return _norm(effective(pa), effective(pb))

def _physical_score(pa, pb, prior_a=0.5, prior_b=0.5):
    """Physical suitability score.
    Age: piecewise curve — peak at 24-28, gentle decline to 33,
         steep 33-40, very steep 40+ (factor < 0.30 at age 40+).
    Height: minor linear bonus/penalty around 175cm baseline (±0.04 at ±12cm).
    Age is the primary multiplier; height is a small additive correction."""
    def s(p, prior):
        if p.age is None:
            return prior
        h_bonus = (p.height_cm - 175) / 300 if p.height_cm else 0.0
        a = p.age
        if a <= 28:
            age_factor = 1.0                                    # peak window
        elif a <= 33:
            age_factor = 1.0 - (a - 28) * 0.05                 # 0.95 → 0.75 over 5 yrs
        elif a <= 40:
            age_factor = 0.75 - (a - 33) * (0.45 / 7)          # 0.75 → 0.30 over 7 yrs
        else:
            age_factor = max(0.30 - (a - 40) * 0.05, 0.05)     # 0.25 → floor 0.05
        return 0.5 * age_factor + h_bonus
    return _norm(s(pa, prior_a), s(pb, prior_b))

def _rest_score(pa, pb, prior_a=0.5, prior_b=0.5):
    """Match density fatigue: ytd_matches / weeks_into_season.
    Higher density = more fatigued = lower rest score.
    Falls back to ranking-anchored prior when ytd data is missing (both None)."""
    weeks = max(date.today().isocalendar()[1], 1)
    def density(pl, prior):
        if pl.ytd_wins is None and pl.ytd_losses is None:
            return prior                         # no ytd data → use ranking prior
        ytd = (pl.ytd_wins or 0) + (pl.ytd_losses or 0)
        d   = ytd / weeks if ytd > 0 else 1.0   # 0 matches played → neutral density
        return 1.0 / (1.0 + d)                  # invert: higher density → lower score
    return _norm(density(pa, prior_a), density(pb, prior_b))

# ──────────────────────────────────────────────────────────────────────────────
# PROBABILITY MODEL
# ──────────────────────────────────────────────────────────────────────────────

def calculate_probability(pa, pb, surface, h2h_a, h2h_b,
                          market_odds_a=None, market_odds_b=None):
    s = surface.lower()
    _elo = get_elo_engine()
    _id_a = canonical_id(pa.full_name or pa.short_name)
    _id_b = canonical_id(pb.full_name or pb.short_name)
    log.info(f"ELO IDs — A: '{_id_a}'  B: '{_id_b}'")
    # Log inactivity days without calling _apply_decay() — decay is applied once
    # inside get_final_rating() via elo_win_probability(). Calling _apply_decay()
    # here too would double-decay inactive players' ratings.
    def _elo_days(pid: str) -> int:
        p = _elo.ratings.get(pid)
        if not p or p.matches_played == 0 or not p.last_updated:
            return 0
        try:
            return (date.today() - date.fromisoformat(p.last_updated)).days
        except ValueError:
            return 0
    days_a = _elo_days(_id_a)
    days_b = _elo_days(_id_b)
    if days_a > 90:
        log.info(f"ELO decay applied: {_id_a} inactive {days_a}d")
    elif days_a > 0:
        log.info(f"ELO no decay: {_id_a} last played {days_a}d ago (threshold 90d)")
    else:
        log.info(f"ELO no decay: {_id_a} — no prior match history (new/fresh rating)")
    if days_b > 90:
        log.info(f"ELO decay applied: {_id_b} inactive {days_b}d")
    elif days_b > 0:
        log.info(f"ELO no decay: {_id_b} last played {days_b}d ago (threshold 90d)")
    else:
        log.info(f"ELO no decay: {_id_b} — no prior match history (new/fresh rating)")
    elo_prob_a, elo_prob_b = _elo.elo_win_probability(
        _id_a, _id_b, surface, pa.ranking, pb.ranking
    )
    # Fallback: if neither player has recorded ELO history, both default to 1500/1500
    # and elo_prob collapses to 0.50/0.50, removing the main anti-underdog anchor.
    # In that case, use vig-stripped market implied probability as the prior anchor.
    if market_odds_a and market_odds_b:
        _pa_elo = _elo.ratings.get(_id_a)
        _pb_elo = _elo.ratings.get(_id_b)
        if ((_pa_elo is None or _pa_elo.matches_played == 0) and
                (_pb_elo is None or _pb_elo.matches_played == 0)):
            _mkt_raw_a = 1.0 / market_odds_a
            _mkt_raw_b = 1.0 / market_odds_b
            _mkt_sum   = _mkt_raw_a + _mkt_raw_b
            elo_prob_a = round(_mkt_raw_a / _mkt_sum, 4)
            elo_prob_b = round(_mkt_raw_b / _mkt_sum, 4)
            log.info(
                f"ELO no history for both players — market implied used as ranking prior: "
                f"{pa.short_name} {elo_prob_a:.1%}  {pb.short_name} {elo_prob_b:.1%}"
            )
    _hb = compute_hold_break_prob(pa, pb, surface)
    comps = {
        "ranking":            (elo_prob_a, elo_prob_b),
        "surface_form":       _surface_form_score(pa, pb, s, elo_prob_a, elo_prob_b),
        "recent_form":        _form_score(pa, pb, elo_prob_a, elo_prob_b),
        "h2h":                _h2h_score(h2h_a, h2h_b, elo_prob_a, elo_prob_b),
        "tournament_exp":     _exp_score(pa, pb),
        "career_surface_pct": _surface_score(pa, pb, s, elo_prob_a, elo_prob_b),
        "physical":           _physical_score(pa, pb, elo_prob_a, elo_prob_b),
        "rest":               _rest_score(pa, pb, elo_prob_a, elo_prob_b),
        "hold_break":         (_hb["prob_a"], _hb["prob_b"]),
    }
    sa = sum(WEIGHTS[k] * comps[k][0] for k in WEIGHTS)
    sb = sum(WEIGHTS[k] * comps[k][1] for k in WEIGHTS)
    prob_a, prob_b = _norm(sa, sb)
    log.info(f"Model → {pa.short_name} {prob_a:.1%} | {pb.short_name} {prob_b:.1%}")

    # ELO prior anchor: shrink pure model probability 20% toward ELO prior.
    # Prevents non-ranking factors from collectively drifting too far above the
    # ranking signal. prob_a + prob_b = 1 is preserved since elo_prob_a + elo_prob_b = 1.
    prob_a = ELO_SHRINK * prob_a + (1 - ELO_SHRINK) * elo_prob_a
    prob_b = ELO_SHRINK * prob_b + (1 - ELO_SHRINK) * elo_prob_b
    log.info(
        f"ELO anchor ({int((1-ELO_SHRINK)*100)}% prior): "
        f"{pa.short_name} {prob_a:.1%} | {pb.short_name} {prob_b:.1%}"
    )

    # Market anchoring: blend model prob with vig-stripped market implied prob.
    # Prevents unrealistic edges when model diverges far from consensus price.
    # Only applied when both market odds are present; pure model used otherwise.
    if market_odds_a and market_odds_b:
        mkt_raw_a  = 1.0 / market_odds_a
        mkt_raw_b  = 1.0 / market_odds_b
        mkt_total  = mkt_raw_a + mkt_raw_b          # strips vig
        mkt_prob_a = mkt_raw_a / mkt_total
        mkt_prob_b = mkt_raw_b / mkt_total
        prob_a = (1.0 - MARKET_WEIGHT) * prob_a + MARKET_WEIGHT * mkt_prob_a
        prob_b = (1.0 - MARKET_WEIGHT) * prob_b + MARKET_WEIGHT * mkt_prob_b
        log.info(
            f"Market blend ({int(MARKET_WEIGHT*100)}% market): "
            f"{pa.short_name} {prob_a:.1%} | {pb.short_name} {prob_b:.1%}  "
            f"[mkt implied {mkt_prob_a:.1%}/{mkt_prob_b:.1%}]"
        )

    # Monte Carlo blend: up to 15% simulation.
    # MC is purely serve-based; it diverges from the multi-factor analytical model
    # when non-serve factors (ELO, H2H, form) drive the result.  Large divergences
    # add noise rather than signal, so the MC weight is tapered when gap > 12%.
    # Examples of structural gaps:
    #   Alcaraz/Dimitrov Hard  — analytical 65%, MC ~53%  (16% gap, no inversion)
    #   Alcaraz/Ruud Clay      — analytical 60%, MC ~42%  (18% gap, inverted)
    #   Eala/Siegemund Hard    — analytical 52%, MC ~66%  (14% gap)
    pre_mc_prob_a = prob_a
    sim = run_simulation(pa, pb, surface, best_of=3, n_simulations=3000)
    mc_gap = abs(sim.win_prob_a - pre_mc_prob_a)
    _MC_GAP_THRESHOLD = 0.12
    if mc_gap > _MC_GAP_THRESHOLD:
        effective_weight = MC_WEIGHT * (_MC_GAP_THRESHOLD / mc_gap)
        log.warning(
            f"Large MC divergence ({mc_gap:.1%}): "
            f"analytical {pre_mc_prob_a:.1%} vs MC {sim.win_prob_a:.1%} "
            f"for {pa.short_name} vs {pb.short_name} [{surface}] — "
            f"tapering MC weight {MC_WEIGHT:.0%} → {effective_weight:.1%}"
        )
    else:
        effective_weight = MC_WEIGHT
    prob_a = (1 - effective_weight) * prob_a + effective_weight * sim.win_prob_a
    prob_b = 1.0 - prob_a
    log.info(
        f"MC blend ({int(MC_WEIGHT*100)}% sim): "
        f"{pa.short_name} {prob_a:.1%} | {pb.short_name} {prob_b:.1%}  "
        f"[sim {sim.win_prob_a:.1%}/{sim.win_prob_b:.1%}  "
        f"3-sets {sim.three_set_prob:.1%}  TB {sim.tiebreak_prob:.1%}]"
    )
    comps["monte_carlo"] = {
        "win_prob_a":     sim.win_prob_a,
        "win_prob_b":     sim.win_prob_b,
        "three_set_prob": sim.three_set_prob,
        "tiebreak_prob":  sim.tiebreak_prob,
        "volatility":     sim.volatility,
    }

    return prob_a, prob_b, comps


def fair_odds(p: float) -> float:
    return round(1.0/p, 2) if p > 0 else 999.0

def edge_pct(market: float, fair: float) -> float:
    return round(((market/fair)-1.0)*100, 1) if fair > 0 else 0.0
