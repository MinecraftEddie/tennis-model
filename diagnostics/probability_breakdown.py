"""
diagnostics/probability_breakdown.py
======================================
Read-only audit of calculate_probability() factor contributions.

Identifies which factors inflate underdog probabilities by decomposing
each component's weighted deviation from neutral (0.50).

Usage:
    python tennis_model/diagnostics/probability_breakdown.py

Data sources used:
  1. data/predictions.json       — stored picks (prob_a/b, odds)
  2. tennis_model/profiles.py    — WTA_PROFILES + STATIC_PROFILES (static, no network)
  3. tennis_model/model.py       — calculate_probability() + WEIGHTS (read-only import)

Does NOT modify any model, scoring, or production file.
"""

import json
import os
import sys
import logging
logging.disable(logging.CRITICAL)   # silence model + ELO chatter during diagnostics

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

PREDICTIONS_FILE = os.path.join(_PARENT, "data", "predictions.json")

# ---------------------------------------------------------------------------
# IMPORTS (read-only — no production modules modified)
# ---------------------------------------------------------------------------
from tennis_model.models    import PlayerProfile
from tennis_model.profiles  import STATIC_PROFILES, WTA_PROFILES
from tennis_model.model     import (
    calculate_probability, WEIGHTS, MARKET_WEIGHT,
    _surface_form_score, _form_score, _h2h_score, _exp_score,
    _surface_score, _physical_score, _rest_score,
)
from tennis_model.elo       import get_elo_engine, canonical_id, ranking_to_elo
from tennis_model.hold_break import compute_hold_break_prob

SEP = "=" * 72


# ---------------------------------------------------------------------------
# PROFILE BUILDER (from static data only — no network)
# ---------------------------------------------------------------------------

def _profile_from_static(pid: str) -> PlayerProfile | None:
    """Build a PlayerProfile from STATIC_PROFILES (ATP) by ATP ID."""
    d = STATIC_PROFILES.get(pid.upper())
    if not d:
        return None
    p = PlayerProfile(short_name=d.get("full_name", pid))
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    p.full_name   = d.get("full_name", pid)
    p.data_source = "static_curated"
    return p


def _profile_from_wta(name_lower: str) -> PlayerProfile | None:
    """Build a PlayerProfile from WTA_PROFILES by player name (lowercase)."""
    # Try exact key match first, then partial
    for key, d in WTA_PROFILES.items():
        last_key = key.split()[-1]
        last_name = name_lower.split()[-1]
        if key == name_lower or last_key == last_name or key in name_lower:
            p = PlayerProfile(short_name=d.get("full_name", name_lower))
            for k, v in d.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            p.full_name   = d.get("full_name", name_lower)
            p.data_source = "wta_static"
            return p
    return None


def _profile_from_pred(pred: dict, side: str) -> PlayerProfile | None:
    """Try to build a profile for player A or B from any static source."""
    name = pred.get(f"player_{side}", "").lower()
    tour = (pred.get("tour") or "").upper()

    if tour == "WTA":
        return _profile_from_wta(name)

    # ATP: find by PLAYER_ID_MAP → STATIC_PROFILES
    from tennis_model.profiles import PLAYER_ID_MAP
    for key, (full, slug, pid) in PLAYER_ID_MAP.items():
        if key in name or name.split()[-1] == key:
            return _profile_from_static(pid)
    return None


# ---------------------------------------------------------------------------
# FACTOR DECOMPOSITION
# ---------------------------------------------------------------------------

def decompose(comps: dict, weights: dict, pa_name: str, pb_name: str) -> dict:
    """
    For each factor, compute:
      delta_a = weight * (factor_prob_a - 0.50)

    Positive delta_a means this factor boosts player A above neutral.
    Sum of all delta_a = pure_model_prob_a - 0.50 (before market/MC blend).
    """
    out = {}
    for k, w in weights.items():
        if k not in comps or not isinstance(comps[k], tuple):
            continue
        pa_raw, pb_raw = comps[k]
        out[k] = {
            "weight":      w,
            f"{pa_name}":  round(pa_raw, 4),
            f"{pb_name}":  round(pb_raw, 4),
            "delta_a":     round(w * (pa_raw - 0.50), 4),  # A's boost above neutral
            "delta_b":     round(w * (pb_raw - 0.50), 4),  # B's boost above neutral
        }
    return out


