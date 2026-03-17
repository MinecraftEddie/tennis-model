"""
tennis_model/backtest.py
========================
Stores predictions automatically when an alert fires, records actual results,
and generates a P&L / ROI report.

data/predictions.json is the single source of truth.
"""

import json
import logging
import os
from datetime import datetime, date
from typing import Optional

log = logging.getLogger(__name__)

# data/ lives one level above tennis_model/  →  Downloads/data/
DATA_DIR         = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.json")
MODEL_VERSION    = "2.0"

# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load() -> dict:
    _ensure_data_dir()
    if not os.path.exists(PREDICTIONS_FILE):
        return {"predictions": []}
    with open(PREDICTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    _ensure_data_dir()
    with open(PREDICTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_id(match_date: str, player_a: str, player_b: str) -> str:
    a_last = player_a.strip().split()[-1].lower()
    b_last = player_b.strip().split()[-1].lower()
    return f"{match_date}_{a_last}_{b_last}"


def _confidence(edge_pct: float) -> str:
    """edge_pct is in percentage points (e.g. 33.3, not 0.333)."""
    if edge_pct >= 20.0:
        return "HIGH"
    elif edge_pct >= 10.0:
        return "MEDIUM"
    return "LOW"

# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

def store_prediction(pick) -> str:
    """
    Called automatically inside maybe_alert() when an alert fires.
    Saves one entry to data/predictions.json.
    Returns the prediction id (skips silently on duplicate).
    """
    today    = date.today().isoformat()
    player_a = pick.player_a.full_name or pick.player_a.short_name
    player_b = pick.player_b.full_name or pick.player_b.short_name
    pred_id  = _make_id(today, player_a, player_b)

    # Determine which side is the pick
    edge_a = pick.edge_a or 0.0
    edge_b = pick.edge_b or 0.0
    if edge_a >= edge_b and edge_a > 0:
        pick_name = player_a
        pick_odds = pick.market_odds_a
        pick_edge = edge_a
    else:
        pick_name = player_b
        pick_odds = pick.market_odds_b
        pick_edge = edge_b

    data = _load()

    # Deduplication: same id = same match on same day, skip re-run
    if any(p["id"] == pred_id for p in data["predictions"]):
        log.info(f"Prediction {pred_id} already stored — skipping duplicate")
        return pred_id

    entry = {
        "id":            pred_id,
        "date":          today,
        "match":         f"{player_a} vs {player_b}",
        "tournament":    pick.tournament,
        "surface":       pick.surface,
        "tour":          pick.tour,
        "player_a":      player_a,
        "player_b":      player_b,
        "prob_a":        round(pick.prob_a, 3),
        "prob_b":        round(pick.prob_b, 3),
        "fair_odds_a":   pick.fair_odds_a,
        "fair_odds_b":   pick.fair_odds_b,
        "best_odds_a":   pick.market_odds_a,
        "best_odds_b":   pick.market_odds_b,
        # stored as decimal fraction (0.305 = 30.5%) to match schema
        "edge_a":        round(edge_a / 100, 3) if pick.edge_a is not None else None,
        "edge_b":        round(edge_b / 100, 3) if pick.edge_b is not None else None,
        "pick":          pick_name,
        "pick_odds":     pick_odds,
        "bookmaker":     pick.bookmaker,
        "confidence":    _confidence(pick_edge),
        "model_version": MODEL_VERSION,
        "result":        None,
        "winner":        None,
        "profit_loss":   None,
        "stored_at":     datetime.now().isoformat(timespec="seconds"),
    }

    data["predictions"].append(entry)
    _save(data)
    log.info(f"Prediction stored: {pred_id}  ({pick_name} @{pick_odds}  edge {pick_edge:+.1f}%)")
    return pred_id


def record_result(prediction_id: str, winner: str) -> dict:
    """
    Record the actual match result.
    winner: player name (substring match accepted, e.g. "Galfi").
    Updates result, winner, profit_loss fields and saves.
    Returns the updated prediction dict.
    """
    data = _load()
    for pred in data["predictions"]:
        if pred["id"] == prediction_id:
            # Resolve to A_WIN or B_WIN via case-insensitive substring match
            if winner.lower() in pred["player_a"].lower() or pred["player_a"].lower() in winner.lower():
                pred["result"] = "A_WIN"
                pred["winner"] = pred["player_a"]
            else:
                pred["result"] = "B_WIN"
                pred["winner"] = pred["player_b"]

            # Profit/loss: net units per 1 unit staked
            if pred["pick"] == pred["winner"]:
                pred["profit_loss"] = round(pred["pick_odds"] - 1.0, 3)
            else:
                pred["profit_loss"] = -1.0

            _save(data)
            pl_str = f"+{pred['profit_loss']:.3f}" if pred["profit_loss"] > 0 else f"{pred['profit_loss']:.3f}"
            log.info(f"Result recorded: {prediction_id}  winner={pred['winner']}  P&L={pl_str}")

            # Update ELO ratings
            def _clean(name): return name.lower().replace(" ", "_").replace(".", "")
            loser = pred["player_b"] if pred["winner"] == pred["player_a"] else pred["player_a"]
            try:
                from tennis_model.elo import get_elo_engine
                elo = get_elo_engine()
                elo.update(
                    winner_id=_clean(pred["winner"]),
                    loser_id=_clean(loser),
                    surface=pred["surface"],
                    tournament_level=pred.get("tournament_level", "wta_250"),
                    winner_ranking=pred.get("winner_ranking", 9999),
                    loser_ranking=pred.get("loser_ranking", 9999),
                )
                log.info(f"ELO updated: {_clean(pred['winner'])} beat {_clean(loser)}")
            except Exception as exc:
                log.warning(f"ELO update skipped: {exc}")

            return pred

    raise ValueError(f"Prediction '{prediction_id}' not found in {PREDICTIONS_FILE}")


def generate_report() -> dict:
    """
    Print a formatted P&L report and return the stats dict.
    Only settled predictions (result != null) count toward P&L.
    Pending predictions are listed separately.
    """
    data      = _load()
    all_preds = data["predictions"]
    settled   = [p for p in all_preds if p["result"] is not None]
    pending   = [p for p in all_preds if p["result"] is None]
    sep       = "=" * 55

    print(f"\n{sep}")
    print(f"  BACKTEST REPORT  —  {date.today().isoformat()}")
    print(f"{sep}")
    print(f"  Total predictions stored : {len(all_preds)}")

    if not settled:
        print(f"  Awaiting results for {len(pending)} predictions")
        if pending:
            print(f"\n  Pending predictions:")
            for p in pending:
                edge_side = p["edge_a"] if p["pick"] == p["player_a"] else p["edge_b"]
                edge_pct  = round((edge_side or 0) * 100, 1)
                print(f"    {p['id']:<40}  {p['pick']:<25}  @{p['pick_odds']}  edge {edge_pct:+.1f}%  [{p['confidence']}]")
        print(f"{sep}\n")
        return {"total_bets": 0, "pending": len(pending)}

    # ── Aggregate stats ────────────────────────────────────────────────────
    total_bets   = len(settled)
    wins         = sum(1 for p in settled if p["pick"] == p["winner"])
    losses       = total_bets - wins
    win_rate     = wins / total_bets
    total_profit = sum(p["profit_loss"] for p in settled)
    roi          = total_profit / total_bets

    def _pick_edge(p):
        e = p["edge_a"] if p["pick"] == p["player_a"] else p["edge_b"]
        return (e or 0.0) * 100   # back to percentage for display

    avg_edge = sum(_pick_edge(p) for p in settled) / total_bets
    avg_odds = sum(p["pick_odds"] for p in settled) / total_bets

    def _breakdown(key: str) -> dict:
        groups: dict = {}
        for p in settled:
            k = p.get(key, "Unknown") or "Unknown"
            if k not in groups:
                groups[k] = {"bets": 0, "wins": 0, "profit": 0.0}
            groups[k]["bets"]   += 1
            groups[k]["wins"]   += int(p["pick"] == p["winner"])
            groups[k]["profit"] += p["profit_loss"]
        return {
            k: {
                "bets": v["bets"],
                "wins": v["wins"],
                "roi":  round(v["profit"] / v["bets"], 3) if v["bets"] > 0 else 0.0,
            }
            for k, v in groups.items()
        }

    report = {
        "total_bets":    total_bets,
        "wins":          wins,
        "losses":        losses,
        "win_rate":      round(win_rate, 3),
        "total_profit":  round(total_profit, 3),
        "roi":           round(roi, 3),
        "avg_edge":      round(avg_edge, 1),
        "avg_odds":      round(avg_odds, 2),
        "by_surface":    _breakdown("surface"),
        "by_tour":       _breakdown("tour"),
        "by_confidence": _breakdown("confidence"),
        "pending":       len(pending),
    }

    # ── Print formatted ────────────────────────────────────────────────────
    print(f"  Bets settled   : {total_bets}   (pending: {len(pending)})")
    print(f"  Wins / Losses  : {wins} / {losses}  ({win_rate:.1%} win rate)")
    print(f"  Total P&L      : {total_profit:+.3f} units")
    print(f"  ROI            : {roi:+.1%}")
    print(f"  Avg edge       : {avg_edge:+.1f}%")
    print(f"  Avg odds       : @{avg_odds:.2f}")

    if report["by_surface"]:
        print(f"\n  By surface:")
        for surf, s in sorted(report["by_surface"].items()):
            print(f"    {surf:<8} {s['bets']:>3} bets  {s['wins']}/{s['bets']} wins  ROI {s['roi']:+.1%}")

    if report["by_tour"]:
        print(f"\n  By tour:")
        for tour, s in sorted(report["by_tour"].items()):
            print(f"    {tour:<5}  {s['bets']:>3} bets  {s['wins']}/{s['bets']} wins  ROI {s['roi']:+.1%}")

    if report["by_confidence"]:
        print(f"\n  By confidence:")
        for conf in ("HIGH", "MEDIUM", "LOW"):
            if conf in report["by_confidence"]:
                s = report["by_confidence"][conf]
                print(f"    {conf:<8} {s['bets']:>3} bets  {s['wins']}/{s['bets']} wins  ROI {s['roi']:+.1%}")

    print(f"{sep}\n")
    return report
