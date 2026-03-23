"""
tennis_model/reporting/calibration.py
======================================
Professional calibration reporting.

Segments settled predictions by tour, odds bucket, confidence, edge bucket,
surface, and all pairwise combinations.  Tracks CLV separately when available.

Usage:
    from tennis_model.reporting.calibration import compute_calibration, print_calibration
    report = compute_calibration(predictions)
    print_calibration(report)
"""
from __future__ import annotations

import logging
from collections import defaultdict

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# BUCKET DEFINITIONS
# ──────────────────────────────────────────────────────────────────────────────

ODDS_BUCKET_ORDER = ["<=1.60", "1.60-1.80", "1.80-2.00", "2.00-2.40", "2.40-3.00", ">3.00"]
EDGE_BUCKET_ORDER = ["<4%", "4-6%", "6-8%", "8-12%", ">12%"]
CONF_ORDER        = ["VERY HIGH", "HIGH", "MEDIUM", "LOW"]
SURFACE_ORDER     = ["Hard", "Clay", "Grass"]
TOUR_ORDER        = ["ATP", "WTA", "CHALLENGER"]


def odds_bucket(odds: float) -> str:
    """Classify decimal odds into display bucket."""
    if odds <= 1.60: return "<=1.60"
    if odds <= 1.80: return "1.60-1.80"
    if odds <= 2.00: return "1.80-2.00"
    if odds <= 2.40: return "2.00-2.40"
    if odds <= 3.00: return "2.40-3.00"
    return ">3.00"


def edge_bucket(edge_pct: float) -> str:
    """Classify edge (in percent, e.g. 7.1) into display bucket."""
    if edge_pct < 4.0:  return "<4%"
    if edge_pct < 6.0:  return "4-6%"
    if edge_pct < 8.0:  return "6-8%"
    if edge_pct < 12.0: return "8-12%"
    return ">12%"


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _pick_edge_pct(p: dict) -> float:
    """Pick-side edge in percent.
    Predictions store edge as decimal (0.071 = 7.1%) — convert to percent here.
    """
    raw = p["edge_a"] if p["pick"] == p["player_a"] else (p.get("edge_b") or 0.0)
    return (raw or 0.0) * 100.0


def _segment_stats(bets: list) -> dict | None:
    """Aggregate stats for a group of settled predictions."""
    n = len(bets)
    if n == 0:
        return None
    wins   = sum(1 for p in bets if p["pick"] == p["winner"])
    profit = sum(p["profit_loss"] for p in bets)
    odds_v = [p["pick_odds"] for p in bets if p.get("pick_odds")]
    edge_v = [_pick_edge_pct(p) for p in bets]
    clv_v  = [p["clv"] for p in bets if p.get("clv") is not None]
    return {
        "count":             n,
        "wins":              wins,
        "losses":            n - wins,
        "hit_rate":          wins / n,
        "profit":            profit,
        "roi":               profit / n,
        "avg_odds":          sum(odds_v) / len(odds_v) if odds_v else None,
        "avg_edge":          sum(edge_v) / n,
        "avg_clv":           sum(clv_v) / len(clv_v) if clv_v else None,
        "positive_clv_rate": (sum(1 for c in clv_v if c > 0) / len(clv_v)) if clv_v else None,
        "clv_count":         len(clv_v),
    }


def _breakdown(bets: list, key_fn) -> dict:
    """Single-dimension breakdown."""
    groups: dict = defaultdict(list)
    for p in bets:
        groups[key_fn(p)].append(p)
    return {k: _segment_stats(v) for k, v in groups.items() if v}


def _breakdown2(bets: list, key_a, key_b) -> dict:
    """Two-dimensional breakdown.  Keys are (primary, secondary) tuples."""
    groups: dict = defaultdict(list)
    for p in bets:
        groups[(key_a(p), key_b(p))].append(p)
    return {k: _segment_stats(v) for k, v in groups.items() if v}


# ──────────────────────────────────────────────────────────────────────────────
# COMPUTE
# ──────────────────────────────────────────────────────────────────────────────

