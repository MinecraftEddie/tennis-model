"""
diagnostics/prior_fix_audit.py
================================
Before / after edge distribution audit for the ranking-anchored prior fix.

Pre-fix  : stored edges from data/predictions.json (computed with neutral 0.50 priors).
Post-fix : edges re-computed by running calculate_probability() with the new model
           (ranking_prior replaces 0.50 when data is missing) on the same matches.

Run from the parent directory of tennis_model/:
    python tennis_model/diagnostics/prior_fix_audit.py

Does NOT modify any model, scoring, or production file.

Audit sections
--------------
1. Pre-fix distribution  (all 43 stored picks)
2. Post-fix distribution (subset re-run with static profiles)
3. Side-by-side comparison on the re-runnable subset
4. Underdog concentration analysis before / after
5. Conclusion
"""

import json
import os
import sys
import logging
logging.disable(logging.CRITICAL)   # silence ELO / model chatter

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

PREDICTIONS_FILE = os.path.join(_PARENT, "data", "predictions.json")
SEP  = "=" * 72
SEP2 = "-" * 72

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------
from tennis_model.models      import PlayerProfile
from tennis_model.profiles    import STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
from tennis_model.model       import calculate_probability
from tennis_model.probability_adjustments import shrink_toward_market, SHRINK_ALPHA


# ---------------------------------------------------------------------------
# PROFILE HELPERS  (no network — static only)
# ---------------------------------------------------------------------------

def _profile_from_static(pid: str) -> "PlayerProfile | None":
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


def _profile_from_wta(name_lower: str) -> "PlayerProfile | None":
    for key, d in WTA_PROFILES.items():
        if key == name_lower or key.split()[-1] == name_lower.split()[-1] or key in name_lower:
            p = PlayerProfile(short_name=d.get("full_name", name_lower))
            for k, v in d.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            p.full_name   = d.get("full_name", name_lower)
            p.data_source = "wta_static"
            return p
    return None


def _build_profile(pred: dict, side: str) -> "PlayerProfile | None":
    name = pred.get(f"player_{side}", "").lower()
    tour = (pred.get("tour") or "").upper()
    if tour == "WTA":
        return _profile_from_wta(name)
    for key, (full, slug, pid) in PLAYER_ID_MAP.items():
        if key in name or name.split()[-1] == key:
            return _profile_from_static(pid)
    return None


# ---------------------------------------------------------------------------
# EDGE HELPERS
# ---------------------------------------------------------------------------

def _stored_edge(pred: dict) -> "float | None":
    """Return stored (pre-fix) edge on the picked side."""
    pick = pred.get("pick")
    if pick == pred.get("player_a"):
        return pred.get("edge_a")
    if pick == pred.get("player_b"):
        return pred.get("edge_b")
    return None


def _is_underdog(pred: dict) -> "bool | None":
    """True if the picked player has the longer market odds."""
    oa = pred.get("best_odds_a")
    ob = pred.get("best_odds_b")
    if not oa or not ob:
        return None
    pick = pred.get("pick")
    pa   = pred.get("player_a")
    # underdog = longer odds = higher decimal value
    if pick == pa:
        return oa > ob
    return ob > oa


def _market_prob(pred: dict, side: str) -> "float | None":
    """Vig-stripped market probability for side 'a' or 'b'."""
    oa = pred.get("best_odds_a")
    ob = pred.get("best_odds_b")
    if not oa or not ob:
        return None
    raw_a = 1.0 / oa
    raw_b = 1.0 / ob
    total = raw_a + raw_b
    return (raw_a / total) if side == "a" else (raw_b / total)


