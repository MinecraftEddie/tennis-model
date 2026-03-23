"""
tennis_model/tracking/blocked_picks_audit.py
=============================================
Blocked-picks audit: measure whether filters and evaluator are helping
or blocking too many good bets.

Sources
-------
  data/forward_predictions.jsonl  — reason_codes, edge, tour, surface
  data/settled_predictions.jsonl  — result, pnl_units (join key: match_id)

Classification
--------------
  accepted  — is_pick=True, blocked_reason=null   (real bet placed)
  blocked   — is_pick=True, blocked_reason set     (direction existed; filter intervened)
  no_bet    — is_pick=False                        (no positive edge, no direction)

Real P&L  : accepted picks only.
Hypothetical P&L : blocked picks, clearly labelled ⚠ HYPOTHETICAL everywhere.
No crossover between the two.

Usage
-----
    from tennis_model.tracking.blocked_picks_audit import audit

    audit()
    audit(export_json="data/blocked_audit.json")
    audit(export_csv="data/blocked_audit.csv")
"""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import defaultdict
from typing import Optional

log = logging.getLogger(__name__)

_DATA_DIR     = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
_SETTLED_FILE = os.path.join(_DATA_DIR, "settled_predictions.jsonl")
_FORWARD_FILE = os.path.join(_DATA_DIR, "forward_predictions.jsonl")

_SEP  = "═" * 64
_LINE = "─" * 64
_COL  = "─" * 42

# Block codes in priority order — used to pick ONE primary code per match.
_BLOCK_PRIORITY = [
    "HIGH_GATE_BLOCK",
    "ODDS_BELOW_MIN",
    "PROB_FLOOR_BLOCK",
    "EVALUATOR_BLOCK",
    "EVALUATOR_WATCHLIST",
    "CONF_LOW",
    "NO_ELO_FALLBACK_MARKET",
    "EDGE_FAIL",
]

# All codes that may appear in blocked breakdowns (ordered for display).
_ALL_BLOCK_CODES = _BLOCK_PRIORITY + ["QUALIFIER_MATCH", "OTHER"]


# ──────────────────────────────────────────────────────────────────────────────
# I/O AND ENRICHMENT
# ──────────────────────────────────────────────────────────────────────────────

def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning(f"Malformed line in {path} — skipped")
    return out


def _build_forward_lookup(forward: list) -> dict:
    """dict[match_id → forward_record]."""
    return {r["match_id"]: r for r in forward if "match_id" in r}


def _enrich(settled: list, fwd: dict) -> list:
    """Attach forward-only fields (reason_codes, tour, surface) to each settled record."""
    out = []
    for r in settled:
        rec = dict(r)
        f = fwd.get(rec.get("match_id"), {})
        if f:
            rec.setdefault("tour",         f.get("tour"))
            rec.setdefault("surface",      f.get("surface"))
            rec.setdefault("tournament",   f.get("tournament"))
            rec["reason_codes"]  = f.get("reason_codes") or rec.get("reason_codes") or []
            rec["is_paper_pick"] = f.get("is_paper_pick", False)
        else:
            rec.setdefault("reason_codes", [])
        out.append(rec)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# PER-RECORD HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _classify(rec: dict) -> str:
    """Return 'accepted' | 'blocked' | 'no_bet'."""
    if not rec.get("is_pick"):
        return "no_bet"
    if rec.get("blocked_reason"):
        return "blocked"
    return "accepted"


def _pick_edge(rec: dict) -> Optional[float]:
    """Pick-side edge in percent (e.g. 3.22 for 3.22%), or None."""
    ps = rec.get("picked_side")
    if ps == "A": return rec.get("edge_a")
    if ps == "B": return rec.get("edge_b")
    return None


def _pick_name(rec: dict) -> str:
    """Display name for the picked player."""
    ps = rec.get("picked_side")
    if ps == "A": return rec.get("player_a", "?")
    if ps == "B": return rec.get("player_b", "?")
    return "?"


def _match_label(rec: dict) -> str:
    return f"{rec.get('player_a','?')} vs {rec.get('player_b','?')}"


