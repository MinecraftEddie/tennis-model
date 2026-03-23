"""
diagnostics/ranking_anchor_audit.py
=====================================
Audit the influence of the ranking/ELO factor vs all other factors.

Goal: measure whether WEIGHTS["ranking"]=0.20 is strong enough to anchor
the model against underdog inflation from the remaining 0.80 of factor weight.

Key metrics:
  - ELO prior vs pure model prob for every underdog in the 35-case matrix
  - Per-factor lift above ELO prior (delta_k = w_k * (factor_k_udog - elo_prior))
  - Cases where model gives >2x the ELO prior to the underdog
  - Whether non-ranking factors together dominate ranking on underdogs

Run from parent directory of tennis_model/:
    python tennis_model/diagnostics/ranking_anchor_audit.py

Does NOT modify any production file.
"""

import logging, math, os, sys
logging.disable(logging.CRITICAL)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

from tennis_model.model import calculate_probability, WEIGHTS
from tennis_model.diagnostics.forward_audit_2026_03_21 import build_matrix

SEP  = "=" * 72
SEP2 = "-" * 72

FACTOR_ORDER = [
    "ranking", "surface_form", "recent_form", "h2h",
    "tournament_exp", "career_surface_pct", "physical", "rest", "hold_break",
]


# ───────────────────────────────────────────────────────────────────
# PER-CASE ANALYSIS
# ───────────────────────────────────────────────────────────────────

def analyze_case(row):
    label, pa, pb, oa, ob, surf, h2a, h2b, tour, is_q = row

    try:
        prob_a, prob_b, comps = calculate_probability(
            pa, pb, surf, h2a, h2b,
            market_odds_a=oa, market_odds_b=ob,
        )
    except Exception as e:
        print(f"  [ERROR] {label}: {e}")
        return None

    # Underdog = higher market odds
    if abs(oa - ob) < 0.01:       # near 50/50 — skip
        return None
    if oa > ob:                   # A is the underdog
        udog_idx, fav_idx = 0, 1
        udog_name, fav_name = pa.short_name, pb.short_name
        udog_odds, fav_odds = oa, ob
        final_udog = prob_a
    else:                          # B is the underdog
        udog_idx, fav_idx = 1, 0
        udog_name, fav_name = pb.short_name, pa.short_name
        udog_odds, fav_odds = ob, oa
        final_udog = prob_b

    elo_prior = comps["ranking"][udog_idx]

    # Pure model prob: weighted sum of all factor probs for the underdog side.
    # Because each factor pair sums to 1 and WEIGHTS sum to 1,
    # pure_udog + pure_fav = 1 (already normalized — no _norm needed).
    pure_udog = sum(WEIGHTS[k] * comps[k][udog_idx] for k in WEIGHTS)

    # Market implied prob for underdog
    mkt_raw_udog = 1.0 / udog_odds
    mkt_raw_fav  = 1.0 / fav_odds
    mkt_udog = mkt_raw_udog / (mkt_raw_udog + mkt_raw_fav)

    # Per-factor raw probability for underdog side
    factor_probs = {k: comps[k][udog_idx] for k in WEIGHTS}

    # Per-factor lift above ELO prior:
    #   delta_k = w_k * (factor_k_udog - elo_prior)
    # Sum of all delta_k = pure_udog - elo_prior  (exact decomposition)
    deltas = {k: WEIGHTS[k] * (factor_probs[k] - elo_prior) for k in WEIGHTS}
    total_lift = pure_udog - elo_prior   # = sum(deltas.values())

    # Dominance: what fraction of the pure_udog comes from non-ranking factors?
    ranking_vote = WEIGHTS["ranking"] * elo_prior
    other_votes  = pure_udog - ranking_vote

    return {
        "label":        label,
        "udog_name":    udog_name,
        "fav_name":     fav_name,
        "tour":         tour.upper(),
        "surf":         surf,
        "udog_odds":    udog_odds,
        "fav_odds":     fav_odds,
        "elo_prior":    elo_prior,
        "pure_udog":    pure_udog,
        "final_udog":   final_udog,
        "mkt_udog":     mkt_udog,
        "total_lift":   total_lift,
        "deltas":       deltas,
        "factor_probs": factor_probs,
        "ranking_vote": ranking_vote,
        "other_votes":  other_votes,
    }


