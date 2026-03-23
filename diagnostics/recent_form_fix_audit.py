"""
diagnostics/recent_form_fix_audit.py
=====================================
Post-fix audit for the recent_form ELO-anchor recalibration.

Pre-fix  : _form_score inner+outer shrink toward neutral 0.50
           return 0.70 * raw + 0.30 * 0.50  (inner)
           return 0.70 * ra + 0.30 * 0.50, 0.70 * rb + 0.30 * 0.50  (outer)

Post-fix : _form_score inner+outer shrink toward ELO prior
           return 0.70 * raw + 0.30 * prior  (inner)
           return 0.70 * ra + 0.30 * prior_a, 0.70 * rb + 0.30 * prior_b  (outer)

Reuses the 35-case matrix from forward_audit_2026_03_21.py.
Monkey-patches tennis_model.model._form_score for the pre-fix pass.

Run from parent directory of tennis_model/:
    python tennis_model/diagnostics/recent_form_fix_audit.py

Does NOT modify any production file.
"""

import logging, math, os, sys
logging.disable(logging.CRITICAL)

# Windows cp1252 fix — force utf-8 output so box-drawing / arrow chars render
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

import tennis_model.model as _m
from tennis_model.model import fair_odds, edge_pct
from tennis_model.diagnostics.forward_audit_2026_03_21 import build_matrix, run_all

SEP  = "=" * 72
SEP2 = "-" * 72


# ───────────────────────────────────────────────────────────────────
# OLD _form_score (pre-fix) — verbatim original, shrinks to 0.50
# ───────────────────────────────────────────────────────────────────

def _norm(a, b):
    t = a + b
    return (a / t, b / t) if t > 0 else (0.5, 0.5)


def _form_OLD(pa, pb, prior_a=0.5, prior_b=0.5):
    """Original: inner + outer both shrink toward neutral 0.50."""
    def f(pl, prior):
        fm = pl.recent_form[-10:]
        if not fm:
            return prior
        total_w = weighted_wins = 0
        for i, result in enumerate(reversed(fm)):
            w = 3 if i < 3 else (2 if i < 7 else 1)
            total_w += w
            if result == "W":
                weighted_wins += w
        raw = weighted_wins / total_w
        return 0.70 * raw + 0.30 * 0.50
    ra, rb = _norm(f(pa, prior_a), f(pb, prior_b))
    return 0.70 * ra + 0.30 * 0.50, 0.70 * rb + 0.30 * 0.50


_form_NEW = _m._form_score   # current (post-fix) function


# ───────────────────────────────────────────────────────────────────
# STATS HELPERS
# ───────────────────────────────────────────────────────────────────