def _primary_block_code(rec: dict) -> str:
    """Single primary block reason from reason_codes, with string fallback."""
    codes = rec.get("reason_codes") or []
    for c in _BLOCK_PRIORITY:
        if c in codes:
            return c
    # Fallback: derive from blocked_reason string (for pre-reason_code records)
    fr = (rec.get("blocked_reason") or "").upper()
    if "DATA GATE" in fr or "INSUFFICIENT DATA" in fr: return "HIGH_GATE_BLOCK"
    if "BELOW MINIMUM" in fr or "INVALID_ODDS" in fr or "NO MARKET ODDS" in fr: return "ODDS_BELOW_MIN"
    if "BELOW FLOOR" in fr:      return "PROB_FLOOR_BLOCK"
    if "EVALUATOR_WATCHLIST" in fr: return "EVALUATOR_WATCHLIST"
    if "EVALUATOR" in fr:        return "EVALUATOR_BLOCK"
    if "LOW CONFIDENCE" in fr:   return "CONF_LOW"
    if fr:                       return "EDGE_FAIL"
    return "OTHER"


def _is_favorite(rec: dict) -> str:
    odds = rec.get("settled_odds")
    if odds is None: return "Unknown"
    return "Favorite" if odds < 2.00 else "Underdog"


# ──────────────────────────────────────────────────────────────────────────────
# AGGREGATION
# ──────────────────────────────────────────────────────────────────────────────

def _seg_stats(recs: list) -> dict:
    """
    Aggregate W/L/P&L stats for a list of records with result ∈ {WIN, LOSS}.
    VOID and UNSETTLED are excluded from P&L but counted separately.
    """
    settled = [r for r in recs if r.get("result") in ("WIN", "LOSS")]
    wins    = [r for r in settled if r["result"] == "WIN"]
    losses  = [r for r in settled if r["result"] == "LOSS"]
    voids   = [r for r in recs if r.get("result") == "VOID"]
    pnl     = sum(r.get("pnl_units", 0.0) for r in settled)
    edges   = [e for r in settled for e in [_pick_edge(r)] if e is not None]
    odds_v  = [r["settled_odds"] for r in settled if r.get("settled_odds")]
    n = len(settled)
    return {
        "total":    len(recs),
        "settled":  n,
        "wins":     len(wins),
        "losses":   len(losses),
        "voids":    len(voids),
        "win_rate": len(wins) / n if n else None,
        "pnl":      round(pnl, 4),
        "roi":      round(pnl / n * 100, 2) if n else None,
        "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        "avg_odds": round(sum(odds_v) / len(odds_v), 3) if odds_v else None,
    }


def _breakdown(recs: list, key_fn) -> dict:
    """Single-dimension breakdown: {label → _seg_stats(group)}."""
    groups: dict = defaultdict(list)
    for r in recs:
        k = key_fn(r)
        if k:
            groups[k].append(r)
    return {k: _seg_stats(v) for k, v in groups.items()}


def _code_breakdown(blocked: list) -> dict:
    """
    Break blocked picks by primary block code.
    Each match is counted exactly once (primary code only).
    """
    groups: dict = defaultdict(list)
    for r in blocked:
        groups[_primary_block_code(r)].append(r)
    return {k: _seg_stats(v) for k, v in groups.items()}


def _code_multilabel(blocked: list) -> dict:
    """
    Multi-label count: how many blocked matches contain each reason code.
    A match may appear in multiple buckets. Useful for frequency analysis.
    """
    counts: dict = defaultdict(int)
    for r in blocked:
        for c in (r.get("reason_codes") or []):
            counts[c] += 1
    return dict(counts)


# ──────────────────────────────────────────────────────────────────────────────
# TOP-N LISTS
# ──────────────────────────────────────────────────────────────────────────────

def _top_n(recs: list, sort_key, n: int = 10, reverse: bool = True) -> list:
    return sorted(recs, key=sort_key, reverse=reverse)[:n]


