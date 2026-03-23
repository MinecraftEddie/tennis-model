"""
diagnostics/surface_form_fix_audit.py
======================================
Post-fix audit for the surface_form ELO-anchor recalibration.

Pre-fix  : _surface_form_score original (neutral 0.50 anchor, no sample guard)
Post-fix : _surface_form_score ELO-anchored, alpha=min(n/20,1.0) sample guard,
           recent_pct = 0.70*raw + 0.30*prior

Reuses the 35-case matrix from forward_audit_2026_03_21.py.
Monkey-patches tennis_model.model._surface_form_score for the pre-fix pass.

Run from parent directory of tennis_model/:
    python tennis_model/diagnostics/surface_form_fix_audit.py

Does NOT modify any production file.
"""

import logging, math, os, sys
logging.disable(logging.CRITICAL)

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

import tennis_model.model as _m
from tennis_model.hold_break import _age_career_decay
from tennis_model.model import calculate_probability, fair_odds, edge_pct
from tennis_model.probability_adjustments import shrink_toward_market
from tennis_model.diagnostics.forward_audit_2026_03_21 import (
    build_matrix, run_all,
)

SEP  = "=" * 72
SEP2 = "-" * 72


# ───────────────────────────────────────────────────────────────────
# OLD surface_form (pre-fix) — verbatim original
# ───────────────────────────────────────────────────────────────────

def _norm(a, b):
    t = a + b
    return (a / t, b / t) if t > 0 else (0.5, 0.5)


def _sf_OLD(pa, pb, surf, prior_a=0.5, prior_b=0.5):
    def f(pl, prior):
        decay = _age_career_decay(pl.age or 0)
        fm = pl.recent_form[-10:]
        recent_pct = fm.count("W") / len(fm) if fm else prior
        w = getattr(pl, f"{surf}_wins",   0)
        l = getattr(pl, f"{surf}_losses", 0)
        raw = w / (w + l) if (w + l) > 0 else prior
        surface_pct = 0.50 + (raw - 0.50) * decay
        return 0.6 * recent_pct + 0.4 * surface_pct
    return _norm(f(pa, prior_a), f(pb, prior_b))


_sf_NEW = _m._surface_form_score   # current (post-fix) function


# ───────────────────────────────────────────────────────────────────
# STATS HELPERS
# ───────────────────────────────────────────────────────────────────