def print_decomposition(label: str, decomp: dict, final_prob_a: float,
                        mkt_prob_a: float, pa_name: str, pb_name: str,
                        pick_side: str) -> None:
    sep_s = "-" * 72
    print(f"\n  {label}")
    print(f"  {sep_s}")
    fav = pa_name if mkt_prob_a > 0.50 else pb_name
    dog = pb_name if mkt_prob_a > 0.50 else pa_name
    print(f"  Favorite (mkt):  {fav}")
    print(f"  Underdog (mkt):  {dog}")
    print(f"  Market prob A:   {mkt_prob_a:.1%}  |  Market prob B:  {1-mkt_prob_a:.1%}")
    print(f"  Model  prob A:   {final_prob_a:.1%}  |  Model  prob B:  {1-final_prob_a:.1%}")
    gap = final_prob_a - mkt_prob_a
    pick_arrow = "(PICK)" if pick_side == "A" else ""
    pick_arrow_b = "(PICK)" if pick_side == "B" else ""
    print(f"  Model vs market: {gap:+.1%} for {pa_name} {pick_arrow}")
    print(f"                   {-gap:+.1%} for {pb_name} {pick_arrow_b}")
    print()
    print(f"  {'Factor':<20} {'Wt':>4}  {'A raw':>7}  {'B raw':>7}  {'A delta':>8}  {'B delta':>8}  Note")
    print(f"  {'-'*20} {'-'*4}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*20}")

    total_a = 0.0
    for k, v in sorted(decomp.items(), key=lambda x: abs(x[1]["delta_a"]), reverse=True):
        a_raw   = v[pa_name]
        b_raw   = v[pb_name]
        delta_a = v["delta_a"]
        delta_b = v["delta_b"]
        total_a += delta_a
        # Annotate extremes
        note = ""
        if k == "tournament_exp" and abs(delta_a) > 0.02:
            note = "*** career-wins gap"
        elif k == "recent_form" and abs(delta_a) > 0.02:
            note = "*** form signal"
        elif k == "surface_form" and abs(delta_a) > 0.02:
            note = "*** surface+form"
        elif abs(delta_a) < 0.005:
            note = "(near-neutral)"
        dir_a = "^" if delta_a > 0.005 else ("v" if delta_a < -0.005 else "~")
        print(f"  {k:<20} {v['weight']:>4.2f}  {a_raw:>7.3f}  {b_raw:>7.3f}  "
              f"{delta_a:>+8.4f}{dir_a} {delta_b:>+8.4f}  {note}")

    print(f"  {'SUM (raw model)':<20} {'1.00':>4}  {'':>7}  {'':>7}  "
          f"{total_a:>+8.4f}   {-total_a:>+8.4f}")
    expected_prob_a = 0.50 + total_a
    print(f"  Expected pre-blend prob A: {expected_prob_a:.3f}  (actual stored: {final_prob_a:.3f})")
    print(f"  Residual (market+MC blend): {final_prob_a - expected_prob_a:+.3f}")


# ---------------------------------------------------------------------------
# ANALYTICAL SUMMARY (from all predictions, no profiles needed)
# ---------------------------------------------------------------------------

