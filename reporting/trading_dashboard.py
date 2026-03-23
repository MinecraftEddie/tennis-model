"""
reporting/trading_dashboard.py
================================
All-time trading-grade performance dashboard.

Reads:
    data/settled_predictions.jsonl   — settled bets (P&L, bankroll, CLV)
    data/forward_predictions.jsonl   — all evaluated matches (stake dist, activity)
    data/predictions.json            — legacy store (supplemental; optional)

Usage:
    python -m tennis_model.reporting.trading_dashboard
    python -m tennis_model.reporting.trading_dashboard --json reports/dashboard.json
    python -m tennis_model.reporting.trading_dashboard --date 2026-03-21

Output: terminal summary + optional JSON export.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_DATA_DIR      = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
_SETTLED_FILE  = os.path.join(_DATA_DIR, "settled_predictions.jsonl")
_FORWARD_FILE  = os.path.join(_DATA_DIR, "forward_predictions.jsonl")

_W   = 64
_SEP = "=" * _W
_DIV = "-" * _W
_COL = "-" * 40


# ──────────────────────────────────────────────────────────────────────────────
# I / O
# ──────────────────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return out


def _enrich_settled(settled: list, fwd_index: dict) -> list:
    """Merge tour / surface / stake_units from forward record when absent in settled."""
    enriched = []
    for r in settled:
        rec = dict(r)
        f = fwd_index.get(rec.get("match_id"), {})
        if f:
            rec.setdefault("tour",        f.get("tour"))
            rec.setdefault("surface",     f.get("surface"))
            rec.setdefault("tournament",  f.get("tournament"))
            if rec.get("stake_units") is None and f.get("stake_units") is not None:
                rec["stake_units"] = f["stake_units"]
        enriched.append(rec)
    return enriched


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _pick_edge(r: dict) -> Optional[float]:
    ps = r.get("picked_side")
    return r.get("edge_a") if ps == "A" else (r.get("edge_b") if ps == "B" else None)


def _pick_odds(r: dict) -> Optional[float]:
    return r.get("settled_odds") or (
        r.get("odds_a") if r.get("picked_side") == "A" else r.get("odds_b")
    )


def _odds_bucket(odds: Optional[float]) -> Optional[str]:
    if odds is None:
        return None
    if odds < 1.5:   return "<1.50"
    if odds < 2.0:   return "1.50-2.00"
    if odds < 2.5:   return "2.00-2.50"
    if odds < 3.0:   return "2.50-3.00"
    if odds < 4.0:   return "3.00-4.00"
    return "4.00+"


def _fmt_pnl(v: float) -> str:
    return f"+{v:.3f}u" if v >= 0 else f"{v:.3f}u"


def _fmt_roi(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"


def _fmt_wr(v: Optional[float]) -> str:
    return f"{v:.1%}" if v is not None else "N/A"


def _fmt_br(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "N/A"


def _pct(n: int, d: int) -> str:
    return f"{n/d*100:.1f}%" if d else "N/A"


def _med(vals: list) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _parse_ts(r: dict) -> Optional[datetime]:
    ts = r.get("settled_at") or r.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CORE COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute(date_filter: Optional[str] = None) -> Dict[str, Any]:
    settled_raw = _load_jsonl(_SETTLED_FILE)
    forward_raw = _load_jsonl(_FORWARD_FILE)

    fwd_index = {r["match_id"]: r for r in forward_raw if "match_id" in r}
    settled   = _enrich_settled(settled_raw, fwd_index)

    if date_filter:
        settled     = [r for r in settled     if r.get("date") == date_filter]
        forward_raw = [r for r in forward_raw if r.get("date") == date_filter]

    # ── categorise settled records ────────────────────────────────────────────
    real_bets   = [r for r in settled if r.get("is_pick") and
                   r.get("result") in ("WIN", "LOSS")]
    wins        = [r for r in real_bets if r["result"] == "WIN"]
    losses      = [r for r in real_bets if r["result"] == "LOSS"]
    voids       = [r for r in settled  if r.get("is_pick") and r.get("result") == "VOID"]
    unsettled   = [r for r in settled  if r.get("is_pick") and r.get("result") == "UNSETTLED"]

    n   = len(real_bets)
    nw  = len(wins)
    nl  = len(losses)

    # ── P & L ─────────────────────────────────────────────────────────────────
    total_pnl    = sum(r.get("pnl_units", 0.0) for r in real_bets)
    stakes       = [r["stake_units"] for r in real_bets if r.get("stake_units") is not None]
    total_staked = sum(stakes) if stakes else float(n)   # fallback: 1u/bet
    roi          = (total_pnl / total_staked * 100) if total_staked else None

    # ── bankroll ──────────────────────────────────────────────────────────────
    from tennis_model.config.runtime_config import BANKROLL_START
    br_start_env = BANKROLL_START
    br_records   = [r for r in settled if r.get("bankroll_after") is not None]
    br_records_s = sorted(br_records, key=lambda r: _parse_ts(r) or datetime.min.replace(tzinfo=timezone.utc))
    bankroll_start   = br_records_s[0]["bankroll_before"] if br_records_s else None
    bankroll_current = br_records_s[-1]["bankroll_after"]  if br_records_s else None
    bankroll_change  = (bankroll_current - bankroll_start) if (
        bankroll_current is not None and bankroll_start is not None) else None

    # ── max drawdown ─────────────────────────────────────────────────────────
    # Build equity curve from sorted real bets (use pnl_units, cumulative)
    sorted_bets = sorted(real_bets, key=lambda r: (_parse_ts(r) or datetime.min.replace(tzinfo=timezone.utc), r.get("date", "")))
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for r in sorted_bets:
        equity += r.get("pnl_units", 0.0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # ── edge & CLV ────────────────────────────────────────────────────────────
    edges     = [e for r in real_bets for e in [_pick_edge(r)] if e is not None]
    avg_edge  = (sum(edges) / len(edges)) if edges else None

    clv_vals  = [r["clv_percent"] for r in real_bets if r.get("clv_percent") is not None]
    avg_clv   = (sum(clv_vals) / len(clv_vals)) if clv_vals else None
    pos_clv_r = (sum(1 for v in clv_vals if v > 0) / len(clv_vals)) if clv_vals else None

    # ── stake distribution (from forward picks with stake_units set) ──────────
    fwd_picks     = [r for r in forward_raw if r.get("is_pick") and
                     r.get("stake_units") is not None]
    fwd_stakes    = [r["stake_units"] for r in fwd_picks]
    stake_avg     = (sum(fwd_stakes) / len(fwd_stakes)) if fwd_stakes else None
    stake_med     = _med(fwd_stakes)
    stake_max     = max(fwd_stakes) if fwd_stakes else None
    stake_min_val = min(fwd_stakes) if fwd_stakes else None

    stake_buckets: Dict[str, int] = defaultdict(int)
    for s in fwd_stakes:
        if s <= 0.05:   stake_buckets["<=0.05"] += 1
        elif s <= 0.10: stake_buckets["0.05-0.10"] += 1
        elif s <= 0.15: stake_buckets["0.10-0.15"] += 1
        elif s <= 0.20: stake_buckets["0.15-0.20"] += 1
        elif s <= 0.25: stake_buckets["0.20-0.25"] += 1
        else:           stake_buckets[">0.25"] += 1

    # ── segmented breakdowns ──────────────────────────────────────────────────
    def seg(group: list) -> dict:
        ng = len(group)
        if ng == 0:
            return {"n": 0, "w": 0, "l": 0, "pnl": 0.0,
                    "win_rate": None, "roi": None, "avg_edge": None, "avg_odds": None}
        gw   = sum(1 for r in group if r["result"] == "WIN")
        gl   = sum(1 for r in group if r["result"] == "LOSS")
        gpnl = sum(r.get("pnl_units", 0.0) for r in group)
        gst  = [r["stake_units"] for r in group if r.get("stake_units") is not None]
        gstk = sum(gst) if gst else float(ng)
        ged  = [e for r in group for e in [_pick_edge(r)] if e is not None]
        god  = [r["settled_odds"] for r in group if r.get("settled_odds")]
        return {
            "n":        ng,
            "w":        gw,
            "l":        gl,
            "pnl":      round(gpnl, 4),
            "win_rate": gw / ng,
            "roi":      round(gpnl / gstk * 100, 2) if gstk else None,
            "avg_edge": round(sum(ged) / len(ged), 2) if ged else None,
            "avg_odds": round(sum(god) / len(god), 3) if god else None,
        }

    def breakdown(key_fn) -> dict:
        groups: dict = defaultdict(list)
        for r in real_bets:
            k = key_fn(r)
            if k:
                groups[k].append(r)
        return {k: seg(v) for k, v in groups.items()}

    by_tour    = breakdown(lambda r: r.get("tour"))
    by_surface = breakdown(lambda r: r.get("surface"))
    by_conf    = breakdown(lambda r: r.get("confidence"))
    by_quality = breakdown(lambda r: r.get("quality_tier"))
    by_odds    = breakdown(lambda r: _odds_bucket(_pick_odds(r)))

    # ── activity counts (from forward file) ───────────────────────────────────
    fwd_picks_all   = [r for r in forward_raw if r.get("is_pick")]
    fwd_blocked     = [r for r in forward_raw if not r.get("is_pick")]
    fwd_watchlist   = [r for r in forward_raw
                       if r.get("evaluator_decision") in ("watchlist",)
                       and not r.get("is_pick")]
    # Risk-cap blocks leave no explicit marker in forward records;
    # proxy: is_pick=True but stake_units=None (alert never reached Kelly step)
    fwd_risk_capped = [r for r in fwd_picks_all if r.get("stake_units") is None]

    # ── top 10 best / worst ───────────────────────────────────────────────────
    top10_best  = sorted(real_bets, key=lambda r: r.get("pnl_units", 0.0), reverse=True)[:10]
    top10_worst = sorted(real_bets, key=lambda r: r.get("pnl_units", 0.0))[:10]

    return {
        # raw counts
        "n_real_bets":       n,
        "n_wins":            nw,
        "n_losses":          nl,
        "n_voids":           len(voids),
        "n_unsettled":       len(unsettled),
        # P&L
        "total_pnl":         round(total_pnl, 4),
        "total_staked":      round(total_staked, 4),
        "roi":               round(roi, 2) if roi is not None else None,
        "hit_rate":          (nw / n) if n else None,
        # bankroll
        "bankroll_start":    bankroll_start,
        "bankroll_current":  bankroll_current,
        "bankroll_change":   round(bankroll_change, 4) if bankroll_change is not None else None,
        "bankroll_start_env": br_start_env,
        # risk
        "max_drawdown":      round(max_dd, 4),
        # edge / CLV
        "avg_edge":          round(avg_edge, 2) if avg_edge is not None else None,
        "avg_clv":           round(avg_clv, 2)  if avg_clv  is not None else None,
        "pos_clv_rate":      round(pos_clv_r, 3) if pos_clv_r is not None else None,
        "n_clv_records":     len(clv_vals),
        # stakes
        "stake_avg":         round(stake_avg, 4)     if stake_avg  is not None else None,
        "stake_med":         round(stake_med, 4)     if stake_med  is not None else None,
        "stake_max":         round(stake_max, 4)     if stake_max  is not None else None,
        "stake_min":         round(stake_min_val, 4) if stake_min_val is not None else None,
        "stake_buckets":     dict(stake_buckets),
        "n_kelly_sized":     len(fwd_stakes),
        # breakdowns
        "by_tour":           by_tour,
        "by_surface":        by_surface,
        "by_confidence":     by_conf,
        "by_quality":        by_quality,
        "by_odds_bucket":    by_odds,
        # activity
        "n_fwd_picks":       len(fwd_picks_all),
        "n_blocked":         len(fwd_blocked),
        "n_watchlist":       len(fwd_watchlist),
        "n_risk_capped_est": len(fwd_risk_capped),
        # top / worst
        "top10_best":        top10_best,
        "top10_worst":       top10_worst,
    }


# ──────────────────────────────────────────────────────────────────────────────
# TERMINAL PRINTER
# ──────────────────────────────────────────────────────────────────────────────

def _row(label: str, value: str, width: int = 26) -> None:
    print(f"  {label:<{width}} {value}")


def _seg_line(label: str, s: dict, indent: str = "    ") -> None:
    if s["n"] == 0:
        return
    wr    = _fmt_wr(s["win_rate"])
    roi   = _fmt_roi(s["roi"])
    edge  = f"{s['avg_edge']:+.1f}%" if s["avg_edge"] is not None else "  N/A "
    odds  = f"@{s['avg_odds']:.2f}"  if s["avg_odds"] is not None else "  N/A"
    pnl   = _fmt_pnl(s["pnl"])
    print(f"{indent}{label:<14}  {s['n']:>3}b  "
          f"{s['w']}W/{s['l']}L  wr {wr}  roi {roi:>8}  "
          f"edge {edge}  {odds}  pnl {pnl}")


def _section(title: str) -> None:
    print(f"\n{_DIV}")
    print(f"  {title}")
    print(f"{_DIV}")


def _breakdown_table(title: str, bd: dict, order: Optional[list] = None) -> None:
    if not bd:
        return
    keys = order if order else sorted(bd.keys())
    active = [k for k in keys if k in bd and bd[k]["n"] > 0]
    if not active:
        return
    print(f"\n  {title}")
    print(f"  {_COL}")
    for k in active:
        _seg_line(str(k), bd[k])


def _top10_table(title: str, records: list, reverse: bool = True) -> None:
    if not records:
        return
    _section(title)
    for i, r in enumerate(records, 1):
        ps    = r.get("picked_side")
        name  = r.get("player_a") if ps == "A" else r.get("player_b", "?")
        odds  = r.get("settled_odds")
        pnl   = r.get("pnl_units", 0.0)
        conf  = r.get("confidence", "")
        qt    = r.get("quality_tier", "")
        edge  = _pick_edge(r)
        result= r.get("result", "?")
        tier  = f"{conf}/{qt}".strip("/")
        odds_s = f"@{odds:.2f}" if odds else "     "
        edge_s = f"{edge:+.1f}%" if edge is not None else "  N/A"
        print(f"  {i:>2}. {name:<20} {odds_s}  {result:<4}  "
              f"pnl {_fmt_pnl(pnl):>9}  edge {edge_s}  [{tier}]")


def print_dashboard(d: Dict[str, Any], date_filter: Optional[str] = None) -> None:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    scope = f"  Date filter: {date_filter}" if date_filter else "  Scope: all-time"

    print(f"\n{_SEP}")
    print(f"  TRADING DASHBOARD  |  {ts}")
    print(f"{scope}")
    print(f"{_SEP}")

    # ── 1. Bankroll ───────────────────────────────────────────────────────────
    _section("BANKROLL")
    if d["bankroll_current"] is not None:
        _row("Starting",  _fmt_br(d["bankroll_start"]))
        _row("Current",   _fmt_br(d["bankroll_current"]))
        chg = d["bankroll_change"]
        _row("Change",    (_fmt_pnl(chg) if chg is not None else "N/A"))
    else:
        _row("Tracking",  "N/A  (pre-bankroll records; no bankroll_after stored)")
        _row("Env start", str(d["bankroll_start_env"]))
    _row("Max drawdown", f"-{d['max_drawdown']:.3f}u")

    # ── 2. Bet summary ────────────────────────────────────────────────────────
    _section("BET SUMMARY")
    n = d["n_real_bets"]
    _row("Total settled bets",  str(n))
    _row("Wins / Losses",       f"{d['n_wins']} / {d['n_losses']}")
    _row("Voids",               str(d["n_voids"]))
    _row("Unsettled",           str(d["n_unsettled"]))
    _row("Hit rate",            _fmt_wr(d["hit_rate"]))
    _row("Total P&L",           _fmt_pnl(d["total_pnl"]))
    _row("Total staked",        f"{d['total_staked']:.3f}u")
    _row("ROI",                 _fmt_roi(d["roi"]))

    # ── 3. Edge & CLV ─────────────────────────────────────────────────────────
    _section("EDGE & CLV")
    avg_e = f"{d['avg_edge']:+.2f}%" if d["avg_edge"] is not None else "N/A"
    _row("Avg edge (picked)",  avg_e)
    if d["n_clv_records"] > 0:
        avg_clv = f"{d['avg_clv']:+.2f}%" if d["avg_clv"] is not None else "N/A"
        pos_clv = _fmt_wr(d["pos_clv_rate"])
        _row("Avg CLV",       avg_clv)
        _row("Positive CLV rate", pos_clv)
        _row("CLV sample size", str(d["n_clv_records"]))
    else:
        _row("CLV",  "N/A  (no closing odds recorded yet)")

    # ── 4. Stake distribution ─────────────────────────────────────────────────
    _section("STAKE DISTRIBUTION")
    nk = d["n_kelly_sized"]
    if nk > 0:
        _row("Kelly-sized picks",  str(nk))
        _row("Min stake",          f"{d['stake_min']:.4f}u" if d["stake_min"] is not None else "N/A")
        _row("Avg stake",          f"{d['stake_avg']:.4f}u" if d["stake_avg"] is not None else "N/A")
        _row("Median stake",       f"{d['stake_med']:.4f}u" if d["stake_med"] is not None else "N/A")
        _row("Max stake",          f"{d['stake_max']:.4f}u" if d["stake_max"] is not None else "N/A")
        print()
        total_ks = sum(d["stake_buckets"].values())
        order = ["<=0.05", "0.05-0.10", "0.10-0.15", "0.15-0.20", "0.20-0.25", ">0.25"]
        for bkt in order:
            cnt = d["stake_buckets"].get(bkt, 0)
            if cnt:
                bar = "#" * min(cnt, 30)
                print(f"    {bkt:<12} {cnt:>4}  {_pct(cnt, total_ks):>6}  {bar}")
    else:
        _row("Kelly-sized picks",  "0  (stake_units not yet populated — pre-Kelly records)")

    # ── 5. Performance breakdowns ─────────────────────────────────────────────
    if d["n_real_bets"] > 0:
        _section("PERFORMANCE BREAKDOWNS")
        print(f"  {'label':<14}  {'n':>3}   W/L    win%      ROI     edge    odds   pnl")
        print(f"  {_COL}")
        _breakdown_table("TOUR",        d["by_tour"],       ["ATP", "WTA"])
        _breakdown_table("SURFACE",     d["by_surface"],    ["Hard", "Clay", "Grass"])
        _breakdown_table("CONFIDENCE",  d["by_confidence"], ["HIGH", "MEDIUM", "LOW"])
        _breakdown_table("QUALITY",     d["by_quality"],    ["CLEAN", "CAUTION", "FRAGILE"])
        _breakdown_table("ODDS BUCKET", d["by_odds_bucket"],
                         ["<1.50", "1.50-2.00", "2.00-2.50", "2.50-3.00", "3.00-4.00", "4.00+"])

    # ── 6. Activity counts ────────────────────────────────────────────────────
    _section("ACTIVITY  (forward predictions)")
    _row("Total picks sent",       str(d["n_fwd_picks"]))
    _row("Blocked / no-bet",       str(d["n_blocked"]))
    _row("Watchlist (no bet)",     str(d["n_watchlist"]))
    _row("Risk-capped (est.)",
         f"{d['n_risk_capped_est']}  "
         "*(is_pick=True but stake_units=None; includes pre-Kelly records)")

    # ── 7. Top 10 best / worst ────────────────────────────────────────────────
    _top10_table("TOP 10 BEST BETS", d["top10_best"],  reverse=True)
    _top10_table("TOP 10 WORST BETS", d["top10_worst"], reverse=False)

    print(f"\n{_SEP}\n")


# ──────────────────────────────────────────────────────────────────────────────
# JSON EXPORT
# ──────────────────────────────────────────────────────────────────────────────

def _serialisable(d: Dict[str, Any]) -> dict:
    """Strip raw record objects; replace with compact label strings."""
    def _label(r: dict) -> str:
        ps   = r.get("picked_side")
        name = r.get("player_a") if ps == "A" else r.get("player_b", "?")
        odds = r.get("settled_odds")
        pnl  = r.get("pnl_units", 0.0)
        return f"{name} @{odds:.2f} → {r.get('result','?')} ({_fmt_pnl(pnl)})" if odds else name

    out = {k: v for k, v in d.items()
           if k not in ("top10_best", "top10_worst")}
    out["top10_best"]  = [_label(r) for r in d["top10_best"]]
    out["top10_worst"] = [_label(r) for r in d["top10_worst"]]
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def export_json(d: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_serialisable(d), fh, indent=2, ensure_ascii=False)
    print(f"  JSON exported → {path}\n")


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def dashboard(date: Optional[str] = None,
              export_json_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute and print the trading dashboard.

    Args:
        date:             Optional ISO date string to scope to a single day.
        export_json_path: Optional file path for JSON export.

    Returns:
        The raw metrics dict (for programmatic use).
    """
    d = compute(date_filter=date)
    print_dashboard(d, date_filter=date)
    if export_json_path:
        export_json(d, export_json_path)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="All-time trading dashboard for the tennis model."
    )
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="Scope report to a single date")
    parser.add_argument("--json", metavar="PATH",
                        help="Export metrics to a JSON file")
    args = parser.parse_args()
    dashboard(date=args.date, export_json_path=args.json)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _main()