def pctile(s, p):
    n = len(s)
    if n == 0: return 0.0
    k = (n - 1) * p / 100.0
    f = int(k); c = min(f + 1, n - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def dist(edges):
    if not edges:
        return {"n": 0}
    s = sorted(edges); n = len(s)
    return {
        "n": n, "mean": sum(s) / n,
        "p50": pctile(s, 50), "p75": pctile(s, 75),
        "p90": pctile(s, 90), "p95": pctile(s, 95),
        "p99": pctile(s, 99), "max": s[-1], "min": s[0],
        "gt5":  sum(1 for x in s if x > 0.05),
        "gt10": sum(1 for x in s if x > 0.10),
        "gt15": sum(1 for x in s if x > 0.15),
        "gt20": sum(1 for x in s if x > 0.20),
        "gt25": sum(1 for x in s if x > 0.25),
        "gt30": sum(1 for x in s if x > 0.30),
    }


def pct(a, b):
    return 0.0 if b == 0 else 100.0 * a / b


# ───────────────────────────────────────────────────────────────────
# RUN ONE PASS
# ───────────────────────────────────────────────────────────────────

def run_with_sf(sf_fn, matrix):
    _m._surface_form_score = sf_fn
    results = run_all(matrix)
    _m._surface_form_score = _sf_NEW   # always restore
    return results


def collect(results):
    valid = [r for r in results if r]
    picks = [r for r in valid if r["pick"]]

    all_e = []; pick_e = []; udog_e = []; fav_e = []; atp_e = []; wta_e = []
    udog_diff = []; fav_diff = []
    pick_udog = pick_fav = 0

    for r in valid:
        for side, edge, adj, mkt in [
            ("A", r["ea"], r["adj_a"], r["mkt_a"]),
            ("B", r["eb"], r["adj_b"], r["mkt_b"]),
        ]:
            all_e.append(edge)
            is_fav = (r["fav_is_a"] and side == "A") or \
                     (not r["fav_is_a"] and side == "B")
            (fav_e  if is_fav else udog_e).append(edge)
            (fav_diff if is_fav else udog_diff).append(adj - mkt)
            (atp_e if r["tour"] == "atp" else wta_e).append(edge)

    for r in picks:
        if r["ev_a"].is_value:
            is_dog = not r["fav_is_a"]
            pick_e.append(r["ea"])
        else:
            is_dog = r["fav_is_a"]
            pick_e.append(r["eb"])
        if is_dog: pick_udog += 1
        else:      pick_fav  += 1

    return {
        "n_valid": len(valid), "n_picks": len(picks),
        "all":  dist(all_e),  "picks": dist(pick_e),
        "udog": dist(udog_e), "fav":   dist(fav_e),
        "atp":  dist(atp_e),  "wta":   dist(wta_e),
        "pick_udog": pick_udog, "pick_fav": pick_fav,
        "udog_diff": sum(udog_diff) / len(udog_diff) if udog_diff else 0.0,
        "fav_diff":  sum(fav_diff)  / len(fav_diff)  if fav_diff  else 0.0,
        "raw": results,
    }


# ───────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────

def main():
    matrix = build_matrix()
    N = len(matrix)

    print(f"\n{SEP}")
    print("  SURFACE_FORM FIX AUDIT — post-fix calibration check")
    print(f"  Test matrix : {N} cases  (8 ATP, 22 WTA, 5 qualifier)")
    print(f"  Pre-fix     : neutral 0.50 anchor, no sample guard")
    print(f"  Post-fix    : ELO-anchored, alpha=min(n/20,1.0), recent 70/30 blend")
    print(f"{SEP}")

    pre  = collect(run_with_sf(_sf_OLD, matrix))
    post = collect(run_with_sf(_sf_NEW, matrix))

    # ── 1. TOTALS ─────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  1. TOTALS")
    print(SEP)
    print(f"  Cases run        : {N}")
    print(f"  Valid            : {pre['n_valid']} pre  /  {post['n_valid']} post")
    print(f"  Alertable picks  : {pre['n_picks']} pre  /  {post['n_picks']} post")
    print(f"  Edge samples     : {pre['all']['n']} (2 x {N})")

    # ── 2. EDGE DISTRIBUTION on picks ─────────────────────────────
    print(f"\n{SEP}")
    print("  2. EDGE DISTRIBUTION — alertable picks only")
    print(SEP)
    pd = pre["picks"]; nd = post["picks"]
    if pd.get("n", 0) > 0 and nd.get("n", 0) > 0:
        print(f"  {'Metric':<12}  {'Pre-fix':>10}  {'Post-fix':>10}  {'Delta':>8}  Dir")
        print(f"  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*8}  {'-'*8}")
        for k, label in [
            ("mean",  "Mean edge"),
            ("p50",   "Median"),
            ("p75",   "p75"),
            ("p90",   "p90"),
            ("p95",   "p95"),
            ("p99",   "p99"),
            ("max",   "Max edge"),
        ]:
            a = pd.get(k, 0); b = nd.get(k, 0); d = b - a
            arrow = "DOWN" if d < -0.005 else ("UP" if d > 0.005 else "~")
            print(f"  {label:<12}  {a*100:>9.1f}%  {b*100:>9.1f}%  {d*100:>+7.1f}%  {arrow}")

    # ── 3. THRESHOLD COUNTS on all samples ────────────────────────
    print(f"\n{SEP}")
    print(f"  3. THRESHOLD COUNTS — all edge samples ({pre['all']['n']} per pass)")
    print(SEP)
    pa = pre["all"]; na = post["all"]
    total = pa.get("n", 1)
    print(f"  {'Thresh':<8}  {'Pre cnt':>8}  {'Pre %':>7}  {'Post cnt':>9}  {'Post %':>8}  Change")
    print(f"  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*12}")
    for label, key in [(">5%","gt5"),(">10%","gt10"),(">15%","gt15"),
                       (">20%","gt20"),(">25%","gt25"),(">30%","gt30")]:
        pc = pa.get(key, 0); nc = na.get(key, 0)
        pp = pc / total * 100; np_ = nc / total * 100
        chg = "reduced" if nc < pc else ("same" if nc == pc else "increased")
        print(f"  {label:<8}  {pc:>8}  {pp:>6.1f}%  {nc:>9}  {np_:>7.1f}%  {chg}")

    # ── 4. UNDERDOG CONCENTRATION ─────────────────────────────────
    print(f"\n{SEP}")
    print("  4. UNDERDOG CONCENTRATION among alertable picks")
    print(SEP)
    pu_pre  = pre["pick_udog"];  tot_pre  = pre["n_picks"]
    pu_post = post["pick_udog"]; tot_post = post["n_picks"]
    print(f"  Pre-fix  : underdogs {pu_pre}/{tot_pre} = {pct(pu_pre,tot_pre):.0f}%  "
          f"favorites {pre['pick_fav']}/{tot_pre} = {pct(pre['pick_fav'],tot_pre):.0f}%")
    print(f"  Post-fix : underdogs {pu_post}/{tot_post} = {pct(pu_post,tot_post):.0f}%  "
          f"favorites {post['pick_fav']}/{tot_post} = {pct(post['pick_fav'],tot_post):.0f}%")
    d_udog_pct = pct(pu_post, tot_post) - pct(pu_pre, tot_pre)
    print(f"  Delta    : {d_udog_pct:+.0f} pp  ({'improved' if d_udog_pct < -3 else 'small'})")

    # ── 5. MODEL PROB OVERESTIMATION ──────────────────────────────
    print(f"\n{SEP}")
    print("  5. AVG (model_adj_prob - market_prob) — all samples")
    print(SEP)
    print(f"  UNDERDOG side:")
    print(f"    Pre-fix  : {pre['udog_diff']*100:+.2f}%")
    print(f"    Post-fix : {post['udog_diff']*100:+.2f}%")
    d_uo = (post["udog_diff"] - pre["udog_diff"]) * 100
    print(f"    Delta    : {d_uo:+.2f}pp  ({'less inflation' if d_uo < -0.5 else 'minimal'})")
    print(f"  FAVORITE side:")
    print(f"    Pre-fix  : {pre['fav_diff']*100:+.2f}%  "
          f"Post-fix: {post['fav_diff']*100:+.2f}%  "
          f"Delta: {(post['fav_diff']-pre['fav_diff'])*100:+.2f}pp")

    # ── 6. ATP / WTA SPLIT ────────────────────────────────────────
    print(f"\n{SEP}")
    print("  6. ATP / WTA EDGE SPLIT (all samples)")
    print(SEP)
    for label, pk, nk in [("ATP", pre["atp"], post["atp"]),
                           ("WTA", pre["wta"], post["wta"])]:
        if pk.get("n", 0) > 0:
            dm   = (nk.get("mean", 0) - pk.get("mean", 0)) * 100
            dp90 = (nk.get("p90",  0) - pk.get("p90",  0)) * 100
            print(f"  {label}  mean: {pk['mean']*100:+.1f}% -> {nk['mean']*100:+.1f}%  ({dm:+.1f}pp)  "
                  f"p90: {pk.get('p90',0)*100:+.1f}% -> {nk.get('p90',0)*100:+.1f}%  ({dp90:+.1f}pp)")

    # ── 7. LARGEST PROB SHIFTS ────────────────────────────────────
    print(f"\n{SEP}")
    print("  7. LARGEST PROBABILITY SHIFTS caused by surface_form change")
    print("     (underdog adj_prob: post - pre)")
    print(SEP)

    pre_map  = {r["label"]: r for r in pre["raw"]  if r}
    post_map = {r["label"]: r for r in post["raw"] if r}
    shifts = []
    for label in pre_map:
        if label not in post_map: continue
        pr = pre_map[label]; nr = post_map[label]
        if pr["fav_is_a"]:
            pre_u = pr["adj_b"]; post_u = nr["adj_b"]
            pre_e = pr["eb"];    post_e = nr["eb"]
            match = f"{pr['pb'].short_name} vs {pr['pa'].short_name}"
        else:
            pre_u = pr["adj_a"]; post_u = nr["adj_a"]
            pre_e = pr["ea"];    post_e = nr["ea"]
            match = f"{pr['pa'].short_name} vs {pr['pb'].short_name}"
        shifts.append((label, match, pre_u, post_u, post_u - pre_u,
                       pre_e, post_e, pr["surf"], pr["tour"].upper()))

    shifts.sort(key=lambda x: abs(x[4]), reverse=True)
    print(f"  {'ID':<8} {'Match':<30} {'Pre adj':>8} {'Post adj':>9} {'Delta':>7} {'Surf':<6} Tour")
    print(f"  {'-'*8} {'-'*30} {'-'*8} {'-'*9} {'-'*7} {'-'*6} {'-'*4}")
    for row in shifts[:12]:
        lab, nm, pre_a, post_a, delta, pre_e, post_e, surf, tour = row
        print(f"  {lab:<8} {nm[:30]:<30} {pre_a*100:>7.1f}%  {post_a*100:>8.1f}%  "
              f"{delta*100:>+6.1f}%  {surf:<6} {tour}")

    # ── 8. STORED PREDICTION BASELINE ────────────────────────────
    print(f"\n{SEP}")
    print("  8. STORED PREDICTION BASELINE (5 pre-fix WTA predictions)")
    print(SEP)
    print(f"  {'Match':<26} {'Model(dog)':>10} {'Market(dog)':>12} {'Overest':>8}")
    print(f"  {'-'*26} {'-'*10} {'-'*12} {'-'*8}")
    stored_over = []
    for row in [
        ("Linette vs Swiatek",    0.327, 0.095),
        ("Kessler vs Andreeva",   0.430, 0.216),
        ("Jacquemot vs Bouzkova", 0.445, 0.278),
        ("Boulter vs Tauson",     0.449, 0.351),
        ("Siegemund vs Eala",     0.453, 0.359),
    ]:
        nm, mdl, mkt = row
        diff = mdl - mkt
        stored_over.append(diff)
        print(f"  {nm:<26} {mdl*100:>9.1f}%  {mkt*100:>11.1f}%  {diff*100:>+7.1f}pp")
    avg_s = sum(stored_over) / len(stored_over)
    print(f"  {'Avg (5 stored picks)':<26} {'':>10} {'':>12} {avg_s*100:>+7.1f}pp")
    print(f"\n  Post-fix synthetic avg overest: {post['udog_diff']*100:+.2f}pp  "
          f"(pre-fix synthetic: {pre['udog_diff']*100:+.2f}pp)")

    # ── 9. VERDICT ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  9. VERDICT")
    print(SEP)

    pre_mean  = pre["picks"].get("mean", 0) or 0
    post_mean = post["picks"].get("mean", 0) or 0
    d_mean    = post_mean - pre_mean

    pre_over20  = pre["picks"].get("gt20", 0)
    post_over20 = post["picks"].get("gt20", 0)

    d_udog_o   = post["udog_diff"] - pre["udog_diff"]
    udog_pct_pre  = pct(pu_pre,  tot_pre)
    udog_pct_post = pct(pu_post, tot_post)

    print()
    print(f"  Mean edge on picks      : {pre_mean*100:.1f}%  ->  {post_mean*100:.1f}%  ({d_mean*100:+.1f}pp)")
    print(f"  Underdog overestimation : {pre['udog_diff']*100:+.1f}%  ->  "
          f"{post['udog_diff']*100:+.1f}%  ({d_udog_o*100:+.1f}pp)")
    print(f"  Picks >20% edge         : {pre_over20}  ->  {post_over20}")
    print(f"  Underdog pick rate      : {udog_pct_pre:.0f}%  ->  {udog_pct_post:.0f}%")
    print()

    material_edge    = d_mean < -0.02
    material_overest = d_udog_o < -0.02
    tail_improved    = post_over20 < pre_over20
    udog_reduced     = (udog_pct_post < udog_pct_pre - 5)
    any_improvement  = (d_mean < -0.005 or d_udog_o < -0.005 or tail_improved)

    if material_edge and material_overest:
        verdict = "surface_form fix materially improved calibration"
    elif any_improvement:
        verdict = "small improvement only"
    else:
        verdict = "no meaningful change"

    print(f"  VERDICT: \"{verdict}\"")
    print()

    if d_mean < -0.005:
        print(f"  [+] Mean pick edge: {abs(d_mean)*100:.1f}pp lower — model less overconfident")
    else:
        print(f"  [-] Mean pick edge change: {d_mean*100:+.1f}pp — limited direct effect on picks")

    if d_udog_o < -0.005:
        print(f"  [+] Underdog overestimation: {abs(d_udog_o)*100:.1f}pp reduction")
    else:
        print(f"  [-] Underdog overestimation change: {d_udog_o*100:+.1f}pp")

    if tail_improved:
        print(f"  [+] Tail >20%: {pre_over20} -> {post_over20} picks")
    else:
        print(f"  [~] Tail >20% unchanged: {post_over20} picks")

    if udog_reduced:
        print(f"  [+] Underdog pick rate fell {udog_pct_pre:.0f}% -> {udog_pct_post:.0f}%")
    else:
        print(f"  [~] Underdog pick rate: {udog_pct_pre:.0f}% -> {udog_pct_post:.0f}% (side selection intact)")

    print()
    print("  Architecture note: surface_form weight=0.20, one of 8 non-ranking")
    print("  factors. ELO carries only 20% of model weight — structural floor")
    print("  remains. Next candidates: recent_form anchor, tournament_exp.")
    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
