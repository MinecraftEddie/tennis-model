"""
tennis_model/tracking/daily_report.py
======================================
Daily performance report from settled predictions.

Source : data/settled_predictions.jsonl  (written by settle_predictions.py)
Joins  : data/forward_predictions.jsonl  (for tour / surface not in settled records)

Usage
-----
    from tennis_model.tracking.daily_report import report

    report()                              # all dates
    report(date="2026-03-21")             # single day
    report(export_json="out/daily.json")  # + JSON export
    report(export_csv="out/daily.csv")    # + CSV export

Terminology
-----------
  pick          — is_pick=True (model had a preferred side and bet was placed)
  evaluator-flagged — is_pick=True but blocked_reason set (bet placed; evaluator noted risk)
  no-bet / blocked  — is_pick=False (data gate or no positive edge; no bet placed)
  settled pick  — pick where result ∈ {WIN, LOSS}  (used for real P&L)

P&L and ROI are computed ONLY from settled picks.
Hypothetical blocked analysis is clearly labelled and never mixed with real metrics.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import defaultdict
from datetime import date as _date
from typing import Optional

log = logging.getLogger(__name__)

_DATA_DIR      = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
_SETTLED_FILE  = os.path.join(_DATA_DIR, "settled_predictions.jsonl")
_FORWARD_FILE  = os.path.join(_DATA_DIR, "forward_predictions.jsonl")

_SEP  = "═" * 62
_LINE = "─" * 62
_COL  = "─" * 38


# ──────────────────────────────────────────────────────────────────────────────
# I/O
# ──────────────────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning(f"Malformed line in {path} — skipped")
    return records


def _forward_lookup() -> dict:
    """Return dict[match_id → forward_record] for tour/surface enrichment."""
    return {r["match_id"]: r for r in _load_jsonl(_FORWARD_FILE) if "match_id" in r}


def _enrich(settled: list, fwd: dict) -> list:
    """Attach tour/surface/tournament from forward file when missing in settled record."""
    out = []
    for r in settled:
        rec = dict(r)
        f = fwd.get(rec.get("match_id"), {})
        if f:
            rec.setdefault("tour",       f.get("tour"))
            rec.setdefault("surface",    f.get("surface"))
            rec.setdefault("tournament", f.get("tournament"))
            rec["is_paper_pick"] = f.get("is_paper_pick", False)
        out.append(rec)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# PER-RECORD HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _pick_edge(rec: dict) -> Optional[float]:
    """Pick-side edge in percent (e.g. 3.22 for 3.22%), or None."""
    ps = rec.get("picked_side")
    if ps == "A":
        return rec.get("edge_a")
    if ps == "B":
        return rec.get("edge_b")
    return None


def _pick_label(rec: dict) -> str:
    """Short display string: 'Sinner @1.95 [MEDIUM/CLEAN]'"""
    ps   = rec.get("picked_side")
    name = rec.get("player_a") if ps == "A" else rec.get("player_b", "?")
    odds = rec.get("settled_odds")
    conf = rec.get("confidence", "")
    qt   = rec.get("quality_tier", "")
    tier = f"{conf}/{qt}" if conf and qt else conf or qt or ""
    odds_str = f"@{odds:.2f}" if odds else ""
    return f"{name} {odds_str}  [{tier}]".strip()


def _is_favorite(rec: dict) -> Optional[str]:
    odds = rec.get("settled_odds")
    if odds is None:
        return None
    return "Favorite" if odds < 2.00 else "Underdog"


# ──────────────────────────────────────────────────────────────────────────────
# SEGMENT STATS  (real bets only — is_pick=True, result ∈ WIN/LOSS)
# ──────────────────────────────────────────────────────────────────────────────

def _seg_stats(settled_picks: list) -> dict:
    """
    Aggregate stats for a list of settled pick records (result=WIN or LOSS).
    Returns an empty-safe dict.
    """
    n = len(settled_picks)
    if n == 0:
        return {"count": 0, "wins": 0, "losses": 0,
                "win_rate": None, "pnl": 0.0, "roi": None,
                "avg_edge": None, "avg_odds": None}

    wins   = [r for r in settled_picks if r.get("result") == "WIN"]
    losses = [r for r in settled_picks if r.get("result") == "LOSS"]
    pnl    = sum(r.get("pnl_units", 0.0) for r in settled_picks)
    edges  = [e for r in settled_picks for e in [_pick_edge(r)] if e is not None]
    odds_v = [r["settled_odds"] for r in settled_picks if r.get("settled_odds")]

    return {
        "count":    n,
        "wins":     len(wins),
        "losses":   len(losses),
        "win_rate": len(wins) / n,
        "pnl":      round(pnl, 4),
        "roi":      round(pnl / n * 100, 2),
        "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        "avg_odds": round(sum(odds_v) / len(odds_v), 3) if odds_v else None,
    }


def _breakdown(settled_picks: list, key_fn) -> dict:
    """Single-dimension breakdown → {label: _seg_stats(...)}."""
    groups: dict = defaultdict(list)
    for r in settled_picks:
        k = key_fn(r)
        if k:
            groups[k].append(r)
    return {k: _seg_stats(v) for k, v in groups.items()}


# ──────────────────────────────────────────────────────────────────────────────
# DAY COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def _day_data(recs: list) -> dict:
    """
    Build the full stats dict for one day's records.

    recs — all settled records for this date (picks + no-bets).
    """
    picks      = [r for r in recs if r.get("is_pick")]
    no_bets    = [r for r in recs if not r.get("is_pick")]

    settled_p  = [r for r in picks if r.get("result") in ("WIN", "LOSS")]
    wins       = [r for r in settled_p if r["result"] == "WIN"]
    losses     = [r for r in settled_p if r["result"] == "LOSS"]
    voids      = [r for r in picks if r.get("result") == "VOID"]
    unsettled  = [r for r in picks if r.get("result") == "UNSETTLED"]
    flagged    = [r for r in settled_p if r.get("blocked_reason")]

    overall    = _seg_stats(settled_p)

    # Highlights (only from picks with real results)
    best_win   = max(wins,   key=lambda r: r.get("pnl_units", 0.0),  default=None)
    worst_loss = min(losses, key=lambda r: _pick_edge(r) or 0.0,     default=None)
    top_edge   = max(settled_p, key=lambda r: _pick_edge(r) or 0.0,  default=None)

    # Hypothetical blocked wins:
    # NO_BET records where model had a direction (picked_side set) and that side won.
    # DO NOT include in any real P&L calculation.
    hyp_wins = [
        r for r in no_bets
        if r.get("picked_side") and r.get("winner") and r["picked_side"] == r["winner"]
    ]
    hyp_all_directed = [r for r in no_bets if r.get("picked_side") and r.get("winner")]

    # Splits (settled picks only)
    by_tour       = _breakdown(settled_p, lambda r: r.get("tour"))
    by_surface    = _breakdown(settled_p, lambda r: r.get("surface"))
    by_confidence = _breakdown(settled_p, lambda r: r.get("confidence"))
    by_quality    = _breakdown(settled_p, lambda r: r.get("quality_tier"))
    by_evaluator  = _breakdown(settled_p, lambda r: r.get("evaluator_decision"))
    by_fav_dog    = _breakdown(settled_p, _is_favorite)
    by_model_ver  = _breakdown(settled_p, lambda r: r.get("model_version"))

    # Watchlist_plus paper picks — tracked separately, never mixed with real P&L
    wlp_all     = [r for r in recs if r.get("is_paper_pick")]
    wlp_settled = [r for r in wlp_all if r.get("result") in ("WIN", "LOSS")]
    wlp_voids   = [r for r in wlp_all if r.get("result") == "VOID"]
    wlp_stats   = _seg_stats(wlp_settled)
    wlp_by_conf = _breakdown(wlp_settled, lambda r: r.get("confidence"))
    wlp_by_surf = _breakdown(wlp_settled, lambda r: r.get("surface"))
    wlp_by_fav  = _breakdown(wlp_settled, _is_favorite)
    wlp_by_ver  = _breakdown(wlp_settled, lambda r: r.get("model_version"))

    return {
        "total_evaluated": len(recs),
        "total_picks":     len(picks),
        "settled_count":   len(settled_p),
        "wins":            len(wins),
        "losses":          len(losses),
        "voids":           len(voids),
        "unsettled":       len(unsettled),
        "no_bet_count":    len(no_bets),
        "flagged_count":   len(flagged),
        "overall":         overall,
        "by_tour":         by_tour,
        "by_surface":      by_surface,
        "by_confidence":   by_confidence,
        "by_quality":      by_quality,
        "by_evaluator":    by_evaluator,
        "by_fav_dog":      by_fav_dog,
        "by_model_ver":    by_model_ver,
        "wlp_count":       len(wlp_all),
        "wlp_voids":       len(wlp_voids),
        "wlp_stats":       wlp_stats,
        "wlp_by_conf":     wlp_by_conf,
        "wlp_by_surf":     wlp_by_surf,
        "wlp_by_fav":      wlp_by_fav,
        "wlp_by_ver":      wlp_by_ver,
        "best_win":        best_win,
        "worst_loss":      worst_loss,
        "top_edge_pick":   top_edge,
        "hyp_wins":        hyp_wins,
        "hyp_directed":    len(hyp_all_directed),
        "blocked_count":   len(no_bets),
    }


# ──────────────────────────────────────────────────────────────────────────────
# TERMINAL FORMATTING
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_pnl(v: float) -> str:
    return f"+{v:.3f}u" if v > 0 else f"{v:.3f}u"


def _fmt_roi(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"+{v:.1f}%" if v > 0 else f"{v:.1f}%"


def _fmt_wr(v: Optional[float]) -> str:
    return f"{v:.1%}" if v is not None else "N/A"


def _print_seg(label: str, s: dict, indent: str = "    ") -> None:
    if s["count"] == 0:
        return
    wr  = _fmt_wr(s["win_rate"])
    roi = _fmt_roi(s["roi"])
    avg_e = f"{s['avg_edge']:+.1f}%" if s["avg_edge"] is not None else "N/A"
    avg_o = f"@{s['avg_odds']:.2f}" if s["avg_odds"] is not None else "N/A"
    print(
        f"{indent}{label:<16}  "
        f"{s['count']:>3} bets  "
        f"{s['wins']}W/{s['losses']}L  "
        f"wr {wr}  "
        f"roi {roi:>8}  "
        f"edge {avg_e}  odds {avg_o}"
    )


def _print_breakdown(title: str, breakdown: dict, order: list | None = None) -> None:
    if not breakdown:
        return
    keys = order if order else sorted(breakdown.keys())
    # Only print if at least one group has data
    if not any(breakdown.get(k, {}).get("count", 0) for k in keys
               if k in breakdown):
        return
    print(f"\n  {title}")
    print(f"  {_COL}")
    for k in keys:
        s = breakdown.get(k)
        if s and s["count"] > 0:
            _print_seg(str(k), s)


def _print_day(date_str: str, d: dict) -> None:
    s = d["overall"]

    print(f"\n{_SEP}")
    print(f"  DAILY REPORT — {date_str}")
    print(f"{_SEP}")

    print(f"  Evaluated : {d['total_evaluated']}  |  "
          f"Picks : {d['total_picks']}  |  "
          f"No-bet/Blocked : {d['blocked_count']}")

    if d["total_picks"] == 0:
        print(f"  No picks logged for this date.")
        print(f"{_SEP}")
        return

    # Pick status summary
    status_parts = []
    if d["settled_count"]:  status_parts.append(f"{d['settled_count']} settled")
    if d["voids"]:          status_parts.append(f"{d['voids']} void")
    if d["unsettled"]:      status_parts.append(f"{d['unsettled']} unsettled")
    if d["flagged_count"]:  status_parts.append(f"{d['flagged_count']} evaluator-flagged")
    print(f"  Picks     : {', '.join(status_parts) if status_parts else 'none settled yet'}")

    print(f"\n  {'─'*30}")
    print(f"  SETTLED P&L  ({d['settled_count']} bets)")
    print(f"  {'─'*30}")

    if d["settled_count"] == 0:
        print("  No settled picks — awaiting results.")
    else:
        print(f"  Wins / Losses : {d['wins']} / {d['losses']}  "
              f"(win rate {_fmt_wr(s['win_rate'])})")
        print(f"  P&L           : {_fmt_pnl(s['pnl'])}")
        print(f"  ROI           : {_fmt_roi(s['roi'])}")
        if s["avg_edge"] is not None:
            print(f"  Avg edge      : {s['avg_edge']:+.2f}%")
        if s["avg_odds"] is not None:
            print(f"  Avg odds      : @{s['avg_odds']:.2f}")

        # Splits
        _print_breakdown("BY TOUR",       d["by_tour"],       ["ATP", "WTA"])
        _print_breakdown("BY SURFACE",    d["by_surface"],    ["Hard", "Clay", "Grass"])
        _print_breakdown("BY CONFIDENCE", d["by_confidence"], ["HIGH", "MEDIUM", "LOW"])
        _print_breakdown("BY QUALITY",    d["by_quality"],    ["CLEAN", "CAUTION", "FRAGILE"])
        _print_breakdown("FAV / UNDERDOG",d["by_fav_dog"],    ["Favorite", "Underdog"])

        # Evaluator decision split (only if more than one value present)
        if len(d["by_evaluator"]) > 1:
            _print_breakdown("BY EVALUATOR", d["by_evaluator"])
        if len(d["by_model_ver"]) > 1:
            _print_breakdown("BY MODEL VERSION", d["by_model_ver"])

        # Highlights
        print(f"\n  HIGHLIGHTS")
        print(f"  {_COL}")
        if d["best_win"]:
            r = d["best_win"]
            print(f"  Best pick    : {_pick_label(r)}  → WIN  {_fmt_pnl(r['pnl_units'])}")
        if d["worst_loss"]:
            r = d["worst_loss"]
            edge_s = f"  edge {_pick_edge(r):+.2f}%" if _pick_edge(r) is not None else ""
            print(f"  Worst loss   : {_pick_label(r)}{edge_s}")
        if d["top_edge_pick"]:
            r = d["top_edge_pick"]
            edge_v = _pick_edge(r)
            result = r.get("result", "?")
            edge_s = f"  edge {edge_v:+.2f}%" if edge_v is not None else ""
            print(f"  Largest edge : {_pick_label(r)}{edge_s}  → {result}")

    # Hypothetical blocked section (clearly separated)
    print(f"\n  BLOCKED / NO-BET  ({d['blocked_count']} matches — hypothetical only)")
    print(f"  {_COL}")
    if d["blocked_count"] == 0:
        print("  None.")
    else:
        print(f"  ⚠  These are NOT included in any P&L or ROI above.")
        if d["hyp_directed"] > 0:
            pct = len(d["hyp_wins"]) / d["hyp_directed"] * 100
            print(f"  Matches with direction : {d['hyp_directed']}")
            print(f"  Would-have-won         : {len(d['hyp_wins'])} / {d['hyp_directed']}"
                  f"  ({pct:.0f}%)")
            for r in d["hyp_wins"][:3]:   # show up to 3
                ps   = r.get("picked_side")
                name = r.get("player_a") if ps == "A" else r.get("player_b", "?")
                odds = (r.get("odds_a") if ps == "A" else r.get("odds_b"))
                hyp_pnl = round((odds or 1.0) - 1.0, 3) if odds else 0.0
                br   = (r.get("blocked_reason") or "no direction")[:50]
                edge_v = _pick_edge(r)
                edge_s = f"  edge {edge_v:+.2f}%" if edge_v is not None else ""
                print(f"    {name} @{odds or '?'}{edge_s}  (blocked: {br})"
                      f"  hyp +{hyp_pnl:.3f}u")
        else:
            print(f"  No settled blocked records with a known direction.")

    # Watchlist_plus paper section — separate from all real P&L
    print(f"\n  WATCHLIST_PLUS  ⚠ PAPER TRADE — not real bets")
    print(f"  {_COL}")
    if d["wlp_count"] == 0:
        print("  No watchlist_plus paper picks for this date.")
    else:
        ws = d["wlp_stats"]
        print(f"  ⚠  Not included in P&L or ROI above.")
        if ws["count"] == 0:
            print(f"  Tracked : {d['wlp_count']}  (voids: {d['wlp_voids']})  — no results settled yet.")
        else:
            print(f"  Tracked  : {d['wlp_count']}  |  "
                  f"Settled: {ws['count']}  ({ws['wins']}W / {ws['losses']}L)  "
                  f"voids: {d['wlp_voids']}")
            print(f"  Win rate : {_fmt_wr(ws['win_rate'])}")
            print(f"  P&L      : {_fmt_pnl(ws['pnl'])}  ⚠ HYPOTHETICAL")
            print(f"  ROI      : {_fmt_roi(ws['roi'])}  ⚠ HYPOTHETICAL")
            if ws["avg_edge"] is not None:
                print(f"  Avg edge : {ws['avg_edge']:+.2f}%")
            if ws["avg_odds"] is not None:
                print(f"  Avg odds : @{ws['avg_odds']:.2f}")
            _print_breakdown("BY CONFIDENCE  (⚠ paper)", d["wlp_by_conf"], ["HIGH", "MEDIUM"])
            _print_breakdown("BY SURFACE  (⚠ paper)",    d["wlp_by_surf"], ["Hard", "Clay", "Grass"])
            _print_breakdown("FAV / UNDERDOG  (⚠ paper)", d["wlp_by_fav"], ["Favorite", "Underdog"])
            if len(d["wlp_by_ver"]) > 1:
                _print_breakdown("BY MODEL VERSION  (⚠ paper)", d["wlp_by_ver"])

    print(f"\n{_SEP}")


# ──────────────────────────────────────────────────────────────────────────────
# EXPORT
# ──────────────────────────────────────────────────────────────────────────────

def _export_json(days: dict, path: str) -> None:
    """Export the full report dict as JSON."""
    # Convert per-day dicts to a serialisable form (remove raw record objects)
    out = {}
    for d_str, d in days.items():
        flat = {k: v for k, v in d.items()
                if k not in ("best_win", "worst_loss", "top_edge_pick", "hyp_wins")}
        flat["best_win"]      = _pick_label(d["best_win"])      if d["best_win"]      else None
        flat["worst_loss"]    = _pick_label(d["worst_loss"])     if d["worst_loss"]    else None
        flat["top_edge_pick"] = _pick_label(d["top_edge_pick"])  if d["top_edge_pick"] else None
        flat["hyp_wins"]      = [_pick_label(r) for r in d["hyp_wins"]]
        out[d_str] = flat
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  JSON exported → {path}")


def _export_csv(days: dict, path: str) -> None:
    """Export one row per date with top-level metrics as CSV."""
    fields = [
        "date", "total_evaluated", "total_picks", "settled_count",
        "wins", "losses", "voids", "unsettled", "no_bet_count",
        "win_rate", "pnl", "roi", "avg_edge", "avg_odds",
        "blocked_count", "hyp_wins_count", "hyp_directed",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for d_str, d in sorted(days.items()):
            s = d["overall"]
            w.writerow({
                "date":             d_str,
                "total_evaluated":  d["total_evaluated"],
                "total_picks":      d["total_picks"],
                "settled_count":    d["settled_count"],
                "wins":             d["wins"],
                "losses":           d["losses"],
                "voids":            d["voids"],
                "unsettled":        d["unsettled"],
                "no_bet_count":     d["no_bet_count"],
                "win_rate":         round(s["win_rate"] * 100, 1) if s["win_rate"] is not None else "",
                "pnl":              s["pnl"],
                "roi":              s["roi"],
                "avg_edge":         s["avg_edge"],
                "avg_odds":         s["avg_odds"],
                "blocked_count":    d["blocked_count"],
                "hyp_wins_count":   len(d["hyp_wins"]),
                "hyp_directed":     d["hyp_directed"],
            })
    print(f"\n  CSV exported → {path}")


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def report(
    date:        Optional[str] = None,
    export_json: Optional[str] = None,
    export_csv:  Optional[str] = None,
) -> dict:
    """
    Generate and print the daily performance report.

    Args:
        date:        ISO date string to filter to a single day (e.g. "2026-03-21").
                     If None, reports all dates present in the settled file.
        export_json: Optional path to write a JSON export.
        export_csv:  Optional path to write a CSV export.

    Returns:
        dict of {date_str: day_data} for programmatic use.
    """
    settled = _load_jsonl(_SETTLED_FILE)
    if not settled:
        print(f"\n  No settled predictions found in {_SETTLED_FILE}")
        return {}

    fwd = _forward_lookup()
    settled = _enrich(settled, fwd)

    # Filter to requested date
    if date:
        settled = [r for r in settled if r.get("date") == date]
        if not settled:
            print(f"\n  No settled records found for date={date!r}")
            return {}

    # Group by date
    by_date: dict = defaultdict(list)
    for r in settled:
        by_date[r.get("date", "unknown")].append(r)

    # Compute and print
    days = {}
    for d_str in sorted(by_date.keys()):
        d = _day_data(by_date[d_str])
        days[d_str] = d
        _print_day(d_str, d)

    # Multi-day summary if more than one date
    if len(days) > 1:
        all_settled = [r for recs in by_date.values()
                       for r in recs if r.get("result") in ("WIN", "LOSS")]
        total_s = _seg_stats(all_settled)
        print(f"\n{_SEP}")
        print(f"  CUMULATIVE SUMMARY  ({len(days)} days)")
        print(f"  {_COL}")
        print(f"  Bets settled : {total_s['count']}   "
              f"{total_s['wins']}W / {total_s['losses']}L")
        print(f"  P&L          : {_fmt_pnl(total_s['pnl'])}")
        print(f"  ROI          : {_fmt_roi(total_s['roi'])}")
        if total_s["avg_edge"] is not None:
            print(f"  Avg edge     : {total_s['avg_edge']:+.2f}%")
        ver_bd = _breakdown(all_settled, lambda r: r.get("model_version"))
        if len(ver_bd) > 1:
            _print_breakdown("BY MODEL VERSION", ver_bd)
        print(f"{_SEP}\n")

    if export_json:
        _export_json(days, export_json)
    if export_csv:
        _export_csv(days, export_csv)

    return days