def compute_calibration(predictions: list) -> dict:
    """
    Compute full calibration report from all stored predictions.
    Only settled predictions (result != None) are included.

    Returns a dict ready for print_calibration().
    """
    settled = [p for p in predictions if p.get("result") is not None]
    if not settled:
        return {"total": 0}

    def _tour(p):  return (p.get("tour") or "Unknown").upper()
    def _conf(p):  return (p.get("confidence") or "Unknown").upper()
    def _surf(p):  return (p.get("surface") or "Unknown").title()
    def _odds(p):  return odds_bucket(p.get("pick_odds") or 2.0)
    def _edge(p):  return edge_bucket(_pick_edge_pct(p))

    return {
        "total":              len(settled),
        "overall":            _segment_stats(settled),
        "by_tour":            _breakdown(settled, _tour),
        "by_odds":            _breakdown(settled, _odds),
        "by_confidence":      _breakdown(settled, _conf),
        "by_edge":            _breakdown(settled, _edge),
        "by_surface":         _breakdown(settled, _surf),
        "by_tour_odds":       _breakdown2(settled, _tour, _odds),
        "by_tour_confidence": _breakdown2(settled, _tour, _conf),
        "by_conf_edge":       _breakdown2(settled, _conf, _edge),
    }


# ──────────────────────────────────────────────────────────────────────────────
# PRINT HELPERS
# ──────────────────────────────────────────────────────────────────────────────

_SEP_W   = 80
_COL_SG  = 20   # segment label column width


def _sep(char: str = "-", width: int = _SEP_W) -> str:
    return char * width


def _hdr() -> str:
    return (
        f"  {'Segment':<{_COL_SG}}"
        f"  {'N':>5}"
        f"  {'W/L':>7}"
        f"  {'Hit%':>6}"
        f"  {'ROI':>7}"
        f"  {'AvgEdge':>8}"
        f"  {'AvgCLV':>7}"
        f"  {'+CLV%':>6}"
    )


def _row(label: str, s: dict | None) -> str | None:
    """Format one stats row.  Returns None if stats is None."""
    if s is None:
        return None
    w_l  = f"{s['wins']}/{s['losses']}"
    clv  = f"{s['avg_clv']:+.1%}" if s.get("avg_clv") is not None else "  --  "
    pclv = f"{s['positive_clv_rate']:.0%}" if s.get("positive_clv_rate") is not None else " -- "
    edge = f"{s['avg_edge']:>+7.1f}%"
    return (
        f"  {label[:_COL_SG]:<{_COL_SG}}"
        f"  {s['count']:>5}"
        f"  {w_l:>7}"
        f"  {s['hit_rate']:>6.1%}"
        f"  {s['roi']:>+7.1%}"
        f"  {edge}"
        f"  {clv:>7}"
        f"  {pclv:>6}"
    )


def _ordered_keys(data: dict, order: list | None) -> list:
    """Return dict keys in preferred order, appending any extras alphabetically."""
    if order:
        return [k for k in order if k in data] + sorted(k for k in data if k not in order)
    return sorted(data.keys())


def _table(title: str, data: dict, order: list | None = None) -> None:
    """Print a single-dimension calibration table."""
    if not data:
        return
    print(f"\n  {title}")
    print(f"  {_sep()}")
    print(_hdr())
    print(f"  {_sep()}")
    for k in _ordered_keys(data, order):
        r = _row(str(k), data.get(k))
        if r:
            print(r)
    print(f"  {_sep()}")


def _table2(title: str, data: dict,
            primary_order: list | None = None,
            secondary_order: list | None = None) -> None:
    """Print a two-dimensional calibration table (primary → section, secondary → rows)."""
    if not data:
        return
    primaries = _ordered_keys(dict.fromkeys(k[0] for k in data), primary_order)
    print(f"\n  {title}")
    for pk in primaries:
        sub = {k[1]: v for k, v in data.items() if k[0] == pk}
        if not sub:
            continue
        print(f"  {_sep()}")
        print(f"  {pk}")
        print(_hdr())
        print(f"  {_sep('-', _SEP_W)}")
        for sk in _ordered_keys(sub, secondary_order):
            r = _row(f"  {sk}", sub.get(sk))
            if r:
                print(r)
    print(f"  {_sep()}")


