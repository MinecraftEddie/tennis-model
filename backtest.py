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
import re
from datetime import datetime, date
from typing import Optional

from tennis_model.config.runtime_config import (
    MODEL_VERSION, ELO_SHRINK, MARKET_WEIGHT, MC_WEIGHT, PROB_FLOOR,
    LONGSHOT_GUARD_THRESHOLD,
    UNDERDOG_EDGE_THRESHOLD_LOW_ODDS, UNDERDOG_EDGE_THRESHOLD_HIGH_ODDS,
)

log = logging.getLogger(__name__)

# data/ lives one level above tennis_model/  →  Downloads/data/
DATA_DIR         = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.json")

# Frozen at import time — all values sourced from config/runtime_config.py.
_MODEL_SNAPSHOT = {
    "model_version":                     MODEL_VERSION,
    "elo_shrink":                        ELO_SHRINK,
    "market_weight":                     MARKET_WEIGHT,
    "mc_weight":                         MC_WEIGHT,
    "prob_floor":                        PROB_FLOOR,
    "longshot_guard_threshold":          LONGSHOT_GUARD_THRESHOLD,
    "underdog_edge_threshold_low_odds":  UNDERDOG_EDGE_THRESHOLD_LOW_ODDS,
    "underdog_edge_threshold_high_odds": UNDERDOG_EDGE_THRESHOLD_HIGH_ODDS,
}

# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, remove . and - (tennis name punctuation)."""
    return re.sub(r"\s+", " ", re.sub(r"[.\-]", " ", s.lower())).strip()


def _name_matches(candidate: str, stored: str) -> bool:
    """Word-token containment match.

    All words in *candidate* must appear in *stored* as whole tokens.
    'Sinner'   matches 'J. Sinner'         → {'sinner'} ⊆ {'j','sinner'}  ✓
    'Li'    does NOT match 'Elina'          → {'li'} ⊄ {'elina'}           ✓
    'King'  does NOT match 'Dekking'        → {'king'} ⊄ {'dekking'}       ✓
    """
    cw = set(_norm(candidate).split())
    sw = set(_norm(stored).split())
    return bool(cw) and cw.issubset(sw)


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

    picked    = pick.require_picked_side()
    pick_name = picked["player"].short_name
    pick_odds = picked["market_odds"]
    pick_edge = picked["edge"] or 0.0

    data = _load()

    # Deduplication: same id = same match on same day, skip re-run
    if any(p["id"] == pred_id for p in data["predictions"]):
        log.info(f"Prediction {pred_id} already stored — skipping duplicate")
        return pred_id

    entry = {
        "id":            pred_id,
        "date":          today,
        "match":         f"{player_a} vs {player_b}",
        "tournament":       pick.tournament,
        "tournament_level": pick.tournament_level,
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
        "edge_a":        round(pick.edge_a / 100, 3) if pick.edge_a is not None else None,
        "edge_b":        round(pick.edge_b / 100, 3) if pick.edge_b is not None else None,
        "pick":          pick_name,
        "pick_odds":     pick_odds,
        "bookmaker":     pick.bookmaker,
        "confidence":       pick.confidence,
        "cautious":         (getattr(pick, "evaluator_result", {}) or {}).get("recommended_action") == "send_with_caution",
        "evaluator_result": getattr(pick, "evaluator_result", {}) or {},
        "model_version":    MODEL_VERSION,
        "model_snapshot":   _MODEL_SNAPSHOT,
        "result":           None,
        "winner":        None,
        "profit_loss":   None,
        # CLV fields — populated separately via record_closing_odds()
        # CLV = opening_odds / closing_odds - 1 for the pick side.
        # Positive CLV = model found value before the market corrected; this is the
        # primary signal that the model has genuine predictive edge, independent of P&L.
        "closing_odds_a": None,
        "closing_odds_b": None,
        "clv":           None,
        "stored_at":     datetime.now().isoformat(timespec="seconds"),
    }

    data["predictions"].append(entry)
    _save(data)
    log.info(f"Prediction stored: {pred_id}  ({pick_name} @{pick_odds}  edge {pick_edge:+.1f}%)")
    return pred_id


def record_closing_odds(prediction_id: str,
                        closing_a: float,
                        closing_b: float) -> dict:
    """
    Record closing line odds and compute CLV for the pick side.

    CLV = opening_odds / closing_odds - 1
    Positive CLV means the model backed a bet whose opening price was higher
    than the closing price — i.e., the market moved in the model's direction.
    A model with consistently positive CLV has genuine edge regardless of P&L noise.

    Call this just before or after the match starts (closing line).
    """
    data = _load()
    for pred in data["predictions"]:
        if pred["id"] == prediction_id:
            pred["closing_odds_a"] = closing_a
            pred["closing_odds_b"] = closing_b
            opening = (pred["best_odds_a"] if pred["pick"] == pred["player_a"]
                       else pred["best_odds_b"])
            closing = closing_a if pred["pick"] == pred["player_a"] else closing_b
            if opening and closing and closing > 1.0:
                pred["clv"] = round(opening / closing - 1, 4)
                clv_str = f"{pred['clv']:+.1%}"
            else:
                pred["clv"] = None
                clv_str = "N/A"
            _save(data)
            log.info(
                f"Closing odds recorded: {prediction_id}  "
                f"closing={closing:.2f}  CLV={clv_str}"
            )
            return pred
    raise ValueError(f"Prediction '{prediction_id}' not found in {PREDICTIONS_FILE}")