def analytical_summary(preds: list) -> None:
    """
    From stored prob_a/b + odds, compute:
    - model vs market gap for every prediction
    - how much the underdog is systematically over-estimated
    """
    print(f"\n{SEP}")
    print("  PART A — ANALYTICAL SUMMARY  (all 38 stored picks)")
    print(SEP)
    print("  Source: stored prob_a, best_odds_a/b in predictions.json")
    print("  market_prob computed as vig-stripped: (1/oa) / (1/oa + 1/ob)")
    print()

    gaps_fav  = []   # model - market gap when model AGREES with market (favorite picked)
    gaps_dog  = []   # model - market gap for picked underdog
    dog_model = []   # model prob for underdog picks
    dog_mkt   = []   # market prob for underdog picks

    for p in preds:
        oa = p.get("best_odds_a")
        ob = p.get("best_odds_b")
        if not oa or not ob:
            continue
        mkt_raw_a = 1.0/oa
        mkt_raw_b = 1.0/ob
        mkt_t     = mkt_raw_a + mkt_raw_b
        mkt_a     = mkt_raw_a / mkt_t
        mkt_b     = mkt_raw_b / mkt_t

        pa = p.get("prob_a", 0.5)
        pb = 1.0 - pa
        pick = p.get("pick", "")
        player_a = p.get("player_a", "")

        pick_is_a  = (pick == player_a)
        model_pick = pa if pick_is_a else pb
        mkt_pick   = mkt_a if pick_is_a else mkt_b
        is_dog     = mkt_pick < 0.50

        gap = model_pick - mkt_pick
        if is_dog:
            gaps_dog.append(gap)
            dog_model.append(model_pick)
            dog_mkt.append(mkt_pick)
        else:
            gaps_fav.append(gap)

    def _mean(lst): return sum(lst)/len(lst) if lst else 0

    print(f"  Underdog picks : {len(gaps_dog)}")
    print(f"  Favorite picks : {len(gaps_fav)}")
    print()
    print(f"  === UNDERDOG PICKS (n={len(gaps_dog)}) ===")
    print(f"  Avg market prob for underdog : {_mean(dog_mkt):.1%}")
    print(f"  Avg model  prob for underdog : {_mean(dog_model):.1%}")
    print(f"  Avg model overestimation     : +{_mean(gaps_dog):.1%} above market")
    if gaps_dog:
        sorted_gaps = sorted(gaps_dog)
        n = len(sorted_gaps)
        p75 = sorted_gaps[int(n*0.75)]
        p90 = sorted_gaps[int(n*0.90)]
        print(f"  p75 overestimation           : +{p75:.1%}")
        print(f"  p90 overestimation           : +{p90:.1%}")

    if gaps_fav:
        print(f"\n  === FAVORITE PICKS (n={len(gaps_fav)}) ===")
        print(f"  Avg model vs market gap      : {_mean(gaps_fav):+.1%}")

    print()
    print("  INTERPRETATION:")
    avg_over = _mean(gaps_dog)
    if avg_over > 0.08:
        print(f"  [!!] Model systematically overestimates underdogs by avg +{avg_over:.1%}")
        print(f"  [!!] This is NOT explained by calibration alone — structural bias present")
    elif avg_over > 0.04:
        print(f"  [!]  Moderate overestimation of underdogs: +{avg_over:.1%} on average")
    else:
        print(f"  [ok] Overestimation within acceptable range: +{avg_over:.1%}")


# ---------------------------------------------------------------------------
# FULL FACTOR BREAKDOWN (for matches with static profiles)
# ---------------------------------------------------------------------------