def pctile(s, p):
    n = len(s)
    if n == 0:
        return 0.0
    k = (n - 1) * p / 100.0
    f = int(k); c = min(f + 1, n - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def dist(edges):
    if not edges:
        return {"n": 0}
    s = sorted(edges); n = len(s)
    return {
        "n":    n,
        "mean": sum(s) / n,
        "p50":  pctile(s, 50), "p75": pctile(s, 75),
        "p90":  pctile(s, 90), "p95": pctile(s, 95),
        "p99":  pctile(s, 99), "max": s[-1], "min": s[0],
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

def run_with_form(form_fn, matrix):
    _m._form_score = form_fn
    results = run_all(matrix)
    _m._form_score = _form_NEW   # always restore
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
    print("  RECENT_FORM FIX AUDIT — post-fix calibration check")
    print(f"  Test matrix : {N} cases  (8 ATP, 22 WTA, 5 qualifier)")
    print(f"  Pre-fix     : inner + outer shrink toward neutral 0.50")
    print(f"  Post-fix    : inner + outer shrink toward ELO prior (beta=0.70)")
    print(f"{SEP}")

    pre  = collect(run_with_form(_form_OLD, matrix))
    post = collect(run_with_form(_form_NEW, matrix))

    # ── 1. TOTALS ─────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  1. TOTALS")
    print(SEP)
    print(f"  Cases analyzed   : {N}")
    print(f"  Valid            : {pre['n_valid']} pre  /  {post['n_valid']} post")
    print(f"  Alertable picks  : {pre['n_picks']} pre  /  {post['n_picks']} post")
    print(f"  Edge samples     : {pre['all']['n']} (2 × {N})")

    # ── 2. EDGE DISTRIBUTION on alertable picks ───────────────────
    print(f"\n{SEP}")
    print("  2. EDGE DISTRIBUTION — alertable picks only")
    print(SEP)
    pd = pre["picks"]; nd = post["picks"]
    if pd.get("n", 0) > 0 and nd.get("n", 0) > 0:
        print(f"  {'Metric':<12}  {'Pre-fix':>10}  {'Post-fix':>10}  {'Delta':>8}  Dir")
        print(f"  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*8}  {'-'*8}")
        for k, label in [
            ("mean", "Mean edge"),
            ("p50",  "Median"),
            ("p75",  "p75"),
            ("p90",  "p90"),
            ("p95",  "p95"),
            ("p99",  "p99"),
            ("max",  "Max edge"),
        ]:
            a = pd.get(k, 0); b = nd.get(k, 0); d = b - a
            arrow = "DOWN" if d < -0.005 else ("UP" if d > 0.005 else "~")
            print(f"  {label:<12}  {a*100:>9.1f}%  {b*100:>9.1f}%  {d*100:>+7.1f}%  {arrow}")

    # ── 3. THRESHOLD COUNTS on all edge samples ───────────────────
    print(f"\n{SEP}")
    print(f"  3. THRESHOLD COUNTS — all edge samples ({pre['all']['n']} per pass)")
    print(SEP)
    pa_ = pre["all"]; na_ = post["all"]
    total = pa_.get("n", 1)
    print(f"  {'Thresh':<8}  {'Pre cnt':>8}  {'Pre %':>7}  {'Post cnt':>9}  {'Post %':>8}  Change")
    print(f"  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*12}")
    for label, key in [(">5%",  "gt5"),  (">10%", "gt10"), (">15%", "gt15"),
                       (">20%", "gt20"), (">25%", "gt25"), (">30%", "gt30")]:
        pc_ = pa_.get(key, 0); nc_ = na_.get(key, 0)
        pp  = pc_ / total * 100; np_ = nc_ / total * 100
        chg = "reduced" if nc_ < pc_ else ("same" if nc_ == pc_ else "increased")
        print(f"  {label:<8}  {pc_:>8}  {pp:>6.1f}%  {nc_:>9}  {np_:>7.1f}%  {chg}")

    # ── 4. UNDERDOG CONCENTRATION ─────────────────────────────────
    print(f"\n{SEP}")
    print("  4. UNDERDOG CONCENTRATION among alertable picks")
    print(SEP)
    pu_pre  = pre["pick_udog"];  tot_pre  = pre["n_picks"]
    pu_post = post["pick_udog"]; tot_post = post["n_picks"]
    print(f"  Pre-fix  : underdogs {pu_pre}/{tot_pre} = {pct(pu_pre, tot_pre):.0f}%  "
          f"favorites {pre['pick_fav']}/{tot_pre} = {pct(pre['pick_fav'], tot_pre):.0f}%")
    print(f"  Post-fix : underdogs {pu_post}/{tot_post} = {pct(pu_post, tot_post):.0f}%  "
          f"favorites {post['pick_fav']}/{tot_post} = {pct(post['pick_fav'], tot_post):.0f}%")
    d_udog_pct = pct(pu_post, tot_post) - pct(pu_pre, tot_pre)
    print(f"  Delta    : {d_udog_pct:+.0f} pp  ({'improved' if d_udog_pct < -3 else 'small change'})")

    # ── 5. MODEL PROB OVERESTIMATION ──────────────────────────────
    print(f"\n{SEP}")
    print("  5. AVG (model_adj_prob − market_prob) — all samples")
    print(SEP)
    print(f"  UNDERDOG side:")
    print(f"    Pre-fix  : {pre['udog_diff']*100:+.2f}%")
    print(f"    Post-fix : {post['udog_diff']*100:+.2f}%")
    d_uo = (post["udog_diff"] - pre["udog_diff"]) * 100
    print(f"    Delta    : {d_uo:+.2f}pp  ({'less inflation' if d_uo < -0.5 else 'minimal'})")
    print(f"  FAVORITE side:")
    d_fav = (post["fav_diff"] - pre["fav_diff"]) * 100
    print(f"    Pre-fix  : {pre['fav_diff']*100:+.2f}%  "
          f"Post-fix : {post['fav_diff']*100:+.2f}%  "
          f"Delta : {d_fav:+.2f}pp")

    # ── 6. ATP / WTA SPLIT ────────────────────────────────────────
    print(f"\n{SEP}")
    print("  6. ATP / WTA EDGE SPLIT (all samples)")
    print(SEP)
    for label, pk, nk in [("ATP", pre["atp"], post["atp"]),
                           ("WTA", pre["wta"], post["wta"])]:
        if pk.get("n", 0) > 0:
            dm   = (nk.get("mean", 0) - pk.get("mean", 0)) * 100
            dp90 = (nk.get("p90",  0) - pk.get("p90",  0)) * 100
            print(f"  {label}  mean: {pk['mean']*100:+.1f}% → {nk['mean']*100:+.1f}%  ({dm:+.1f}pp)  "
                  f"p90: {pk.get('p90',0)*100:+.1f}% → {nk.get('p90',0)*100:+.1f}%  ({dp90:+.1f}pp)")

    # ── 7. COMPARISON vs SURFACE_FORM AUDIT (previous audit) ──────
    # Baseline: the "post-fix" numbers from surface_form_fix_audit.py
    # represent the model state just before today's recent_form fix.
    # We re-derive them here as: run current model WITH old _form_score,
    # which equals the post-surface_form, pre-recent_form state.
    # That is exactly what pre[] contains.  So:
    #   previous audit post-fix = pre[]  (surface fixed, form not fixed)
    #   today's post-fix        = post[] (both fixed)
    print(f"\n{SEP}")
    print("  7. COMPARISON vs PREVIOUS AUDIT (surface_form fix, before recent_form fix)")
    print(SEP)
    pre_mean  = pre["picks"].get("mean", 0)  or 0.0
    post_mean = post["picks"].get("mean", 0) or 0.0
    d_mean    = post_mean - pre_mean
    pre_max   = pre["picks"].get("max", 0)   or 0.0
    post_max  = post["picks"].get("max", 0)  or 0.0
    d_max     = post_max - pre_max
    udog_pct_pre  = pct(pu_pre,  tot_pre)
    udog_pct_post = pct(pu_post, tot_post)
    print(f"  Mean edge change     : {pre_mean*100:.1f}% → {post_mean*100:.1f}%  "
          f"({d_mean*100:+.1f}pp)")
    print(f"  Underdog % change    : {udog_pct_pre:.0f}% → {udog_pct_post:.0f}%  "
          f"({udog_pct_post - udog_pct_pre:+.0f}pp)")
    print(f"  Max edge change      : {pre_max*100:.1f}% → {post_max*100:.1f}%  "
          f"({d_max*100:+.1f}pp)")

    # ── 8. TOP 5 PROB REDUCTIONS caused by recent_form fix ────────
    print(f"\n{SEP}")
    print("  8. TOP 5 PROBABILITY REDUCTIONS caused by recent_form fix")
    print("     (underdog adj_prob: post − pre, sorted by largest drop)")
    print(SEP)

    pre_map  = {r["label"]: r for r in pre["raw"]  if r}
    post_map = {r["label"]: r for r in post["raw"] if r}
    shifts = []
    for label in pre_map:
        if label not in post_map:
            continue
        pr = pre_map[label]; nr = post_map[label]
        # underdog side
        if pr["fav_is_a"]:
            pre_u = pr["adj_b"]; post_u = nr["adj_b"]
            pre_e = pr["eb"];    post_e = nr["eb"]
            udog_name = pr["pb"].short_name
            fav_name  = pr["pa"].short_name
        else:
            pre_u = pr["adj_a"]; post_u = nr["adj_a"]
            pre_e = pr["ea"];    post_e = nr["ea"]
            udog_name = pr["pa"].short_name
            fav_name  = pr["pb"].short_name
        match = f"{udog_name} vs {fav_name}"
        delta = post_u - pre_u
        shifts.append((label, match, pre_u, post_u, delta,
                       pre_e, post_e, pr["surf"], pr["tour"].upper()))

    # sort by largest reduction (most negative delta first)
    shifts.sort(key=lambda x: x[4])
    print(f"  {'ID':<8} {'Underdog vs Favorite':<30} {'Pre adj':>8} {'Post adj':>9} "
          f"{'Δ adj':>7} {'Surf':<6} Tour")
    print(f"  {'-'*8} {'-'*30} {'-'*8} {'-'*9} {'-'*7} {'-'*6} {'-'*4}")
    for row in shifts[:5]:
        lab, nm, pre_a, post_a, delta, pre_e, post_e, surf, tour = row
        print(f"  {lab:<8} {nm[:30]:<30} {pre_a*100:>7.1f}%  {post_a*100:>8.1f}%  "
              f"{delta*100:>+6.1f}%  {surf:<6} {tour}")

    # also show top 5 increases (underdogs who moved up — unexpected)
    increases = [s for s in shifts if s[4] > 0.005]
    if increases:
        increases.sort(key=lambda x: x[4], reverse=True)
        print(f"\n  Note: {len(increases)} underdogs with adj_prob increase "
              f"(favorite had better form, so underdog rose relatively):")
        for row in increases[:3]:
            lab, nm, pre_a, post_a, delta, *_ = row
            print(f"    {lab:<8} {nm[:30]:<30} {pre_a*100:.1f}% → {post_a*100:.1f}%  "
                  f"({delta*100:+.1f}pp)")

    # ── 9. VERDICT ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  9. VERDICT")
    print(SEP)

    d_udog_o = (post["udog_diff"] - pre["udog_diff"]) * 100

    material_edge    = d_mean < -0.02
    material_overest = d_uo   < -0.5
    tail_improved    = post["picks"].get("gt20", 0) < pre["picks"].get("gt20", 0)
    udog_reduced     = (udog_pct_post < udog_pct_pre - 5)
    any_improvement  = (d_mean < -0.005 or d_uo < -0.5 or tail_improved)

    if material_edge and material_overest:
        verdict = "calibration improved"
    elif any_improvement:
        verdict = "small change"
    else:
        verdict = "still underdog-heavy"

    print()
    print(f"  Mean pick edge          : {pre_mean*100:.1f}%  →  {post_mean*100:.1f}%  ({d_mean*100:+.1f}pp)")
    print(f"  Underdog overestimation : {pre['udog_diff']*100:+.1f}%  →  "
          f"{post['udog_diff']*100:+.1f}%  ({d_udog_o:+.1f}pp)")
    print(f"  Picks >20% edge         : {pre['picks'].get('gt20',0)}  →  "
          f"{post['picks'].get('gt20',0)}")
    print(f"  Underdog pick rate      : {udog_pct_pre:.0f}%  →  {udog_pct_post:.0f}%")
    print()
    print(f"  VERDICT: \"{verdict}\"")
    print()

    if d_mean < -0.005:
        print(f"  [+] Mean pick edge: {abs(d_mean)*100:.1f}pp lower — model less overconfident")
    else:
        print(f"  [~] Mean pick edge change: {d_mean*100:+.1f}pp")

    if d_uo < -0.5:
        print(f"  [+] Underdog overestimation: {abs(d_uo):.1f}pp reduction")
    else:
        print(f"  [~] Underdog overestimation change: {d_uo:+.1f}pp")

    if tail_improved:
        print(f"  [+] Tail >20%: {pre['picks'].get('gt20',0)} → {post['picks'].get('gt20',0)} picks")
    else:
        print(f"  [~] Tail >20% unchanged: {post['picks'].get('gt20',0)} picks")

    if udog_reduced:
        print(f"  [+] Underdog pick rate fell {udog_pct_pre:.0f}% → {udog_pct_post:.0f}%")
    else:
        print(f"  [~] Underdog pick rate: {udog_pct_pre:.0f}% → {udog_pct_post:.0f}%")

    print()
    print("  recent_form now shrinks toward elo prior")
    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