# ──────────────────────────────────────────────────────────────────────────────
# WARNINGS
# ──────────────────────────────────────────────────────────────────────────────

def _emit_warnings(report: dict) -> None:
    """Flag model health issues to stdout."""
    issues = []

    high = (report.get("by_confidence") or {}).get("HIGH")
    if high and high["count"] >= 10:
        if high["hit_rate"] < 0.55:
            issues.append(
                f"HIGH confidence win rate {high['hit_rate']:.1%} < 55%"
                f"  ({high['count']} bets)"
            )
        if high.get("avg_clv") is not None and high["avg_clv"] < 0:
            issues.append(
                f"HIGH confidence avg CLV {high['avg_clv']:+.1%} is negative"
                f"  ({high['clv_count']} bets with CLV)"
            )

    overall = report.get("overall") or {}
    if overall.get("avg_clv") is not None and overall["avg_clv"] < 0:
        issues.append(
            f"Overall avg CLV {overall['avg_clv']:+.1%} -- model may not beat closing line"
            f"  ({overall['clv_count']} bets tracked)"
        )
    if overall.get("count", 0) >= 20 and overall.get("roi", 0) < -0.10:
        issues.append(
            f"Overall ROI {overall['roi']:+.1%} below -10% -- check model calibration"
        )

    if issues:
        print(f"\n  {'!' * 60}")
        print(f"  MODEL HEALTH WARNINGS")
        for w in issues:
            print(f"  [!] {w}")
        print(f"  {'!' * 60}")


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def print_calibration(report: dict) -> None:
    """Print full calibration report to stdout."""
    if report.get("total", 0) == 0:
        print("\n  No settled predictions for calibration.")
        return

    overall = report["overall"]
    n_clv   = overall.get("clv_count", 0)
    clv_note = (
        f"  CLV tracked on {n_clv} / {overall['count']} bets"
        if n_clv > 0
        else "  CLV: not yet tracked -- use --closing-odds <id> <odds_a> <odds_b>"
    )

    sep55 = "=" * 55
    print(f"\n{sep55}")
    print(f"  CALIBRATION REPORT  ({overall['count']} settled bets)")
    print(f"{sep55}")
    print(clv_note)

    # ── Single-dimension tables ─────────────────────────────────────────────
    _table("A) BY TOUR",        report.get("by_tour", {}),       order=TOUR_ORDER)
    _table("B) BY ODDS BUCKET", report.get("by_odds", {}),       order=ODDS_BUCKET_ORDER)
    _table("C) BY CONFIDENCE",  report.get("by_confidence", {}), order=CONF_ORDER)
    _table("D) BY EDGE BUCKET", report.get("by_edge", {}),       order=EDGE_BUCKET_ORDER)
    _table("H) BY SURFACE",     report.get("by_surface", {}),    order=SURFACE_ORDER)

    # ── Two-dimensional tables ──────────────────────────────────────────────
    _table2(
        "E) BY TOUR + ODDS BUCKET",
        report.get("by_tour_odds", {}),
        primary_order=TOUR_ORDER,
        secondary_order=ODDS_BUCKET_ORDER,
    )
    _table2(
        "F) BY TOUR + CONFIDENCE",
        report.get("by_tour_confidence", {}),
        primary_order=TOUR_ORDER,
        secondary_order=CONF_ORDER,
    )
    _table2(
        "G) BY CONFIDENCE + EDGE BUCKET",
        report.get("by_conf_edge", {}),
        primary_order=CONF_ORDER,
        secondary_order=EDGE_BUCKET_ORDER,
    )

    # ── Warnings ────────────────────────────────────────────────────────────
    _emit_warnings(report)

    print(f"\n{sep55}\n")