def _rerun_edge(pred: dict) -> "tuple[float, float, float] | None":
    """
    Re-run calculate_probability() with new model, apply shrink, return:
        (new_edge, new_model_prob_pick, mkt_prob_pick)
    Returns None if profiles unavailable or computation fails.
    """
    pa = _build_profile(pred, "a")
    pb = _build_profile(pred, "b")
    if pa is None or pb is None:
        return None

    oa  = pred.get("best_odds_a")
    ob  = pred.get("best_odds_b")
    pick_odds = pred.get("pick_odds")
    if not oa or not ob or not pick_odds:
        return None

    try:
        prob_a, prob_b, _ = calculate_probability(
            pa, pb,
            surface=pred.get("surface", "Hard"),
            h2h_a=0, h2h_b=0,
            market_odds_a=oa, market_odds_b=ob,
        )
    except Exception:
        return None

    pick = pred.get("pick")
    is_a = (pick == pred.get("player_a"))
    prob_pick = prob_a if is_a else prob_b
    mkt_pick  = (_market_prob(pred, "a") if is_a else _market_prob(pred, "b")) or (1.0/pick_odds)

    shrunk    = shrink_toward_market(prob_pick, pick_odds)
    new_edge  = pick_odds * shrunk - 1.0          # decimal fraction: 0.15 = 15%

    return (new_edge, shrunk, mkt_pick)


# ---------------------------------------------------------------------------
# STATS HELPERS  (same pattern as edge_distribution_audit.py)
# ---------------------------------------------------------------------------

def _pct(n, total):
    return 0.0 if total == 0 else round(100.0 * n / total, 1)


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]


def _dist_stats(edges: list) -> dict:
    if not edges:
        return {"n": 0}
    s = sorted(edges)
    n = len(s)
    return {
        "n":      n,
        "mean":   round(sum(s) / n, 4),
        "median": _percentile(s, 50),
        "p75":    _percentile(s, 75),
        "p90":    _percentile(s, 90),
        "p95":    _percentile(s, 95),
        "p99":    _percentile(s, 99),
        "min":    s[0],
        "max":    s[-1],
        "counts": {
            f">{int(t*100)}%": {
                "count": sum(1 for e in s if e > t),
                "pct":   _pct(sum(1 for e in s if e > t), n),
            }
            for t in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
        },
    }


def _print_stats(label: str, stats: dict) -> None:
    if stats.get("n", 0) == 0:
        print(f"  {label:<22}  n=0  (no data)")
        return
    n = stats["n"]
    c = stats["counts"]
    print(f"  {label:<22}  n={n}")
    print(f"    mean={stats['mean']:.1%}  median={stats['median']:.1%}"
          f"  p75={stats['p75']:.1%}  p90={stats['p90']:.1%}"
          f"  p95={stats['p95']:.1%}  p99={stats['p99']:.1%}")
    print(f"    min={stats['min']:.1%}  max={stats['max']:.1%}")
    parts = [f"{k}: {v['count']} ({v['pct']}%)" for k, v in c.items()]
    print(f"    Thresholds: {' | '.join(parts)}")