def record_result(prediction_id: str, winner: str) -> dict:
    """
    Record the actual match result.
    winner: player name, matched by word-token containment (e.g. "Sinner" matches
    "J. Sinner"; partial last names work, but "Li" will NOT falsely match "Elina").
    Updates result, winner, profit_loss fields and saves.
    Returns the updated prediction dict.
    """
    data = _load()
    for pred in data["predictions"]:
        if pred["id"] == prediction_id:
            log.info(f"Settling {prediction_id} from {PREDICTIONS_FILE}")
            # Resolve to A_WIN or B_WIN via word-token containment match
            match_a = _name_matches(winner, pred["player_a"])
            match_b = _name_matches(winner, pred["player_b"])

            if match_a and not match_b:
                pred["result"] = "A_WIN"
                pred["winner"] = pred["player_a"]
            elif match_b and not match_a:
                pred["result"] = "B_WIN"
                pred["winner"] = pred["player_b"]
            elif match_a and match_b:
                log.warning(
                    f"Ambiguous winner {winner!r}: matches both "
                    f"{pred['player_a']!r} and {pred['player_b']!r} — provide a more specific name"
                )
                raise ValueError(
                    f"Ambiguous winner {winner!r}: matches both "
                    f"player_a={pred['player_a']!r} and player_b={pred['player_b']!r}. "
                    f"Provide a more specific name."
                )
            else:
                log.warning(
                    f"Winner {winner!r} matches neither player: "
                    f"player_a={pred['player_a']!r}, player_b={pred['player_b']!r} — skipping settlement"
                )
                raise ValueError(
                    f"Winner {winner!r} matches neither player: "
                    f"player_a={pred['player_a']!r}, player_b={pred['player_b']!r}"
                )

            # Profit/loss: net units per 1 unit staked
            if pred["pick"] == pred["winner"]:
                pred["profit_loss"] = round(pred["pick_odds"] - 1.0, 3)
            else:
                pred["profit_loss"] = -1.0

            _save(data)
            pl_str = f"+{pred['profit_loss']:.3f}" if pred["profit_loss"] > 0 else f"{pred['profit_loss']:.3f}"
            log.info(f"Result recorded: {prediction_id}  winner={pred['winner']}  P&L={pl_str}")

            # Update ELO ratings
            loser = pred["player_b"] if pred["winner"] == pred["player_a"] else pred["player_a"]
            try:
                from tennis_model.elo import get_elo_engine, canonical_id
                elo = get_elo_engine()
                elo.update(
                    winner_id=canonical_id(pred["winner"]),
                    loser_id=canonical_id(loser),
                    surface=pred["surface"],
                    tournament_level=pred.get("tournament_level", "wta_250"),
                    winner_ranking=pred.get("winner_ranking", 9999),
                    loser_ranking=pred.get("loser_ranking", 9999),
                )
                log.info(f"ELO updated: {pred['winner']} beat {loser}")
            except Exception as exc:
                log.warning(f"ELO update skipped: {exc}")

            return pred

    log.warning(f"Settlement failed: prediction {prediction_id!r} not found in {PREDICTIONS_FILE}")
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

    # CLV stats (only bets where closing odds were recorded)
    clv_bets = [p for p in settled if p.get("clv") is not None]
    avg_clv          = sum(p["clv"] for p in clv_bets) / len(clv_bets) if clv_bets else None
    positive_clv_ct  = sum(1 for p in clv_bets if p["clv"] > 0) if clv_bets else 0

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
        "total_bets":        total_bets,
        "wins":              wins,
        "losses":            losses,
        "win_rate":          round(win_rate, 3),
        "total_profit":      round(total_profit, 3),
        "roi":               round(roi, 3),
        "avg_edge":          round(avg_edge, 1),
        "avg_odds":          round(avg_odds, 2),
        "clv_tracked":       len(clv_bets),
        "avg_clv":           round(avg_clv, 4) if avg_clv is not None else None,
        "positive_clv_rate": round(positive_clv_ct / len(clv_bets), 3) if clv_bets else None,
        "by_surface":        _breakdown("surface"),
        "by_tour":           _breakdown("tour"),
        "by_confidence":     _breakdown("confidence"),
        "pending":           len(pending),
    }

    # ── Print formatted ────────────────────────────────────────────────────
    print(f"  Bets settled   : {total_bets}   (pending: {len(pending)})")
    print(f"  Wins / Losses  : {wins} / {losses}  ({win_rate:.1%} win rate)")
    print(f"  Total P&L      : {total_profit:+.3f} units")
    print(f"  ROI            : {roi:+.1%}")
    print(f"  Avg edge       : {avg_edge:+.1f}%")
    print(f"  Avg odds       : @{avg_odds:.2f}")

    if clv_bets:
        print(f"\n  CLV ({len(clv_bets)} bets with closing odds tracked):")
        print(f"    Avg CLV      : {avg_clv:+.1%}")
        print(f"    Positive CLV : {positive_clv_ct}/{len(clv_bets)}")
        print(f"    (Positive CLV = model found value before market corrected)")
    else:
        print(f"\n  CLV: not yet tracked — use record_closing_odds() before match start")

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

    # ── Calibration report (segmented professional stats) ──────────────────
    try:
        from tennis_model.reporting.calibration import compute_calibration, print_calibration
        cal = compute_calibration(all_preds)
        print_calibration(cal)
    except Exception as exc:
        log.warning(f"Calibration report skipped: {exc}")

    return report