def _top_row(rec: dict, hyp: bool = False) -> str:
    match   = _match_label(rec)
    name    = _pick_name(rec)
    odds    = rec.get("settled_odds")
    pnl     = rec.get("pnl_units", 0.0)
    edge    = _pick_edge(rec)
    odds_s  = f"@{odds:.2f}" if odds else "?"
    pnl_s   = f"+{pnl:.3f}u" if pnl > 0 else f"{pnl:.3f}u"
    edge_s  = f"  edge {edge:+.2f}%" if edge is not None else ""
    hyp_s   = " ⚠HYP" if hyp else ""
    code_s  = f"  [{_primary_block_code(rec)}]" if hyp else ""
    date_s  = rec.get("date", "")
    return f"  {date_s}  {match:<30}  {name} {odds_s}{edge_s}  {pnl_s}{hyp_s}{code_s}"


# ──────────────────────────────────────────────────────────────────────────────
# FORMATTING HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_pnl(v: float, hyp: bool = False) -> str:
    s = f"+{v:.3f}u" if v > 0 else f"{v:.3f}u"
    return s + ("  ⚠ HYPOTHETICAL" if hyp else "")


def _fmt_roi(v: Optional[float], hyp: bool = False) -> str:
    if v is None: return "N/A"
    s = f"+{v:.1f}%" if v > 0 else f"{v:.1f}%"
    return s + ("  ⚠ HYPOTHETICAL" if hyp else "")


def _fmt_wr(v: Optional[float]) -> str:
    return f"{v:.1%}" if v is not None else "N/A"


def _print_stats(s: dict, hyp: bool = False, indent: str = "  ") -> None:
    label = " ⚠ HYPOTHETICAL" if hyp else ""
    print(f"{indent}Settled   : {s['settled']}"
          f"   (wins {s['wins']}  losses {s['losses']}  voids {s['voids']})")
    print(f"{indent}Win rate  : {_fmt_wr(s['win_rate'])}")
    print(f"{indent}P&L       : {_fmt_pnl(s['pnl'], hyp)}")
    print(f"{indent}ROI       : {_fmt_roi(s['roi'], hyp)}")
    if s["avg_edge"] is not None:
        print(f"{indent}Avg edge  : {s['avg_edge']:+.2f}%")
    if s["avg_odds"] is not None:
        print(f"{indent}Avg odds  : @{s['avg_odds']:.2f}")


def _print_seg_row(label: str, s: dict, hyp: bool = False) -> None:
    if s["settled"] == 0: return
    wr  = _fmt_wr(s["win_rate"])
    roi = f"+{s['roi']:.1f}%" if s["roi"] and s["roi"] > 0 else (f"{s['roi']:.1f}%" if s["roi"] is not None else "N/A")
    e   = f"{s['avg_edge']:+.2f}%" if s["avg_edge"] is not None else "N/A"
    o   = f"@{s['avg_odds']:.2f}" if s["avg_odds"] is not None else "N/A"
    hyp_s = " ⚠HYP" if hyp else ""
    print(f"  {label:<24}  {s['settled']:>3}  "
          f"{s['wins']}W/{s['losses']}L  wr {wr}  roi {roi:>7}{hyp_s}  "
          f"edge {e}  odds {o}")


def _print_breakdown(title: str, bd: dict, order: Optional[list] = None,
                     hyp: bool = False) -> None:
    if not bd: return
    keys = order if order else sorted(bd.keys())
    active = [k for k in keys if k in bd and bd[k]["settled"] > 0]
    if not active: return
    print(f"\n  {title}")
    print(f"  {_COL}")
    for k in active:
        _print_seg_row(str(k), bd[k], hyp=hyp)


def _fmt_roi_plain(v: Optional[float]) -> str:
    if v is None: return "N/A"
    return f"+{v:.1f}%" if v > 0 else f"{v:.1f}%"


def _fmt_pnl_per_bet(stats: dict) -> str:
    n = stats["settled"]
    if not n: return "N/A"
    v = stats["pnl"] / n
    return f"+{v:.3f}u" if v > 0 else f"{v:.3f}u"


def _fmt_edge(v: Optional[float]) -> str:
    return f"{v:+.2f}%" if v is not None else "N/A"


def _fmt_odds(v: Optional[float]) -> str:
    return f"@{v:.2f}" if v is not None else "N/A"