# ---------------------------------------------------------------------------
# MAIN AUDIT
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{SEP}")
    print("  PRIOR-FIX AUDIT  —  ranking-anchored prior vs neutral 0.50 prior")
    print(f"  Pre-fix  : stored edges from predictions.json (old model)")
    print(f"  Post-fix : re-run with new model (ranking_prior replaces 0.50 fallback)")
    print(f"{SEP}\n")

    # ── Load data ──────────────────────────────────────────────────────────
    if not os.path.exists(PREDICTIONS_FILE):
        print(f"  ERROR: {PREDICTIONS_FILE} not found.")
        return

    with open(PREDICTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    all_preds = data.get("predictions", [])
    usable    = [p for p in all_preds if p.get("pick") and _stored_edge(p) is not None]
    print(f"  Loaded {len(all_preds)} predictions | {len(usable)} with pick+edge")
    print(f"  Data source: {PREDICTIONS_FILE}\n")

    # ── SECTION 1 — Pre-fix distribution (all stored picks) ──────────────
    print(f"{SEP}")
    print("  1. PRE-FIX DISTRIBUTION  (stored edges — neutral 0.50 prior model)")
    print(f"{SEP}")

    pre_edges_all  = [_stored_edge(p) for p in usable]
    pre_atp        = [_stored_edge(p) for p in usable if (p.get("tour") or "").upper() == "ATP"]
    pre_wta        = [_stored_edge(p) for p in usable if (p.get("tour") or "").upper() == "WTA"]
    pre_fav        = [_stored_edge(p) for p in usable if _is_underdog(p) is False]
    pre_dog        = [_stored_edge(p) for p in usable if _is_underdog(p) is True]
    pre_high       = [_stored_edge(p) for p in usable if (p.get("confidence") or "").upper() == "HIGH"]
    pre_medium     = [_stored_edge(p) for p in usable if (p.get("confidence") or "").upper() == "MEDIUM"]

    _print_stats("ALL (pre-fix)",    _dist_stats(pre_edges_all))
    print()
    _print_stats("ATP (pre-fix)",    _dist_stats(pre_atp))
    _print_stats("WTA (pre-fix)",    _dist_stats(pre_wta))
    print()
    _print_stats("FAVORITE (pre)",   _dist_stats(pre_fav))
    _print_stats("UNDERDOG (pre)",   _dist_stats(pre_dog))
    print()
    _print_stats("HIGH conf (pre)",  _dist_stats(pre_high))
    _print_stats("MEDIUM conf (pre)",_dist_stats(pre_medium))

    n_dog_pre = len(pre_dog)
    pct_dog_pre = _pct(n_dog_pre, len(usable))
    print(f"\n  Underdog picks (pre-fix): {n_dog_pre}/{len(usable)} = {pct_dog_pre}%")

    # ── SECTION 2 — Re-run with new model ─────────────────────────────────
    print(f"\n{SEP}")
    print("  2. RE-RUNNING NEW MODEL  (ranking-anchored priors, static profiles only)")
    print(f"{SEP}")

    rerun_results = []   # (pred, pre_edge, new_edge, new_prob, mkt_prob)
    skipped = 0

    for pred in usable:
        pre_edge = _stored_edge(pred)
        result   = _rerun_edge(pred)
        if result is None:
            skipped += 1
            continue
        new_edge, new_prob, mkt_prob = result
        rerun_results.append({
            "pred":     pred,
            "pre_edge": pre_edge,
            "new_edge": new_edge,
            "new_prob": new_prob,
            "mkt_prob": mkt_prob,
            "is_dog":   _is_underdog(pred),
            "tour":     (pred.get("tour") or "?").upper(),
            "conf":     (pred.get("confidence") or "?").upper(),
        })

    n_rerun = len(rerun_results)
    print(f"  Re-run successful: {n_rerun} matches | Skipped (no static profile): {skipped}")

    if n_rerun == 0:
        print("  Cannot continue — no static profiles found for any stored pick.")
        print("  Add players to STATIC_PROFILES / WTA_PROFILES and re-run.\n")
        return

    # ── SECTION 3 — Post-fix distribution (re-run subset) ────────────────
    print(f"\n{SEP}")
    print("  3. POST-FIX DISTRIBUTION  (re-run subset — new model, new priors)")
    print(f"{SEP}")

    post_edges_all = [r["new_edge"] for r in rerun_results]
    post_atp       = [r["new_edge"] for r in rerun_results if r["tour"] == "ATP"]
    post_wta       = [r["new_edge"] for r in rerun_results if r["tour"] == "WTA"]
    post_fav       = [r["new_edge"] for r in rerun_results if r["is_dog"] is False]
    post_dog       = [r["new_edge"] for r in rerun_results if r["is_dog"] is True]
    post_high      = [r["new_edge"] for r in rerun_results if r["conf"] == "HIGH"]
    post_medium    = [r["new_edge"] for r in rerun_results if r["conf"] == "MEDIUM"]

    _print_stats("ALL (post-fix)",    _dist_stats(post_edges_all))
    print()
    _print_stats("ATP (post-fix)",    _dist_stats(post_atp))
    _print_stats("WTA (post-fix)",    _dist_stats(post_wta))
    print()
    _print_stats("FAVORITE (post)",   _dist_stats(post_fav))
    _print_stats("UNDERDOG (post)",   _dist_stats(post_dog))
    print()
    _print_stats("HIGH conf (post)",  _dist_stats(post_high))
    _print_stats("MEDIUM conf (post)",_dist_stats(post_medium))

    n_dog_post = sum(1 for r in rerun_results if r["is_dog"] is True)
    pct_dog_post = _pct(n_dog_post, n_rerun)
    print(f"\n  Underdog picks (post-fix, new model edges): {n_dog_post}/{n_rerun} = {pct_dog_post}%")
    print(f"  (Side selection unchanged — same stored picks; only edge magnitude changes)")

    # ── SECTION 4 — Side-by-side on re-run subset ─────────────────────────
    print(f"\n{SEP}")
    print("  4. SIDE-BY-SIDE COMPARISON  (same {n_rerun} matches, pre vs post)")
    print(f"{SEP}")

    # Compare on the SAME subset (pre-fix edges for re-run matches vs new edges)
    sub_pre  = [r["pre_edge"] for r in rerun_results]
    sub_post = [r["new_edge"] for r in rerun_results]
    sub_pre_s  = _dist_stats(sub_pre)
    sub_post_s = _dist_stats(sub_post)

    def _delta(pre, post, key):
        a, b = pre.get(key), post.get(key)
        if a is None or b is None:
            return "n/a"
        return f"{b - a:+.1%}"

    print(f"  n = {n_rerun} matches (those with static profiles)")
    print()
    print(f"  {'Metric':<12}  {'Pre-fix':>9}  {'Post-fix':>9}  {'Delta':>8}  Direction")
    print(f"  {'-'*12}  {'-'*9}  {'-'*9}  {'-'*8}  {'-'*20}")
    rows = [
        ("mean",   "Mean edge"),
        ("median", "Median edge"),
        ("p75",    "p75"),
        ("p90",    "p90"),
        ("p95",    "p95"),
        ("p99",    "p99"),
        ("max",    "Max edge"),
    ]
    for key, label in rows:
        pre_v  = sub_pre_s.get(key)
        post_v = sub_post_s.get(key)
        if pre_v is None or post_v is None:
            continue
        delta  = post_v - pre_v
        arrow  = "DOWN (better)" if delta < -0.005 else ("UP (worse)" if delta > 0.005 else "unchanged")
        print(f"  {label:<12}  {pre_v:>9.1%}  {post_v:>9.1%}  {delta:>+8.1%}  {arrow}")

    print()
    print(f"  Threshold counts (same subset, n={n_rerun}):")
    print(f"  {'Threshold':<12}  {'Pre count':>10}  {'Post count':>10}  {'Pre %':>7}  {'Post %':>7}  Change")
    print(f"  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*7}  {'-'*15}")
    for t_key in (">5%", ">10%", ">15%", ">20%", ">25%", ">30%"):
        pc = sub_pre_s["counts"].get(t_key, {})
        qc = sub_post_s["counts"].get(t_key, {})
        pre_c  = pc.get("count", 0)
        post_c = qc.get("count", 0)
        pre_p  = pc.get("pct", 0.0)
        post_p = qc.get("pct", 0.0)
        change = "reduced" if post_c < pre_c else ("same" if post_c == pre_c else "increased")
        print(f"  {t_key:<12}  {pre_c:>10}  {post_c:>10}  {pre_p:>6.1f}%  {post_p:>6.1f}%  {change}")

    # ── SECTION 5 — Underdog analysis ────────────────────────────────────
    print(f"\n{SEP}")
    print("  5. UNDERDOG ANALYSIS  (model prob vs market prob)")
    print(f"{SEP}")

    dog_rows = [r for r in rerun_results if r["is_dog"] is True]
    fav_rows = [r for r in rerun_results if r["is_dog"] is False]

    def _avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    if dog_rows:
        # Pre-fix: stored prob_a/b vs market
        pre_dog_model_probs = []
        pre_dog_mkt_probs   = []
        for r in dog_rows:
            pred   = r["pred"]
            pick   = pred.get("pick")
            is_a   = (pick == pred.get("player_a"))
            stored_prob_a = pred.get("prob_a", 0.5)
            stored_pick_prob = stored_prob_a if is_a else (1.0 - stored_prob_a)
            pre_dog_model_probs.append(stored_pick_prob)
            pre_dog_mkt_probs.append(r["mkt_prob"])

        post_dog_model_probs = [r["new_prob"] for r in dog_rows]
        post_dog_mkt_probs   = [r["mkt_prob"] for r in dog_rows]

        pre_overest  = _avg([m - mk for m, mk in zip(pre_dog_model_probs, pre_dog_mkt_probs)])
        post_overest = _avg([m - mk for m, mk in zip(post_dog_model_probs, post_dog_mkt_probs)])

        print(f"  Underdog picks in re-run subset : {len(dog_rows)}/{n_rerun}")
        print()
        print(f"  {'Metric':<42}  {'Pre-fix':>8}  {'Post-fix':>9}  {'Delta':>8}")
        print(f"  {'-'*42}  {'-'*8}  {'-'*9}  {'-'*8}")
        print(f"  {'Avg model prob for underdogs':<42}  "
              f"{_avg(pre_dog_model_probs):>8.1%}  "
              f"{_avg(post_dog_model_probs):>9.1%}  "
              f"{_avg(post_dog_model_probs) - _avg(pre_dog_model_probs):>+8.1%}")
        print(f"  {'Avg market prob for underdogs':<42}  "
              f"{_avg(pre_dog_mkt_probs):>8.1%}  "
              f"{_avg(post_dog_mkt_probs):>9.1%}  "
              f"{'(same)':>8}")
        print(f"  {'Avg model overestimation (model - market)':<42}  "
              f"{pre_overest:>+8.1%}  "
              f"{post_overest:>+9.1%}  "
              f"{post_overest - pre_overest:>+8.1%}")
        print()
        print(f"  Avg pre-fix  underdog edge : {_avg([r['pre_edge'] for r in dog_rows]):.1%}")
        print(f"  Avg post-fix underdog edge : {_avg([r['new_edge'] for r in dog_rows]):.1%}")
        print(f"  Edge reduction on underdogs: {_avg([r['new_edge'] for r in dog_rows]) - _avg([r['pre_edge'] for r in dog_rows]):+.1%}")

    if fav_rows:
        print(f"\n  Favorite picks in re-run subset : {len(fav_rows)}/{n_rerun}")
        print(f"  Avg pre-fix  favorite edge : {_avg([r['pre_edge'] for r in fav_rows]):.1%}")
        print(f"  Avg post-fix favorite edge : {_avg([r['new_edge'] for r in fav_rows]):.1%}")

    # ── Underdog concentration pre vs post ────────────────────────────────
    print(f"\n  % picks on underdogs (pre-fix stored, all {len(usable)}): {pct_dog_pre}%")
    print(f"  % picks on underdogs (post-fix model, subset {n_rerun}): {pct_dog_post}%")
    print(f"  Note: side selection unchanged — model probability changes reduce underdog")
    print(f"  EDGE, not underdog SELECTION (that's determined by EV threshold, not prior).")

    # ── SECTION 6 — Comparison table vs known audit baselines ─────────────
    print(f"\n{SEP}")
    print("  6. COMPARISON VS PRIOR AUDIT BASELINES")
    print(f"{SEP}")
    print()
    print(f"  {'Metric':<28}  {'Pre-shrink':>10}  {'Post-shrink':>11}  {'Post-prior-fix':>14}")
    print(f"  {'(from probability_breakdown)':28}  {'(alpha=0.70)':>10}  {'(alpha=0.70)':>11}  {'(this audit)':>14}")
    print(f"  {'-'*28}  {'-'*10}  {'-'*11}  {'-'*14}")

    # Retrieve overall stats for the comparison table
    all_pre_s  = _dist_stats(pre_edges_all)
    post_fix_s = _dist_stats(post_edges_all)

    # Pre-shrink = pre_edges_all / SHRINK_ALPHA  (reverse the stored shrink)
    preshrink_edges = [e / SHRINK_ALPHA for e in pre_edges_all]
    preshrink_s     = _dist_stats(preshrink_edges)

    rows_cmp = [
        ("mean",   "Mean edge"),
        ("median", "Median edge"),
        ("p90",    "p90"),
        ("p95",    "p95"),
        ("max",    "Max edge"),
    ]
    for key, label in rows_cmp:
        a = preshrink_s.get(key)
        b = all_pre_s.get(key)
        c = post_fix_s.get(key)
        a_s = f"{a:.1%}" if a is not None else "n/a"
        b_s = f"{b:.1%}" if b is not None else "n/a"
        c_s = f"{c:.1%}" if c is not None else "n/a"
        print(f"  {label:<28}  {a_s:>10}  {b_s:>11}  {c_s:>14}")

    # Underdog % rows for comparison table
    pct_udog_post_full = _pct(
        sum(1 for r in rerun_results if r["is_dog"] is True), n_rerun
    )
    print(f"  {'% underdog picks':<28}  {'n/a':>10}  {pct_dog_pre:>10.1f}%  {pct_udog_post_full:>13.1f}%")

    # ── SECTION 7 — Conclusion ────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  7. CONCLUSION")
    print(f"{SEP}")

    sub_mean_pre  = sub_pre_s.get("mean",  0) or 0
    sub_mean_post = sub_post_s.get("mean", 0) or 0
    mean_delta    = sub_mean_post - sub_mean_pre
    post_over20   = sub_post_s["counts"].get(">20%", {}).get("pct", 0)
    pre_over20    = sub_pre_s["counts"].get(">20%", {}).get("pct", 0)

    if dog_rows:
        overest_pre  = pre_overest
        overest_post = post_overest
        overest_delta = overest_post - overest_pre
    else:
        overest_pre = overest_post = overest_delta = 0.0

    print()
    print(f"  Prior fix summary (n={n_rerun} re-runnable matches):")
    print(f"    Mean edge change       : {mean_delta:+.1%}  ({sub_mean_pre:.1%} -> {sub_mean_post:.1%})")
    if dog_rows:
        print(f"    Underdog overestimation: {overest_delta:+.1%}  ({overest_pre:+.1%} -> {overest_post:+.1%})")
    print(f"    Picks >20% edge        : {pre_over20:.1f}% -> {post_over20:.1f}% of subset")
    print(f"    Underdog pick rate     : {pct_dog_pre:.1f}% (pre) -> {pct_dog_post:.1f}% (post)")
    print()

    # Determine verdict
    material_edge_drop   = mean_delta < -0.02
    material_overest_drop = (overest_delta < -0.03) if dog_rows else False
    tail_improved        = post_over20 < pre_over20 - 5

    if material_edge_drop and material_overest_drop:
        verdict = "prior fix materially improved calibration"
    elif material_edge_drop or material_overest_drop or tail_improved:
        verdict = "prior fix helped but underdog inflation remains"
    else:
        verdict = "little change, investigate tournament_exp next"

    print(f"  VERDICT: \"{verdict}\"")
    print()

    # Evidence summary
    if mean_delta < -0.01:
        print(f"  [+] Mean edge dropped {abs(mean_delta):.1%} — model less overconfident overall")
    else:
        print(f"  [-] Mean edge change small ({mean_delta:+.1%}) — prior fix limited impact on stored picks")

    if dog_rows and overest_delta < -0.02:
        print(f"  [+] Underdog overestimation reduced {abs(overest_delta):.1%} pp — ranking prior working")
    elif dog_rows:
        print(f"  [-] Underdog overestimation change small ({overest_delta:+.1%}) — tournament_exp may still dominate")

    if post_over20 < pre_over20:
        print(f"  [+] Tail (>20%) compressed: {pre_over20:.1f}% -> {post_over20:.1f}% of picks")
    else:
        print(f"  [-] Tail unchanged — consider adjusting tournament_exp or recent_form weight")

    if pct_dog_pre > 90:
        print(f"  [!] {pct_dog_pre:.0f}% underdog picks confirms structural bias remains")
        print(f"      Next step: audit tournament_exp factor for veteran-underdog inflation")

    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
