"""
diagnostics/edge_distribution_audit.py
=======================================
Read-only audit of edge distribution from data/predictions.json.

Run from the parent directory of tennis_model/:
    python tennis_model/diagnostics/edge_distribution_audit.py [--save] [--simulate-shrink]

Flags:
    --save             Write results to diagnostics/edge_audit_results.json and
                       diagnostics/edge_audit_top20.csv
    --simulate-shrink  Apply the market-shrink transform (alpha=0.70) retroactively
                       to stored edges to show what the distribution looks like
                       post-shrink without re-running the full pipeline.
                       Mathematical basis: edge_new = alpha * edge_old exactly,
                       because shrink_toward_market(p, o) = alpha*p + (1-alpha)/o
                       => edge_new = o*(alpha*p + (1-alpha)/o) - 1
                                   = alpha*(o*p - 1) = alpha * edge_old.

Data source:
    data/predictions.json  (written by backtest.store_prediction on every alert)
    Edge values are stored as decimal fractions: 0.15 = 15%.

Does NOT modify any model, scoring, or production file.
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# PATH SETUP (works whether run as script or imported)
# ---------------------------------------------------------------------------
_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)          # tennis_model/
_PARENT = os.path.dirname(_ROOT)          # Downloads/

PREDICTIONS_FILE = os.path.join(_PARENT, "data", "predictions.json")
OUT_JSON         = os.path.join(_HERE,   "edge_audit_results.json")
OUT_CSV          = os.path.join(_HERE,   "edge_audit_top20.csv")

SAVE             = "--save"             in sys.argv
SIMULATE_SHRINK  = "--simulate-shrink"  in sys.argv
SHRINK_ALPHA     = 0.70   # must match probability_adjustments.SHRINK_ALPHA


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _pct(n, total):
    return 0.0 if total == 0 else round(100.0 * n / total, 1)


def _percentile(sorted_vals, p):
    """Nearest-rank percentile from a sorted list."""
    if not sorted_vals:
        return None
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]


def _pick_edge(pred: dict, simulate_shrink: bool = False) -> float | None:
    """Return the decimal edge on the picked side (or None if unavailable).

    If simulate_shrink=True, applies the alpha=0.70 factor retroactively.
    This is exact: edge_new = SHRINK_ALPHA * edge_old (see module docstring).
    """
    if pred["pick"] == pred["player_a"]:
        raw = pred.get("edge_a")
    elif pred["pick"] == pred["player_b"]:
        raw = pred.get("edge_b")
    else:
        return None
    if raw is None:
        return None
    return round(SHRINK_ALPHA * raw, 6) if simulate_shrink else raw


def _is_favorite(pred: dict) -> bool | None:
    """True if the picked side is the favorite (lower market odds)."""
    oa = pred.get("best_odds_a") or pred.get("pick_odds")
    ob = pred.get("best_odds_b") or pred.get("pick_odds")
    if oa is None or ob is None:
        return None
    pick_odds = pred.get("pick_odds")
    if pick_odds is None:
        return None
    return pick_odds <= min(oa, ob)


def _edge_counts(edges, thresholds=(0.05, 0.10, 0.15, 0.20, 0.25, 0.30)):
    total = len(edges)
    return {
        f">{int(t*100)}%": {
            "count": sum(1 for e in edges if e > t),
            "pct":   _pct(sum(1 for e in edges if e > t), total),
        }
        for t in thresholds
    }


def _dist_stats(edges: list[float]) -> dict:
    if not edges:
        return {"n": 0}
    s = sorted(edges)
    n = len(s)
    mean   = round(sum(s) / n, 4)
    median = _percentile(s, 50)
    return {
        "n":    n,
        "mean": mean,
        "median": median,
        "p50":  _percentile(s, 50),
        "p75":  _percentile(s, 75),
        "p90":  _percentile(s, 90),
        "p95":  _percentile(s, 95),
        "p99":  _percentile(s, 99),
        "min":  s[0],
        "max":  s[-1],
        "counts": _edge_counts(s),
    }


# ---------------------------------------------------------------------------
# RED-FLAG LOGIC  (diagnostics only — no production effect)
# ---------------------------------------------------------------------------

def _red_flags(report: dict) -> list[str]:
    flags = []
    overall = report.get("overall", {})
    n = overall.get("n", 0)

    if n == 0:
        flags.append("NO DATA: predictions.json is empty or absent — "
                     "run the model and record picks first")
        return flags

    # Flag: suspiciously high proportion of edges > 20%
    over20_pct = overall.get("counts", {}).get(">20%", {}).get("pct", 0)
    if over20_pct > 15:
        flags.append(
            f"RED FLAG - {over20_pct:.1f}% of edges exceed 20% "
            f"(expected <5-10% in a calibrated model)"
        )

    # Flag: WTA tail much fatter than ATP
    wta_p95 = report.get("by_tour", {}).get("WTA", {}).get("p95")
    atp_p95 = report.get("by_tour", {}).get("ATP", {}).get("p95")
    if wta_p95 is not None and atp_p95 is not None:
        if wta_p95 > atp_p95 * 1.5:
            flags.append(
                f"RED FLAG - WTA edge tail much fatter than ATP "
                f"(WTA p95={wta_p95:.1%} vs ATP p95={atp_p95:.1%}): "
                f"WTA model may be systematically overconfident"
            )

    # Flag: underdog tail much fatter than favorite tail
    udog_p90 = report.get("by_side", {}).get("underdog", {}).get("p90")
    fav_p90  = report.get("by_side", {}).get("favorite", {}).get("p90")
    if udog_p90 is not None and fav_p90 is not None:
        if udog_p90 > fav_p90 * 1.5:
            flags.append(
                f"RED FLAG - Underdog edge tail much fatter than favorite tail "
                f"(udog p90={udog_p90:.1%} vs fav p90={fav_p90:.1%}): "
                f"model may be generating inflated underdog probabilities"
            )

    # Flag: HIGH confidence not materially different from MEDIUM in edge distribution
    high_med  = report.get("by_confidence", {}).get("HIGH", {}).get("median")
    med_med   = report.get("by_confidence", {}).get("MEDIUM", {}).get("median")
    if high_med is not None and med_med is not None:
        if abs(high_med - med_med) < 0.02:
            flags.append(
                f"RED FLAG - HIGH confidence median edge ({high_med:.1%}) "
                f"not materially different from MEDIUM ({med_med:.1%}): "
                f"confidence tiers may not be filtering on pick quality"
            )

    # Flag: mean edge very high overall
    mean_edge = overall.get("mean", 0)
    if mean_edge > 0.15:
        flags.append(
            f"RED FLAG - Mean edge {mean_edge:.1%} is very high "
            f"(expected 5-12% in a realistic model): likely model miscalibration"
        )

    if not flags:
        flags.append("No red flags detected in this sample")

    return flags


# ---------------------------------------------------------------------------
# MAIN AUDIT
# ---------------------------------------------------------------------------

def run_audit() -> dict:
    sep = "=" * 66
    sim = SIMULATE_SHRINK

    print(f"\n{sep}")
    if sim:
        print("  EDGE DISTRIBUTION AUDIT  [SIMULATE-SHRINK mode: alpha=0.70]")
    else:
        print("  EDGE DISTRIBUTION AUDIT")
    print(f"{sep}\n")

    # ── Load data ──────────────────────────────────────────────────────────
    if not os.path.exists(PREDICTIONS_FILE):
        print(f"  DATA SOURCE : {PREDICTIONS_FILE}")
        print(f"  STATUS      : FILE NOT FOUND — no picks have been stored yet")
        print(f"\n  The model has not yet fired any alerts that triggered")
        print(f"  backtest.store_prediction().  Run the scanner and record")
        print(f"  results first, then re-run this audit.")
        print(f"\n{sep}\n")
        return {"error": "no_predictions_file", "n": 0}

    with open(PREDICTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    all_preds = data.get("predictions", [])
    print(f"  DATA SOURCE : {PREDICTIONS_FILE}")
    print(f"  Total stored predictions : {len(all_preds)}")

    # Filter to picks with usable edges
    usable = [
        p for p in all_preds
        if p.get("pick") and _pick_edge(p) is not None
    ]
    missing_edge = len(all_preds) - len(usable)
    if missing_edge > 0:
        print(f"  Predictions with missing edge data : {missing_edge} (excluded)")
    if sim:
        print(f"  Edge transform                     : stored_edge x {SHRINK_ALPHA} (retroactive shrink)")
    print(f"  Predictions used for edge audit    : {len(usable)}\n")

    if not usable:
        print("  No usable predictions. Audit cannot continue.\n")
        print(f"{sep}\n")
        return {"error": "no_usable_predictions", "n": 0}

    # ── Extract edges (with optional retroactive shrink) ────────────────────
    def _e(p):
        return _pick_edge(p, simulate_shrink=sim)

    edges_all  = [_e(p) for p in usable]

    # ── Split by tour ───────────────────────────────────────────────────────
    atp_edges  = [_e(p) for p in usable if (p.get("tour") or "").upper() == "ATP"]
    wta_edges  = [_e(p) for p in usable if (p.get("tour") or "").upper() == "WTA"]

    # ── Split by favorite / underdog ────────────────────────────────────────
    fav_edges  = [_e(p) for p in usable if _is_favorite(p) is True]
    udog_edges = [_e(p) for p in usable if _is_favorite(p) is False]

    # ── Split by confidence ─────────────────────────────────────────────────
    conf_edges = {}
    for tier in ("HIGH", "MEDIUM", "LOW"):
        conf_edges[tier] = [
            _e(p) for p in usable
            if (p.get("confidence") or "").upper() == tier
        ]

    # ── Build report ────────────────────────────────────────────────────────
    report = {
        "n_total":      len(all_preds),
        "n_usable":     len(usable),
        "simulate_shrink": sim,
        "shrink_alpha": SHRINK_ALPHA if sim else None,
        "overall":      _dist_stats(edges_all),
        "by_tour": {
            "ATP": _dist_stats(atp_edges),
            "WTA": _dist_stats(wta_edges),
        },
        "by_side": {
            "favorite": _dist_stats(fav_edges),
            "underdog": _dist_stats(udog_edges),
        },
        "by_confidence": {t: _dist_stats(v) for t, v in conf_edges.items()},
    }

    # ── Top 20 edges ────────────────────────────────────────────────────────
    top20 = sorted(usable, key=lambda p: _e(p), reverse=True)[:20]

    # ── WTA concentration check ─────────────────────────────────────────────
    wta_top5_count  = sum(1 for p in top20[:5]  if (p.get("tour") or "").upper() == "WTA")
    udog_top5_count = sum(1 for p in top20[:5]  if _is_favorite(p) is False)

    # ── Red flags ───────────────────────────────────────────────────────────
    flags = _red_flags(report)
    report["red_flags"] = flags

    # ── Print ───────────────────────────────────────────────────────────────
    def _fmt_stats(label, stats):
        if stats.get("n", 0) == 0:
            print(f"  {label:<20}  n=0  (no data)")
            return
        n   = stats["n"]
        c   = stats["counts"]
        print(f"  {label:<20}  n={n}")
        print(f"    mean={stats['mean']:.1%}  median={stats['median']:.1%}"
              f"  p75={stats['p75']:.1%}  p90={stats['p90']:.1%}"
              f"  p95={stats['p95']:.1%}  p99={stats['p99']:.1%}")
        print(f"    min={stats['min']:.1%}  max={stats['max']:.1%}")
        parts = []
        for k, v in c.items():
            parts.append(f"{k}: {v['count']} ({v['pct']}%)")
        print(f"    Edge thresholds: {' | '.join(parts)}")

    print(f"{sep}")
    print("1. OVERALL DISTRIBUTION")
    print(f"{sep}")
    _fmt_stats("ALL PICKS", report["overall"])

    print(f"\n{sep}")
    print("2. BY TOUR")
    print(f"{sep}")
    _fmt_stats("ATP", report["by_tour"]["ATP"])
    _fmt_stats("WTA", report["by_tour"]["WTA"])

    print(f"\n{sep}")
    print("3. BY SIDE (favorite vs underdog)")
    print(f"{sep}")
    _fmt_stats("FAVORITE", report["by_side"]["favorite"])
    _fmt_stats("UNDERDOG", report["by_side"]["underdog"])

    print(f"\n{sep}")
    print("4. BY CONFIDENCE TIER")
    print(f"{sep}")
    for tier in ("HIGH", "MEDIUM", "LOW"):
        _fmt_stats(tier, report["by_confidence"][tier])

    print(f"\n{sep}")
    print("5. TOP 20 LARGEST EDGES")
    print(f"{sep}")
    print(f"  {'#':<3}  {'Edge':>6}  {'Pick':<22}  {'Odds':>5}  {'Tour':<4}  {'Conf':<7}  Match")
    print(f"  {'-'*3}  {'-'*6}  {'-'*22}  {'-'*5}  {'-'*4}  {'-'*7}  {'-'*30}")
    for i, p in enumerate(top20, 1):
        e     = _e(p)
        pick  = (p.get("pick") or "")[:22]
        odds  = p.get("pick_odds") or 0
        tour  = (p.get("tour") or "?")[:4]
        conf  = (p.get("confidence") or "?")[:7]
        match = (p.get("match") or "")[:30]
        print(f"  {i:<3}  {e:>6.1%}  {pick:<22}  {odds:>5.2f}  {tour:<4}  {conf:<7}  {match}")

    print(f"\n{sep}")
    print("6. CONCENTRATION ANALYSIS (top 5 edges)")
    print(f"{sep}")
    print(f"  WTA in top-5 largest edges  : {wta_top5_count}/5")
    print(f"  Underdogs in top-5 largest  : {udog_top5_count}/5")

    # Concentration by tournament
    tourn_counter: dict = {}
    for p in top20:
        t = p.get("tournament") or "Unknown"
        tourn_counter[t] = tourn_counter.get(t, 0) + 1
    print(f"  Tournament distribution (top 20):")
    for t, cnt in sorted(tourn_counter.items(), key=lambda x: -x[1]):
        print(f"    {t:<35} : {cnt}")

    # Surface
    surf_counter: dict = {}
    for p in top20:
        s = p.get("surface") or "Unknown"
        surf_counter[s] = surf_counter.get(s, 0) + 1
    print(f"  Surface distribution (top 20):")
    for s, cnt in sorted(surf_counter.items(), key=lambda x: -x[1]):
        print(f"    {s:<10} : {cnt}")

    print(f"\n{sep}")
    print("7. RED FLAGS")
    print(f"{sep}")
    for flag in flags:
        prefix = "  [!] " if flag.startswith("RED FLAG") else "  [ok]"
        print(f"{prefix} {flag}")

    # ── Before/after comparison (only in simulate-shrink mode) ─────────────
    if sim:
        print(f"\n{sep}")
        print("8. BEFORE / AFTER COMPARISON  (pre-shrink  vs  post-shrink alpha=0.70)")
        print(f"{sep}")

        # Recompute pre-shrink stats from the same usable set
        pre_edges   = [_pick_edge(p, simulate_shrink=False) for p in usable]
        pre_stats   = _dist_stats(pre_edges)
        post_stats  = report["overall"]

        def _delta(pre, post, key):
            a, b = pre.get(key), post.get(key)
            if a is None or b is None:
                return "n/a"
            return f"{b - a:+.1%}"

        rows = [
            ("mean",   "Mean edge"),
            ("median", "Median edge"),
            ("p75",    "p75"),
            ("p90",    "p90"),
            ("p95",    "p95"),
            ("p99",    "p99"),
            ("max",    "Max edge"),
        ]
        print(f"  {'Metric':<12}  {'Pre-shrink':>10}  {'Post-shrink':>11}  {'Delta':>8}")
        print(f"  {'-'*12}  {'-'*10}  {'-'*11}  {'-'*8}")
        for key, label in rows:
            pre_v  = pre_stats.get(key)
            post_v = post_stats.get(key)
            pre_s  = f"{pre_v:.1%}"  if pre_v  is not None else "n/a"
            post_s = f"{post_v:.1%}" if post_v is not None else "n/a"
            print(f"  {label:<12}  {pre_s:>10}  {post_s:>11}  {_delta(pre_stats, post_stats, key):>8}")

        # Threshold counts comparison
        pre_c  = pre_stats.get("counts", {})
        post_c = post_stats.get("counts", {})
        print(f"\n  {'Threshold':<12}  {'Pre count':>10}  {'Post count':>11}  {'Pre %':>7}  {'Post %':>7}")
        print(f"  {'-'*12}  {'-'*10}  {'-'*11}  {'-'*7}  {'-'*7}")
        for t_key in (">5%", ">10%", ">15%", ">20%", ">25%", ">30%"):
            pc = pre_c.get(t_key, {})
            qc = post_c.get(t_key, {})
            print(f"  {t_key:<12}  {pc.get('count',0):>10}  {qc.get('count',0):>11}  "
                  f"{pc.get('pct',0):>6.1f}%  {qc.get('pct',0):>6.1f}%")

        # Underdog proportion unchanged — note it explicitly
        pre_udog_n  = sum(1 for p in usable if _is_favorite(p) is False)
        pct_udog    = _pct(pre_udog_n, len(usable))
        print(f"\n  Underdog picks: {pre_udog_n}/{len(usable)} ({pct_udog}%) "
              f"[unchanged - shrink reduces edge size, not side selection]")

    print(f"\n{sep}\n")

    # ── Optional save ───────────────────────────────────────────────────────
    if SAVE:
        os.makedirs(_HERE, exist_ok=True)

        # JSON report (serialise all numeric values)
        def _make_serialisable(obj):
            if isinstance(obj, dict):
                return {k: _make_serialisable(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_make_serialisable(v) for v in obj]
            if isinstance(obj, float):
                return round(obj, 6)
            return obj

        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(_make_serialisable(report), f, indent=2, ensure_ascii=False)
        print(f"  Saved JSON  : {OUT_JSON}")

        # CSV top-20
        import csv
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "edge_pct", "pick", "pick_odds", "tour",
                        "confidence", "surface", "tournament", "match", "date"])
            for i, p in enumerate(top20, 1):
                w.writerow([
                    i,
                    f"{_e(p):.4f}",
                    p.get("pick", ""),
                    p.get("pick_odds", ""),
                    p.get("tour", ""),
                    p.get("confidence", ""),
                    p.get("surface", ""),
                    p.get("tournament", ""),
                    p.get("match", ""),
                    p.get("date", ""),
                ])
        print(f"  Saved CSV   : {OUT_CSV}\n")

    return report


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_audit()