def _print_comparison(acc: dict, blk: dict) -> None:
    print(f"\n  ACCEPTED vs BLOCKED COMPARISON")
    print(f"  {_COL}")
    print(f"  {'Metric':<20}  {'Accepted':>12}  {'Blocked (⚠HYP)':>16}")
    print(f"  {'─'*20}  {'─'*12}  {'─'*16}")
    print(f"  {'Win rate':<20}  {_fmt_wr(acc['win_rate']):>12}  {_fmt_wr(blk['win_rate']):>16}")
    print(f"  {'ROI':<20}  {_fmt_roi_plain(acc['roi']):>12}  {_fmt_roi_plain(blk['roi']):>16}")
    print(f"  {'P&L (per bet)':<20}  {_fmt_pnl_per_bet(acc):>12}  {_fmt_pnl_per_bet(blk):>16}")
    print(f"  {'Avg edge':<20}  {_fmt_edge(acc['avg_edge']):>12}  {_fmt_edge(blk['avg_edge']):>16}")
    print(f"  {'Avg odds':<20}  {_fmt_odds(acc['avg_odds']):>12}  {_fmt_odds(blk['avg_odds']):>16}")


# ──────────────────────────────────────────────────────────────────────────────
# EXPORT
# ──────────────────────────────────────────────────────────────────────────────