# ───────────────────────────────────────────────────────────────────
# STATS HELPERS
# ───────────────────────────────────────────────────────────────────

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def pctile(s, p):
    n = len(s)
    if n == 0: return 0.0
    k = (n - 1) * p / 100.0
    f = int(k); c = min(f + 1, n - 1)
    return s[f] + (k - f) * (s[c] - s[f])

def pct(a, b):
    return 0.0 if b == 0 else 100.0 * a / b


# ───────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────

def main():
    matrix = build_matrix()

    print(f"\n{SEP}")
    print("  RANKING ANCHOR AUDIT — ELO weight vs non-ranking factor dominance")
    print(f"  Test matrix  : {len(matrix)} cases  (8 ATP, 22 WTA, 5 qualifier)")
    print(f"  ranking weight : {WEIGHTS['ranking']:.0%}  |  "
          f"other factors : {1-WEIGHTS['ranking']:.0%}")
    print(f"{SEP}")

    print(f"\n  Running {len(matrix)} cases (includes MC simulation per case)...")
    results = []
    for row in matrix:
        r = analyze_case(row)
        if r is not None:
            results.append(r)

    N = len(results)
    print(f"  Analyzed: {N} underdog sides\n")

    # ── 1. FACTOR WEIGHTS ─────────────────────────────────────────
    print(f"{SEP}")
    print("  1. FACTOR WEIGHTS")
    print(SEP)
    print(f"  {'Factor':<20}  {'Weight':>8}  {'Anchored to ELO?'}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*20}")
    anchored = {
        "ranking": "YES — is the ELO prior",
        "surface_form": "YES — 70/30 blend toward prior",
        "recent_form": "YES — 70/30 blend toward prior (fixed)",
        "h2h": "YES — falls back to prior if no H2H",
        "tournament_exp": "NO  — raw log(career_wins), no prior anchor",
        "career_surface_pct": "YES — alpha sample guard → prior",
        "physical": "YES — falls back to prior if age=None",
        "rest": "YES — falls back to prior if no YTD",
        "hold_break": "NO  — pure serve/return Markov chain",
    }
    for k in FACTOR_ORDER:
        print(f"  {k:<20}  {WEIGHTS[k]:>7.0%}  {anchored.get(k,'')}")
    non_anchored_w = WEIGHTS["tournament_exp"] + WEIGHTS["hold_break"]
    print(f"\n  Non-ELO-anchored factor weight: {non_anchored_w:.0%} "
          f"(tournament_exp 10% + hold_break 5%)")

    # ── 2. ELO PRIOR vs PURE MODEL PROB — all underdogs ──────────
    print(f"\n{SEP}")
    print("  2. ELO PRIOR vs PURE MODEL PROB — all underdog sides")
    print(SEP)
    elo_priors  = [r["elo_prior"]  for r in results]
    pure_probs  = [r["pure_udog"]  for r in results]
    final_probs = [r["final_udog"] for r in results]
    lifts       = [r["total_lift"] for r in results]
    s_lift = sorted(lifts)

    print(f"  {'Metric':<22}  {'ELO prior':>10}  {'Pure model':>11}  {'Final (blended)':>16}")
    print(f"  {'-'*22}  {'-'*10}  {'-'*11}  {'-'*16}")
    for label, src in [("Mean", [elo_priors, pure_probs, final_probs]),
                        ("Median", None),
                        ("Min", None),
                        ("Max", None)]:
        if label == "Mean":
            a, b, c = mean(elo_priors), mean(pure_probs), mean(final_probs)
        elif label == "Median":
            a = pctile(sorted(elo_priors), 50)
            b = pctile(sorted(pure_probs), 50)
            c = pctile(sorted(final_probs), 50)
        elif label == "Min":
            a, b, c = min(elo_priors), min(pure_probs), min(final_probs)
        else:
            a, b, c = max(elo_priors), max(pure_probs), max(final_probs)
        print(f"  {label:<22}  {a*100:>9.1f}%  {b*100:>10.1f}%  {c*100:>15.1f}%")

    print()
    print(f"  Average lift (pure_model - elo_prior): {mean(lifts)*100:+.2f}pp")
    print(f"  p75 lift : {pctile(s_lift, 75)*100:+.1f}pp")
    print(f"  p90 lift : {pctile(s_lift, 90)*100:+.1f}pp")
    print(f"  Max lift : {max(lifts)*100:+.1f}pp")
    print(f"  Min lift : {min(lifts)*100:+.1f}pp")

    # Count underdogs where model is >2x elo prior
    over2x = [r for r in results if r["pure_udog"] > 2.0 * r["elo_prior"]]
    print(f"\n  Cases where pure_model > 2x elo_prior: {len(over2x)}/{N}")

    # ── 3. AVERAGE DELTA PER FACTOR ───────────────────────────────
    print(f"\n{SEP}")
    print("  3. AVERAGE DELTA PER FACTOR")
    print("     delta_k = w_k × (factor_k_udog_prob - elo_prior)")
    print("     Positive = pushes underdog above ELO; Negative = anchors down")
    print(SEP)
    print(f"  {'Factor':<20}  {'Weight':>6}  {'Avg factor prob':>15}  "
          f"{'Avg delta':>10}  {'Avg |prob-prior|':>17}  Direction")
    print(f"  {'-'*20}  {'-'*6}  {'-'*15}  {'-'*10}  {'-'*17}  {'-'*12}")

    factor_stats = {}
    for k in FACTOR_ORDER:
        fp_list = [r["factor_probs"][k] for r in results]
        d_list  = [r["deltas"][k]       for r in results]
        avg_fp  = mean(fp_list)
        avg_d   = mean(d_list)
        avg_dev = mean([abs(fp - r["elo_prior"]) for fp, r in zip(fp_list, results)])
        factor_stats[k] = {"avg_fp": avg_fp, "avg_d": avg_d, "avg_dev": avg_dev}
        direction = "LIFT^" if avg_d > 0.005 else ("anchor v" if avg_d < -0.005 else "neutral")
        print(f"  {k:<20}  {WEIGHTS[k]:>5.0%}  {avg_fp*100:>14.1f}%  "
              f"{avg_d*100:>+9.2f}pp  {avg_dev*100:>16.1f}pp  {direction}")

    total_avg_lift = mean(lifts)
    ranking_avg_d = factor_stats["ranking"]["avg_d"]
    other_avg_d   = total_avg_lift - ranking_avg_d  # = sum of all non-ranking deltas
    print(f"\n  Sum of non-ranking deltas   : {other_avg_d*100:>+.2f}pp (avg)")
    print(f"  Ranking delta               : {ranking_avg_d*100:>+.2f}pp (always 0 by definition)")
    print(f"  Net lift above ELO prior    : {total_avg_lift*100:>+.2f}pp")

    # ── 4. CASES WHERE MODEL > 2x ELO PRIOR ──────────────────────
    print(f"\n{SEP}")
    print("  4. CASES WHERE PURE MODEL PROB > 2x ELO PRIOR")
    print(SEP)
    if not over2x:
        print("  None found.")
    else:
        print(f"  {'ID':<8} {'Underdog':<22} {'ELO prior':>9} {'Pure model':>11} "
              f"{'Ratio':>7} {'Lift':>7} {'Surf':<6} Tour")
        print(f"  {'-'*8} {'-'*22} {'-'*9} {'-'*11} {'-'*7} {'-'*7} {'-'*6} {'-'*4}")
        for r in sorted(over2x, key=lambda x: x["pure_udog"]/x["elo_prior"], reverse=True):
            ratio = r["pure_udog"] / r["elo_prior"]
            print(f"  {r['label']:<8} {r['udog_name'][:22]:<22} "
                  f"{r['elo_prior']*100:>8.1f}%  {r['pure_udog']*100:>10.1f}%  "
                  f"{ratio:>6.2f}x  {r['total_lift']*100:>+6.1f}%  "
                  f"{r['surf']:<6} {r['tour']}")

    # ── 5. FACTOR CONTRIBUTIONS ON EXTREME CASES ─────────────────
    if over2x:
        print(f"\n{SEP}")
        print("  5. FACTOR CONTRIBUTIONS — cases with model > 2x ELO prior")
        print("     (+) = pushes above ELO prior  |  (-) = anchors down")
        print(SEP)
        for r in sorted(over2x, key=lambda x: x["pure_udog"]/x["elo_prior"], reverse=True):
            ratio = r["pure_udog"] / r["elo_prior"]
            print(f"\n  {r['label']}  {r['udog_name']} @ {r['udog_odds']:.2f}  "
                  f"[{r['tour']}/{r['surf']}]")
            print(f"    ELO prior: {r['elo_prior']*100:.1f}%  "
                  f"pure model: {r['pure_udog']*100:.1f}%  "
                  f"ratio: {ratio:.2f}x  lift: {r['total_lift']*100:+.1f}pp")
            print(f"    {'Factor':<20}  {'w':>5}  {'Factor prob':>12}  {'Delta':>8}")
            print(f"    {'-'*20}  {'-'*5}  {'-'*12}  {'-'*8}")
            # sort by absolute delta descending
            sorted_factors = sorted(FACTOR_ORDER,
                                    key=lambda k: abs(r["deltas"][k]), reverse=True)
            for k in sorted_factors:
                d  = r["deltas"][k]
                fp = r["factor_probs"][k]
                bar = "+" * int(abs(d) * 200) if d > 0 else "-" * int(abs(d) * 200)
                print(f"    {k:<20}  {WEIGHTS[k]:>4.0%}  {fp*100:>11.1f}%  "
                      f"{d*100:>+7.2f}pp  {bar}")
    else:
        print(f"\n{SEP}")
        print("  5. FACTOR CONTRIBUTIONS — no cases exceed 2x threshold")
        print(SEP)
        print("  Showing top-5 underdogs by total lift instead:")
        top5 = sorted(results, key=lambda x: x["total_lift"], reverse=True)[:5]
        for r in top5:
            print(f"\n  {r['label']}  {r['udog_name']}  elo={r['elo_prior']*100:.1f}%  "
                  f"model={r['pure_udog']*100:.1f}%  lift={r['total_lift']*100:+.1f}pp")
            for k in sorted(FACTOR_ORDER, key=lambda k: r["deltas"][k], reverse=True):
                d  = r["deltas"][k]
                if abs(d) > 0.002:
                    fp = r["factor_probs"][k]
                    print(f"    {k:<20}  {fp*100:>6.1f}%  delta {d*100:>+6.2f}pp")

    # ── 6. DOMINANCE CHECK ────────────────────────────────────────
    print(f"\n{SEP}")
    print("  6. DOMINANCE CHECK — do non-ranking factors outweigh ranking?")
    print(SEP)

    ranking_votes = [r["ranking_vote"] for r in results]
    other_votes_l = [r["other_votes"]  for r in results]
    dom_ratios    = [r["other_votes"] / max(r["ranking_vote"], 1e-6) for r in results]

    print(f"  Avg ranking vote  (w=20% × elo_prior) : {mean(ranking_votes)*100:.2f}%")
    print(f"  Avg other votes   (w=80% × factors)   : {mean(other_votes_l)*100:.2f}%")
    print(f"  Avg dominance ratio (other / ranking)  : {mean(dom_ratios):.2f}x")
    print(f"  If all factors equal prior: ratio would be exactly 4.00x (0.80/0.20)")
    print()

    # At neutral (all factors = elo_prior): ratio = 0.80*prior / 0.20*prior = 4.0
    # If others are higher than prior: ratio > 4.0 → they dominate more than expected
    avg_ratio = mean(dom_ratios)
    neutral_ratio = (1 - WEIGHTS["ranking"]) / WEIGHTS["ranking"]   # = 4.0
    excess = avg_ratio - neutral_ratio
    print(f"  Excess dominance (vs 4.00x neutral): {excess:+.2f}x")
    print()

    # Also: what % of underdogs have other_votes > 50% of pure_udog?
    other_dominant = sum(1 for r in results
                         if r["other_votes"] > 0.5 * r["pure_udog"])
    print(f"  Cases where other factors > 50% of pure_model: "
          f"{other_dominant}/{N} ({pct(other_dominant, N):.0f}%)")

    ranking_alone_would_pass = sum(
        1 for r in results if r["elo_prior"] > r["mkt_udog"]
    )
    model_pushes_over_mkt = sum(
        1 for r in results if r["pure_udog"] > r["mkt_udog"] >= r["elo_prior"]
    )
    model_lifts_from_elo = sum(
        1 for r in results
        if r["pure_udog"] > r["elo_prior"] + 0.02
    )
    print(f"  Underdogs where pure_model > market (ELO also above): "
          f"{ranking_alone_would_pass}")
    print(f"  Underdogs pushed above market BY non-ranking factors:  "
          f"{model_pushes_over_mkt}")
    print(f"  Underdogs lifted >2pp above ELO by non-ranking:       "
          f"{model_lifts_from_elo}/{N}")

    # ── 7. RECOMMENDATION ─────────────────────────────────────────
    print(f"\n{SEP}")
    print("  7. RECOMMENDATION")
    print(SEP)

    avg_lift = mean(lifts)
    max_lift = max(lifts)
    n_over2x = len(over2x)
    n_lifted_2pp = model_lifts_from_elo

    print()
    print(f"  Avg lift above ELO prior        : {avg_lift*100:+.2f}pp")
    print(f"  Max lift                        : {max_lift*100:+.2f}pp")
    print(f"  Cases >2x ELO prior             : {n_over2x}")
    print(f"  Cases lifted >2pp above ELO     : {n_lifted_2pp}/{N}")
    print(f"  Underdog overestimation (vs mkt): "
          f"+{mean([r['pure_udog']-r['mkt_udog'] for r in results])*100:.2f}pp (pure model)")
    print()

    # Decision logic
    if avg_lift > 0.04 or n_over2x >= 3:
        verdict = "ranking too weak"
        detail  = (f"avg lift {avg_lift*100:+.1f}pp, {n_over2x} cases >2x prior — "
                   f"non-ranking factors consistently overwhelm ELO anchor")
    elif avg_lift > 0.01 or n_over2x >= 1 or n_lifted_2pp > N // 3:
        verdict = "need final shrink toward prior"
        detail  = (f"avg lift {avg_lift*100:+.1f}pp — ranking weight is structurally "
                   f"limited (20% can only provide 20pp pull); a post-model shrink "
                   f"toward ELO prior would be more effective than raising the weight")
    else:
        verdict = "ranking weight ok"
        detail  = f"avg lift {avg_lift*100:+.1f}pp — non-ranking factors not dominating"

    print(f"  VERDICT: \"{verdict}\"")
    print(f"  {detail}")
    print()

    # Structural observation
    print(f"  Structural note:")
    print(f"    ranking weight = {WEIGHTS['ranking']:.0%} — maximum anchor pull is {WEIGHTS['ranking']*100:.0f}pp.")
    print(f"    Even if ELO prior is 0.10 (big underdog), ranking contributes only")
    print(f"    {WEIGHTS['ranking']:.0%} × 0.10 = {WEIGHTS['ranking']*0.10*100:.1f} raw points to the weighted sum.")
    print(f"    The remaining {1-WEIGHTS['ranking']:.0%} can push underdog prob much higher")
    print(f"    if those 8 factors systematically rate the underdog above their ELO.")
    print(f"    Unanchored factors (tournament_exp, hold_break) = {non_anchored_w:.0%} of weight.")
    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
