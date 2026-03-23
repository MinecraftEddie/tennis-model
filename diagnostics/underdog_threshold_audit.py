"""
diagnostics/underdog_threshold_audit.py
=========================================
Post-fix audit: impact of the underdog-specific alert threshold
added to evaluator/evaluator.py (Step 6c).

Data source: data/predictions.json  (43 stored historical picks)
             All stored picks were previously alertable (send / send_with_caution).
             This audit simulates which would now be demoted to WATCHLIST.

Threshold rule (evaluator Step 6c):
    If picked side is the market underdog (pick_odds > opp_odds):
        odds <= 3.00  ->  require edge >= 0.15
        odds >  3.00  ->  require edge >= 0.18
    Otherwise: WATCHLIST

Run from the parent directory of tennis_model/:
    python tennis_model/diagnostics/underdog_threshold_audit.py

Does NOT modify any model, evaluator, or production file.
"""

import json
import os
import sys

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

PREDICTIONS_FILE = os.path.join(_PARENT, "data", "predictions.json")

SEP  = "=" * 72
SEP2 = "-" * 72


# ---------------------------------------------------------------------------
# THRESHOLD RULE (mirrors evaluator Step 6c exactly)
# ---------------------------------------------------------------------------
THRESHOLD_LOW  = 0.15   # odds <= 3.00
THRESHOLD_HIGH = 0.18   # odds >  3.00
ODDS_CUTOFF    = 3.00


def _apply_underdog_threshold(pick_odds: float, opp_odds: float,
                               edge_dec: float) -> tuple[bool, bool, float]:
    """
    Returns (is_underdog, demoted, threshold_used).
    demoted=True means this pick would be moved to WATCHLIST.
    """
    if pick_odds <= 0 or opp_odds <= 0:
        return False, False, 0.0
    is_underdog = pick_odds > opp_odds
    if not is_underdog:
        return False, False, 0.0
    threshold = THRESHOLD_HIGH if pick_odds > ODDS_CUTOFF else THRESHOLD_LOW
    demoted   = edge_dec < threshold
    return True, demoted, threshold


# ---------------------------------------------------------------------------
# DATA HELPERS
# ---------------------------------------------------------------------------

def _pick_data(pred: dict) -> dict | None:
    """Extract pick-side edge, pick_odds, opp_odds, tour, confidence."""
    pick   = pred.get("pick", "")
    pa     = pred.get("player_a", "")
    pb     = pred.get("player_b", "")
    oa     = pred.get("best_odds_a") or 0.0
    ob     = pred.get("best_odds_b") or 0.0

    if pick == pa:
        edge_dec  = pred.get("edge_a") or 0.0
        pick_odds = oa
        opp_odds  = ob
    elif pick == pb:
        edge_dec  = pred.get("edge_b") or 0.0
        pick_odds = ob
        opp_odds  = oa
    else:
        return None  # can't determine side

    return {
        "id":         pred.get("id", ""),
        "match":      pred.get("match", ""),
        "pick":       pick,
        "pick_odds":  pick_odds,
        "opp_odds":   opp_odds,
        "edge_dec":   edge_dec,
        "tour":       (pred.get("tour") or "").upper(),
        "confidence": (pred.get("confidence") or "").upper(),
        "surface":    pred.get("surface", ""),
        "date":       pred.get("date", ""),
    }


def _percentile(s: list, p: float) -> float:
    if not s:
        return 0.0
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


def _stats(edges: list[float]) -> dict:
    if not edges:
        return {}
    s = sorted(edges)
    n = len(s)
    return {
        "n":      n,
        "mean":   sum(s) / n,
        "median": _percentile(s, 50),
        "p75":    _percentile(s, 75),
        "p90":    _percentile(s, 90),
        "p95":    _percentile(s, 95),
        "p99":    _percentile(s, 99),
        "gt05":   sum(1 for e in s if e > 0.05),
        "gt10":   sum(1 for e in s if e > 0.10),
        "gt15":   sum(1 for e in s if e > 0.15),
        "gt20":   sum(1 for e in s if e > 0.20),
        "gt25":   sum(1 for e in s if e > 0.25),
        "gt30":   sum(1 for e in s if e > 0.30),
    }