def _export_json(result: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  JSON exported → {path}")


def _export_csv(accepted: list, blocked: list, path: str) -> None:
    """One row per settled match with classification label."""
    fields = ["date", "match_id", "player_a", "player_b", "classification",
              "picked_side", "result", "settled_odds", "pnl_units",
              "edge", "confidence", "quality_tier", "tour", "surface",
              "blocked_reason", "primary_block_code"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for cls, recs in (("accepted", accepted), ("blocked", blocked)):
            for r in recs:
                w.writerow({
                    "date":               r.get("date"),
                    "match_id":           r.get("match_id"),
                    "player_a":           r.get("player_a"),
                    "player_b":           r.get("player_b"),
                    "classification":     cls,
                    "picked_side":        r.get("picked_side"),
                    "result":             r.get("result"),
                    "settled_odds":       r.get("settled_odds"),
                    "pnl_units":          r.get("pnl_units"),
                    "edge":               _pick_edge(r),
                    "confidence":         r.get("confidence"),
                    "quality_tier":       r.get("quality_tier"),
                    "tour":               r.get("tour"),
                    "surface":            r.get("surface"),
                    "blocked_reason":     r.get("blocked_reason"),
                    "primary_block_code": _primary_block_code(r) if cls == "blocked" else "",
                })
    print(f"\n  CSV exported → {path}")


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def audit(
    export_json: Optional[str] = None,
    export_csv:  Optional[str] = None,
) -> dict:
    """
    Run the blocked-picks audit and print the report.

    Returns a dict with 'accepted_stats', 'blocked_stats', 'breakdown_by_code',
    and 'comparison' for programmatic use.
    """
    settled = _load_jsonl(_SETTLED_FILE)
    if not settled:
        print(f"\n  No settled predictions found in {_SETTLED_FILE}")
        return {}

    fwd     = _build_forward_lookup(_load_jsonl(_FORWARD_FILE))
    records = _enrich(settled, fwd)

    # Classify and filter — skip NO_BET (no direction) and UNSETTLED
    accepted  = [r for r in records if _classify(r) == "accepted"
                 and r.get("result") in ("WIN", "LOSS", "VOID")]
    blocked   = [r for r in records if _classify(r) == "blocked"
                 and r.get("result") in ("WIN", "LOSS", "VOID")]
    no_bet_ct = sum(1 for r in records if _classify(r) == "no_bet")
    unsettled = sum(1 for r in records if r.get("result") == "UNSETTLED")

    acc_stats = _seg_stats(accepted)
    blk_stats = _seg_stats(blocked)

    date_min = min((r.get("date","") for r in records), default="")
    date_max = max((r.get("date","") for r in records), default="")
    date_range = f"{date_min} to {date_max}" if date_min != date_max else date_min

    # ── Print ─────────────────────────────────────────────────────────────────

    print(f"\n{_SEP}")
    print(f"  BLOCKED PICKS AUDIT  —  {date_range}")
    print(f"{_SEP}")
    print(f"  Total settled records : {len(records)}")
    print(f"  Accepted picks        : {len(accepted)}")
    print(f"  Blocked picks         : {len(blocked)}")
    print(f"  No-bet (no direction) : {no_bet_ct}")
    print(f"  Unsettled / skipped   : {unsettled}")

    # ── Accepted picks (real) ─────────────────────────────────────────────────
    print(f"\n  ACCEPTED PICKS  (real performance)")
    print(f"  {_COL}")
    if acc_stats["settled"] == 0:
        print("  No accepted picks settled yet.")
    else:
        _print_stats(acc_stats, hyp=False)

    # ── Blocked picks (hypothetical) ──────────────────────────────────────────
    print(f"\n  BLOCKED PICKS  ⚠ HYPOTHETICAL — not real bets")
    print(f"  {_COL}")
    if blk_stats["settled"] == 0:
        print("  No blocked picks settled yet.")
    else:
        _print_stats(blk_stats, hyp=True)

    # ── Comparison table ──────────────────────────────────────────────────────
    if acc_stats["settled"] > 0 and blk_stats["settled"] > 0:
        _print_comparison(acc_stats, blk_stats)

    # ── Breakdown by block reason ──────────────────────────────────────────────
    code_bd = _code_breakdown(blocked)
    _print_breakdown(
        "BREAKDOWN BY BLOCK REASON  (⚠ hypothetical)",
        code_bd,
        order=_ALL_BLOCK_CODES,
        hyp=True,
    )

    # Multi-label frequency (informational)
    multilabel = _code_multilabel(blocked)
    interesting = {k: v for k, v in multilabel.items()
                   if k in _ALL_BLOCK_CODES and v > 0}
    if interesting:
        print(f"\n  REASON CODE FREQUENCY  (multi-label; sums > total blocked)")
        print(f"  {_COL}")
        for k in _ALL_BLOCK_CODES:
            if k in interesting:
                print(f"  {k:<26}  {interesting[k]:>4} matches")

    # ── Splits on accepted picks (real) ───────────────────────────────────────
    _print_breakdown(
        "ACCEPTED — BY TOUR",
        _breakdown(accepted, lambda r: r.get("tour")),
        order=["ATP", "WTA"],
    )
    _print_breakdown(
        "ACCEPTED — BY CONFIDENCE",
        _breakdown(accepted, lambda r: r.get("confidence")),
        order=["HIGH", "MEDIUM", "LOW"],
    )
    _print_breakdown(
        "ACCEPTED — BY QUALITY",
        _breakdown(accepted, lambda r: r.get("quality_tier")),
        order=["CLEAN", "CAUTION", "FRAGILE"],
    )
    _print_breakdown(
        "ACCEPTED — FAV / UNDERDOG",
        _breakdown(accepted, _is_favorite),
        order=["Favorite", "Underdog"],
    )

    # Splits on blocked (hypothetical)
    _print_breakdown(
        "BLOCKED — BY TOUR  (⚠ hypothetical)",
        _breakdown(blocked, lambda r: r.get("tour")),
        order=["ATP", "WTA"],
        hyp=True,
    )
    _print_breakdown(
        "BLOCKED — BY CONFIDENCE  (⚠ hypothetical)",
        _breakdown(blocked, lambda r: r.get("confidence")),
        order=["HIGH", "MEDIUM", "LOW"],
        hyp=True,
    )

    # Version splits — only printed when multiple model versions are present
    _by_ver_acc = _breakdown(accepted, lambda r: r.get("model_version"))
    _by_ver_blk = _breakdown(blocked,  lambda r: r.get("model_version"))
    if len(_by_ver_acc) > 1:
        _print_breakdown("ACCEPTED — BY MODEL VERSION", _by_ver_acc)
    if len(_by_ver_blk) > 1:
        _print_breakdown("BLOCKED — BY MODEL VERSION  (⚠ hypothetical)", _by_ver_blk, hyp=True)

    # ── Top-N lists ────────────────────────────────────────────────────────────
    blk_wins   = [r for r in blocked if r.get("result") == "WIN"]
    blk_losses = [r for r in blocked if r.get("result") == "LOSS"]
    acc_wins   = [r for r in accepted if r.get("result") == "WIN"]
    acc_losses = [r for r in accepted if r.get("result") == "LOSS"]

    def _top_section(title: str, recs: list, sort_key, n: int, hyp: bool) -> None:
        top = _top_n(recs, sort_key, n=n)
        if not top: return
        print(f"\n  {title}")
        print(f"  {_COL}")
        for r in top:
            print(_top_row(r, hyp=hyp))

    _top_section(
        "TOP 10 BLOCKED WINNERS  ⚠ HYPOTHETICAL",
        blk_wins,
        sort_key=lambda r: r.get("pnl_units", 0.0),
        n=10, hyp=True,
    )
    _top_section(
        "TOP 10 BLOCKED LOSERS AVOIDED  ⚠ HYPOTHETICAL (edge sorted — best saves)",
        blk_losses,
        sort_key=lambda r: _pick_edge(r) or 0.0,
        n=10, hyp=True,
    )
    _top_section(
        "TOP 10 ACCEPTED WINNERS",
        acc_wins,
        sort_key=lambda r: r.get("pnl_units", 0.0),
        n=10, hyp=False,
    )
    _top_section(
        "TOP 10 ACCEPTED LOSERS  (edge sorted — most costly misses)",
        acc_losses,
        sort_key=lambda r: _pick_edge(r) or 0.0,
        n=10, hyp=False,
    )

    # ── Watchlist_plus paper picks (separate section — ⚠ hypothetical) ──────────
    paper = [r for r in records if r.get("is_paper_pick")
             and r.get("result") in ("WIN", "LOSS", "VOID")]
    print(f"\n  WATCHLIST_PLUS PAPER PICKS  ⚠ HYPOTHETICAL — not real bets")
    print(f"  {_COL}")
    if not paper:
        print(f"  No watchlist_plus paper picks settled yet.")
    else:
        print(f"  ⚠  Tracked separately — excluded from all P&L and ROI above.")
        pa_stats = _seg_stats(paper)
        print(f"  Total     : {pa_stats['total']}")
        _print_stats(pa_stats, hyp=True)
        _print_breakdown(
            "BY CONFIDENCE  (⚠ hypothetical)",
            _breakdown(paper, lambda r: r.get("confidence")),
            order=["HIGH", "MEDIUM"],
            hyp=True,
        )
        _print_breakdown(
            "BY SURFACE  (⚠ hypothetical)",
            _breakdown(paper, lambda r: r.get("surface")),
            order=["Hard", "Clay", "Grass"],
            hyp=True,
        )
        _print_breakdown(
            "FAV / UNDERDOG  (⚠ hypothetical)",
            _breakdown(paper, _is_favorite),
            order=["Favorite", "Underdog"],
            hyp=True,
        )
        _by_ver_paper = _breakdown(paper, lambda r: r.get("model_version"))
        if len(_by_ver_paper) > 1:
            _print_breakdown("BY MODEL VERSION  (⚠ hypothetical)", _by_ver_paper, hyp=True)

    print(f"\n{_SEP}\n")

    # ── Build return dict ─────────────────────────────────────────────────────
    result = {
        "date_range":         date_range,
        "total_records":      len(records),
        "accepted_count":     len(accepted),
        "blocked_count":      len(blocked),
        "no_bet_count":       no_bet_ct,
        "unsettled_count":    unsettled,
        "accepted_stats":     acc_stats,
        "blocked_stats":      blk_stats,
        "breakdown_by_code":  {k: v for k, v in code_bd.items()},
        "code_frequency":     multilabel,
        "by_tour_accepted":   _breakdown(accepted, lambda r: r.get("tour")),
        "by_tour_blocked":    _breakdown(blocked,  lambda r: r.get("tour")),
        "by_conf_accepted":   _breakdown(accepted, lambda r: r.get("confidence")),
        "by_conf_blocked":    _breakdown(blocked,  lambda r: r.get("confidence")),
        "paper_count":        len(paper),
        "paper_stats":        _seg_stats(paper) if paper else {},
        "by_version_accepted": _by_ver_acc,
        "by_version_blocked":  _by_ver_blk,
    }

    if export_json:
        _export_json(result, export_json)
    if export_csv:
        _export_csv(accepted, blocked, export_csv)

    return result