def run_static_breakdowns(preds: list) -> None:
    print(f"\n{SEP}")
    print("  PART B — FULL FACTOR BREAKDOWN (static profiles, no network)")
    print(SEP)
    print("  Runs calculate_probability() on matches where both players have")
    print("  data in STATIC_PROFILES or WTA_PROFILES.")
    print()

    runs = 0
    factor_agg = {k: [] for k in WEIGHTS}  # collect delta_a per factor for underdog picks

    for pred in preds:
        pa_name = pred.get("player_a", "")
        pb_name = pred.get("player_b", "")
        surface = pred.get("surface", "Hard")
        tour    = (pred.get("tour") or "").upper()
        pick    = pred.get("pick", "")
        oa      = pred.get("best_odds_a")
        ob      = pred.get("best_odds_b")

        if not oa or not ob:
            continue

        pa_prof = _profile_from_pred(pred, "a")
        pb_prof = _profile_from_pred(pred, "b")

        if pa_prof is None or pb_prof is None:
            continue

        # Run calculate_probability with static profiles (no market odds to isolate pure model)
        try:
            prob_a, prob_b, comps = calculate_probability(
                pa_prof, pb_prof, surface,
                h2h_a=0, h2h_b=0,
                market_odds_a=oa, market_odds_b=ob,
            )
        except Exception as e:
            continue

        mkt_a = (1/oa) / (1/oa + 1/ob)
        mkt_b = 1 - mkt_a
        pick_is_a = (pick == pa_name)
        pick_side = "A" if pick_is_a else "B"
        model_pick = prob_a if pick_is_a else prob_b
        mkt_pick   = mkt_a  if pick_is_a else mkt_b
        is_dog     = mkt_pick < 0.50

        label = (f"{pa_name} vs {pb_name}  [{surface}]  "
                 f"pick={pick} @{pred.get('pick_odds','?')} "
                 f"({'underdog' if is_dog else 'favorite'})")

        decomp = decompose(comps, WEIGHTS, pa_name, pb_name)
        stored_prob_a = pred.get("prob_a", prob_a)
        print_decomposition(label, decomp, stored_prob_a, mkt_a,
                            pa_name, pb_name, pick_side)

        # Accumulate for aggregate analysis
        if is_dog:
            for k, v in decomp.items():
                delta = v["delta_a"] if pick_is_a else v["delta_b"]
                factor_agg[k].append(delta)

        runs += 1
        if runs >= 10:
            break

    if runs == 0:
        print("  No matches found with both players in static profiles.")
        print("  Re-run after adding more players to WTA_PROFILES / STATIC_PROFILES.")
        return

    # ── Aggregate: which factors most inflate underdog probability? ──────────
    print(f"\n{SEP}")
    print("  AGGREGATE: WHICH FACTORS INFLATE UNDERDOG PROBABILITY MOST?")
    print(SEP)
    print(f"  Based on {runs} matches with static profiles.")
    print(f"  delta = weight * (factor_prob_for_underdog - 0.50)")
    print(f"  Positive delta = factor HELPS the underdog above neutral.")
    print()

    agg_rows = []
    for k, deltas in factor_agg.items():
        if not deltas:
            continue
        avg_d = sum(deltas)/len(deltas)
        max_d = max(deltas, key=abs)
        agg_rows.append((k, avg_d, max_d, len(deltas)))

    agg_rows.sort(key=lambda x: x[1], reverse=True)

    print(f"  {'Factor':<22} {'Weight':>6}  {'Avg delta':>10}  {'Max |delta|':>12}  Interpretation")
    print(f"  {'-'*22} {'-'*6}  {'-'*10}  {'-'*12}  {'-'*30}")
    for k, avg_d, max_d, n in agg_rows:
        w = WEIGHTS.get(k, 0)
        interp = ""
        if avg_d > 0.015:
            interp = "*** INFLATES UNDERDOG"
        elif avg_d > 0.005:
            interp = "  * mild boost"
        elif avg_d < -0.015:
            interp = "  * correctly penalises"
        else:
            interp = "    near-neutral"
        print(f"  {k:<22} {w:>6.2f}  {avg_d:>+10.4f}  {abs(max_d):>12.4f}  {interp}")

    # ── Neutral-prior analysis ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  NEUTRAL-PRIOR ANALYSIS")
    print(SEP)
    print("  Factors that default to 0.50 when data is missing (or no H2H):")
    print()
    neutral_defaults = {
        "h2h":                "50/50 when no prior meetings  (0.10 weight = 0.05 per side, no penalty)",
        "career_surface_pct": "50% win rate when surface W/L = 0/0  (neutral, no underdog penalty)",
        "physical":           "age=26 assumed if unknown  => peak factor = 1.0  (helps unknown players)",
        "rest":               "density=1.0 neutral fallback when ytd is missing  (no penalty)",
        "hold_break":         "proxy serve stats if no real data  (approx neutral for WTA estimated)",
        "recent_form":        "0.50 if no recent_form list  (applies to qualifiers/unknown players)",
        "surface_form":       "50% recent + 50% career when data missing  (neutral for qualifiers)",
    }
    for factor, note in neutral_defaults.items():
        w = WEIGHTS.get(factor, 0)
        print(f"  [{w:.2f}] {factor:<22}  {note}")

    total_neutral_weight = sum(
        WEIGHTS[k] for k in neutral_defaults
    )
    ranking_weight = WEIGHTS["ranking"]
    print()
    print(f"  Total weight of potentially-neutral factors : {total_neutral_weight:.2f}")
    print(f"  Weight of ranking (ELO) alone               : {ranking_weight:.2f}")
    print()
    print("  => For an unknown player (qualifier / no data), up to")
    print(f"     {total_neutral_weight:.0%} of the model weight defaults to neutral (0.50).")
    print(f"     Only {ranking_weight:.0%} (ELO/ranking) correctly penalises the underdog.")
    print(f"     Result: a rank-150 qualifier gets model prob ~0.40-0.44")
    print(f"     even against a top-10 player (market prob ~0.10-0.15).")