def _fmt_stats(label: str, st: dict) -> None:
    if not st or st.get("n", 0) == 0:
        print(f"  {label:<24}  n=0  (no data)")
        return
    n = st["n"]
    print(f"  {label:<24}  n={n}")
    print(f"    mean={st['mean']:.1%}  median={st['median']:.1%}"
          f"  p75={st['p75']:.1%}  p90={st['p90']:.1%}"
          f"  p95={st['p95']:.1%}  p99={st['p99']:.1%}")
    parts = [
        f">5%:{st['gt05']}/{n}",
        f">10%:{st['gt10']}/{n}",
        f">15%:{st['gt15']}/{n}",
        f">20%:{st['gt20']}/{n}",
        f">25%:{st['gt25']}/{n}",
        f">30%:{st['gt30']}/{n}",
    ]
    print(f"    Counts: {' | '.join(parts)}")


def _pct(n, total) -> str:
    if total == 0:
        return "n/a"
    return f"{100.0*n/total:.1f}%"


# ---------------------------------------------------------------------------
# MAIN AUDIT
# ---------------------------------------------------------------------------

def run_audit() -> None:
    print(f"\n{SEP}")
    print("  UNDERDOG THRESHOLD AUDIT  —  post-fix evaluator Step 6c")
    print(f"  Threshold: underdog @<=3.00 -> edge>=15%  |  @>3.00 -> edge>=18%")
    print(f"  Baseline : all 43 stored picks (previously alertable, pre-threshold)")
    print(SEP)

    # ── Load ──────────────────────────────────────────────────────────────────
    if not os.path.exists(PREDICTIONS_FILE):
        print(f"\n  predictions.json not found at {PREDICTIONS_FILE}")
        return

    with open(PREDICTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    all_preds = data.get("predictions", [])
    print(f"\n  Loaded {len(all_preds)} predictions from {PREDICTIONS_FILE}\n")

    rows = [r for p in all_preds if (r := _pick_data(p)) is not None]
    skipped = len(all_preds) - len(rows)
    if skipped:
        print(f"  Skipped {skipped} records with unresolvable pick side.\n")

    total = len(rows)

    # ── Classify ──────────────────────────────────────────────────────────────
    alertable   = []   # passes new threshold (or favorite)
    watchlisted = []   # demoted by underdog threshold
    favorites   = []
    underdogs_pass  = []
    underdogs_fail  = []

    for r in rows:
        is_udog, demoted, thresh = _apply_underdog_threshold(
            r["pick_odds"], r["opp_odds"], r["edge_dec"]
        )
        r["is_underdog"] = is_udog
        r["demoted"]     = demoted
        r["threshold"]   = thresh

        if demoted:
            watchlisted.append(r)
            underdogs_fail.append(r)
        else:
            alertable.append(r)
            if is_udog:
                underdogs_pass.append(r)
            else:
                favorites.append(r)

    n_alert = len(alertable)
    n_watch = len(watchlisted)
    n_udog_alert = len(underdogs_pass)
    n_fav_alert  = len(favorites)

    # ── Report ────────────────────────────────────────────────────────────────

    print(f"{SEP}")
    print("1. PICK COUNTS")
    print(SEP)
    print(f"  Total picks analyzed          : {total}")
    print(f"  Normal alertable (pass)       : {n_alert}  ({_pct(n_alert, total)})")
    print(f"    of which — favorites        : {n_fav_alert}")
    print(f"    of which — underdogs (pass) : {n_udog_alert}")
    print(f"  Demoted -> WATCHLIST           : {n_watch}  ({_pct(n_watch, total)})")
    print()

    # ── 2. Edge stats on alertable picks ──────────────────────────────────────
    print(f"{SEP}")
    print("2. EDGE DISTRIBUTION  (normal alertable picks only)")
    print(SEP)
    alert_edges = [r["edge_dec"] for r in alertable]
    _fmt_stats("ALL ALERTABLE", _stats(alert_edges))
    _fmt_stats("  favorites",   _stats([r["edge_dec"] for r in favorites]))
    _fmt_stats("  underdogs",   _stats([r["edge_dec"] for r in underdogs_pass]))
    print()

    # ── 3. Underdog % ─────────────────────────────────────────────────────────
    print(f"{SEP}")
    print("3. UNDERDOG CONCENTRATION")
    print(SEP)
    total_udog_baseline = sum(1 for r in rows if r["is_underdog"])
    pct_udog_baseline   = _pct(total_udog_baseline, total)
    pct_udog_alert      = _pct(n_udog_alert, n_alert)
    print(f"  Baseline (pre-threshold):  {total_udog_baseline}/{total} underdogs = {pct_udog_baseline}")
    print(f"  After threshold:           {n_udog_alert}/{n_alert} underdogs = {pct_udog_alert}")
    print(f"  Underdogs demoted:         {len(underdogs_fail)}  (of {total_udog_baseline} baseline underdogs)")
    print()

    # ── 4. ATP / WTA split ────────────────────────────────────────────────────
    print(f"{SEP}")
    print("4. ATP / WTA SPLIT  (normal alertable picks only)")
    print(SEP)
    for tour in ("ATP", "WTA"):
        subset = [r for r in alertable if r["tour"] == tour]
        udog   = sum(1 for r in subset if r["is_underdog"])
        print(f"  {tour:<4}  alertable: {len(subset)}  "
              f"underdogs: {udog}/{len(subset)} = {_pct(udog, len(subset))}")
        _fmt_stats(f"  {tour}", _stats([r["edge_dec"] for r in subset]))
    print()

    # ── 5. Confidence split ───────────────────────────────────────────────────
    print(f"{SEP}")
    print("5. CONFIDENCE TIER SPLIT  (normal alertable picks only)")
    print(SEP)
    for tier in ("HIGH", "MEDIUM", "LOW", "VERY HIGH", ""):
        subset = [r for r in alertable if r["confidence"] == tier]
        if not subset:
            continue
        label = tier if tier else "(unknown)"
        udog  = sum(1 for r in subset if r["is_underdog"])
        print(f"  {label:<10}  alertable: {len(subset)}  "
              f"underdogs: {udog}/{len(subset)} = {_pct(udog, len(subset))}")
        _fmt_stats(f"  {label}", _stats([r["edge_dec"] for r in subset]))
    print()

    # ── 6. Before / after comparison ──────────────────────────────────────────
    print(f"{SEP}")
    print("6. BEFORE / AFTER COMPARISON  (vs previous post-fix audit baseline)")
    print(SEP)
    baseline_edges = [r["edge_dec"] for r in rows]
    pre  = _stats(baseline_edges)
    post = _stats(alert_edges)

    if pre and post:
        rows_cmp = [
            ("n picks",  pre["n"],      post["n"]),
            ("mean",     pre["mean"],   post["mean"]),
            ("median",   pre["median"], post["median"]),
            ("p75",      pre["p75"],    post["p75"]),
            ("p90",      pre["p90"],    post["p90"]),
            ("p95",      pre["p95"],    post["p95"]),
            ("p99",      pre["p99"],    post["p99"]),
        ]
        print(f"  {'Metric':<12}  {'Pre-threshold':>14}  {'Post-threshold':>15}  {'Delta':>8}")
        print(f"  {SEP2[:55]}")
        for label, pre_v, post_v in rows_cmp:
            if label == "n picks":
                delta = f"{post_v - pre_v:+d}"
                print(f"  {label:<12}  {pre_v:>14d}  {post_v:>15d}  {delta:>8}")
            else:
                delta = f"{post_v - pre_v:+.1%}"
                print(f"  {label:<12}  {pre_v:>13.1%}  {post_v:>14.1%}  {delta:>8}")

        print()
        # Threshold counts comparison
        thresh_keys = [
            (">5%",  "gt05"), (">10%", "gt10"), (">15%", "gt15"),
            (">20%", "gt20"), (">25%", "gt25"), (">30%", "gt30"),
        ]
        print(f"  {'Threshold':<10}  {'Pre n':>7}  {'Pre%':>7}  "
              f"{'Post n':>7}  {'Post%':>8}")
        print(f"  {SEP2[:50]}")
        for label, key in thresh_keys:
            pre_c   = pre[key]
            post_c  = post[key]
            pre_pct  = _pct(pre_c,  pre["n"])
            post_pct = _pct(post_c, post["n"])
            print(f"  {label:<10}  {pre_c:>7}  {pre_pct:>7}  "
                  f"{post_c:>7}  {post_pct:>8}")
        print()

        # Underdog change
        pre_udog_pct  = _pct(total_udog_baseline, total)
        post_udog_pct = _pct(n_udog_alert, n_alert)
        print(f"  % underdogs in alertable set:  {pre_udog_pct} -> {post_udog_pct}")
    print()

    # ── 7. Demoted picks detail ────────────────────────────────────────────────
    print(f"{SEP}")
    print("7. DEMOTED PICKS  (would now be WATCHLIST)")
    print(SEP)
    if watchlisted:
        print(f"  {'#':<3}  {'Odds':>5}  {'Edge':>6}  {'Thresh':>7}  "
              f"{'Tour':<4}  {'Conf':<7}  {'Date':<11}  Match")
        print(f"  {'-'*3}  {'-'*5}  {'-'*6}  {'-'*7}  "
              f"{'-'*4}  {'-'*7}  {'-'*11}  {'-'*30}")
        for i, r in enumerate(sorted(watchlisted,
                                     key=lambda x: x["pick_odds"]), 1):
            print(f"  {i:<3}  {r['pick_odds']:>5.2f}  "
                  f"{r['edge_dec']:>6.1%}  {r['threshold']:>7.1%}  "
                  f"{r['tour']:<4}  {r['confidence']:<7}  "
                  f"{r['date']:<11}  {r['match'][:30]}")
    else:
        print("  No picks demoted.")
    print()

    # ── 8. Verdict ─────────────────────────────────────────────────────────────
    print(f"{SEP}")
    print("8. VERDICT")
    print(SEP)
    pct_udog_after_f = (100.0 * n_udog_alert / n_alert) if n_alert else 0.0

    if pct_udog_after_f <= 30.0:
        verdict = "final alert distribution now looks reasonable"
    elif pct_udog_after_f <= 60.0:
        verdict = "improved but still underdog-heavy"
    else:
        verdict = "further calibration still needed"

    print(f"\n  >> \"{verdict}\"")
    print()
    print(f"  Key metrics summary:")
    print(f"    Total picks analyzed            : {total}")
    print(f"    Normal alertable (post)         : {n_alert}  "
          f"({_pct(n_alert, total)} of baseline)")
    print(f"    WATCHLIST (demoted by threshold): {n_watch}  "
          f"({_pct(n_watch, total)} of baseline)")
    print(f"    % underdogs  BEFORE threshold   : {_pct(total_udog_baseline, total)}")
    print(f"    % underdogs  AFTER  threshold   : {_pct(n_udog_alert, n_alert)}")
    if post:
        print(f"    Mean edge on alertable picks    : {post['mean']:.1%}")
        print(f"    Median edge on alertable picks  : {post['median']:.1%}")
    print()
    print(f"  Fixes currently active:")
    print(f"    [x] HIGH confidence gate (edge>=12%, gap>=8pp)")
    print(f"    [x] DATA_AVAILABILITY_CAP = 0.55")
    print(f"    [x] SHRINK_ALPHA = 0.70  (market shrink)")
    print(f"    [x] Ranking-anchored ELO fallback -> market-implied when both mp==0")
    print(f"    [x] log1p tournament_exp compression")
    print(f"    [x] Longshot guard (market_prob < 15% -> watchlist)")
    print(f"    [x] Underdog alert threshold (Step 6c)  *** NEW")
    print()
    print(f"{SEP}\n")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run_audit()