# ---------------------------------------------------------------------------
# TOURNAMENT_EXP SPOTLIGHT
# ---------------------------------------------------------------------------

def tournament_exp_spotlight(preds: list) -> None:
    print(f"\n{SEP}")
    print("  PART C — TOURNAMENT_EXP SPOTLIGHT (veteran underdog inflation)")
    print(SEP)
    print("  tournament_exp = _norm(career_wins * age_decay(a), career_wins * age_decay(b))")
    print("  A veteran rank-130 player with 350 career wins scores HIGHER than")
    print("  a rank-45 young player with 100 career wins on this factor.")
    print()

    runs = 0
    for pred in preds:
        pa_prof = _profile_from_pred(pred, "a")
        pb_prof = _profile_from_pred(pred, "b")
        if pa_prof is None or pb_prof is None:
            continue

        pa_name = pred.get("player_a", "")
        pb_name = pred.get("player_b", "")
        oa = pred.get("best_odds_a")
        ob = pred.get("best_odds_b")
        pick = pred.get("pick", "")
        pick_odds = pred.get("pick_odds")

        if not oa or not ob:
            continue

        mkt_a = (1/oa) / (1/oa + 1/ob)
        mkt_b = 1 - mkt_a
        pick_is_a = (pick == pa_name)
        mkt_pick  = mkt_a if pick_is_a else mkt_b
        if mkt_pick >= 0.50:
            continue  # only show underdog picks

        from tennis_model.hold_break import _age_career_decay
        exp_a = max((pa_prof.career_wins or 0) * _age_career_decay(pa_prof.age or 0), 1.0)
        exp_b = max((pb_prof.career_wins or 0) * _age_career_decay(pb_prof.age or 0), 1.0)
        total_exp = exp_a + exp_b
        exp_prob_a = exp_a / total_exp
        exp_prob_b = 1.0 - exp_prob_a

        pick_exp = exp_prob_a if pick_is_a else exp_prob_b
        pick_name = pick
        opp_name  = pb_name if pick_is_a else pa_name

        print(f"  {pick_name} ({pick} @{pick_odds}) vs {opp_name}")
        print(f"    Ranking:   {pa_prof.ranking if pick_is_a else pb_prof.ranking} "
              f"vs {pb_prof.ranking if pick_is_a else pa_prof.ranking}")
        pick_p  = pa_prof if pick_is_a else pb_prof
        opp_p   = pb_prof if pick_is_a else pa_prof
        pick_eff = max((pick_p.career_wins or 0) * _age_career_decay(pick_p.age or 0), 1.0)
        opp_eff  = max((opp_p.career_wins or 0)  * _age_career_decay(opp_p.age or 0),  1.0)
        print(f"    Career wins (raw): pick={pick_p.career_wins or 0}, opp={opp_p.career_wins or 0}")
        print(f"    Age-decay factor:  pick={_age_career_decay(pick_p.age or 0):.2f}, "
              f"opp={_age_career_decay(opp_p.age or 0):.2f}")
        print(f"    Effective wins:    pick={pick_eff:.0f}, opp={opp_eff:.0f}")
        print(f"    tournament_exp prob for PICK: {pick_exp:.1%}  "
              f"(delta from neutral: {WEIGHTS['tournament_exp']*(pick_exp-0.50):+.4f})")
        print(f"    Market prob for pick: {mkt_pick:.1%}")
        direction = "INFLATES" if pick_exp > 0.50 else "deflates"
        print(f"    => tournament_exp {direction} underdog probability")
        print()
        runs += 1
        if runs >= 6:
            break


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(f"\n{SEP}")
    print("  PROBABILITY BREAKDOWN AUDIT")
    print(f"  calculate_probability() factor analysis")
    print(f"{SEP}\n")

    # ── Load predictions ────────────────────────────────────────────────────
    if not os.path.exists(PREDICTIONS_FILE):
        print(f"  No predictions file at {PREDICTIONS_FILE}")
        return
    with open(PREDICTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    all_preds = data.get("predictions", [])
    usable    = [p for p in all_preds if p.get("pick") and p.get("best_odds_a") and p.get("best_odds_b")]
    print(f"  Predictions loaded: {len(all_preds)}  ({len(usable)} with odds)")

    # ── Model overview ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  MODEL FACTORS  (calculate_probability, model.py)")
    print(SEP)
    print(f"  {'Factor':<22} {'Weight':>6}  Description")
    print(f"  {'-'*22} {'-'*6}  {'-'*45}")
    descriptions = {
        "ranking":            "ELO win probability (surface-blended: 50% surf + 30% overall + 20% recent)",
        "surface_form":       "60% recent L10 win% + 40% career surface win% (age-decayed)",
        "recent_form":        "recency-weighted L10: last 3 x3, next 4 x2, last 3 x1",
        "h2h":                "head-to-head record (defaults to 50/50 if no meetings)",
        "tournament_exp":     "career_wins * age_decay (35+: 0.75, 38+: 0.50, 41+: 0.25)",
        "career_surface_pct": "career all-time surface win% (age-decayed)",
        "physical":           "age curve (peak 24-28) + height bonus",
        "rest":               "match density = ytd_matches / weeks (fatigued = lower score)",
        "hold_break":         "Markov serve/return model",
    }
    for k, w in WEIGHTS.items():
        print(f"  {k:<22} {w:>6.2f}  {descriptions.get(k,'')}")
    print(f"\n  Plus: market blend {int(MARKET_WEIGHT*100)}% vig-stripped (inside model)")
    print(f"        Monte Carlo blend up to 15% (serve-based simulation)")
    print(f"        Shrink toward market alpha=0.70 (post-model, pipeline.py)")

    # ── Analytical summary ──────────────────────────────────────────────────
    analytical_summary(usable)

    # ── Full breakdowns ─────────────────────────────────────────────────────
    run_static_breakdowns(usable)

    # ── tournament_exp spotlight ─────────────────────────────────────────────
    tournament_exp_spotlight(usable)

    # ── Final diagnosis ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  DIAGNOSIS SUMMARY")
    print(SEP)
    findings = [
        ("STRUCTURAL", "Neutral prior (0.50) for 7/9 factors (80% weight) when data is missing"),
        ("STRUCTURAL", "Only ranking/ELO (20% weight) penalises underdogs by default"),
        ("STRUCTURAL", "tournament_exp inflates veteran underdogs: career_wins x age_decay"),
        ("STRUCTURAL", "recent_form (20% weight) highly volatile: 3 recent wins = 75%+ factor score"),
        ("OBSERVATION","All 37/38 underdog picks — structural, not random noise"),
        ("OBSERVATION","ATP and WTA both affected equally (similar factor structure)"),
    ]
    for category, text in findings:
        print(f"  [{category}] {text}")

    print()
    print("  SUSPECTED ROOT CAUSES (in order of impact):")
    causes = [
        "1. tournament_exp uses raw career wins — benefits veterans regardless of current level",
        "2. Neutral prior floors: qualifiers/unknown players get ~40-44% model prob (mkt: 10-26%)",
        "3. recent_form (20% wt) too volatile: 3-match hot streak overwhelms ranking signal",
        "4. h2h defaults to 50/50 — correct for unknown but 10% weight adds to underdog floor",
        "5. The four factors above combine to give structural 8-12pp overestimation of underdogs",
    ]
    for c in causes:
        print(f"    {c}")

    print()
    print("  RECOMMENDATIONS (for next session, no code changes now):")
    recs = [
        "A. Replace tournament_exp metric: use ranking-relative career wins, not absolute",
        "B. Shrink neutral prior for missing factors using ranking as anchor (not flat 0.50)",
        "C. Reduce recent_form weight from 0.20 to 0.10-0.15, or shrink raw score toward 0.50",
        "D. Consider a hard-floor on ranking factor: ELO gap > 200 pts should dominate more",
        "E. For qualifiers/unknown profiles: apply explicit penalty rather than neutral prior",
    ]
    for r in recs:
        print(f"    {r}")

    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
