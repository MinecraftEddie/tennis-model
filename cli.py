import argparse
import logging
import os
import time

import schedule

from tennis_model.pipeline import run_match, run_from_config, scan_today

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULER + CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    os.makedirs("data", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join("data", "model.log"), encoding="utf-8"),
        ],
    )
    p = argparse.ArgumentParser(description="ATP Tennis Model v2")
    p.add_argument("--match",       type=str)
    p.add_argument("--tournament",  type=str,   default="ATP Tour")
    p.add_argument("--level",       type=str,   default="ATP 250")
    p.add_argument("--surface",     type=str,   default="Hard")
    p.add_argument("--market_odds", type=float, nargs=2, metavar=("OA","OB"))
    p.add_argument("--bookmaker",   type=str,   default="")
    p.add_argument("--schedule",    action="store_true")
    p.add_argument("--config",      type=str,   default="config.json")
    p.add_argument("--results",     action="store_true", help="Show backtest report")
    p.add_argument("--record",      type=str,   nargs=2, metavar=("ID", "WINNER"),
                   help="Record result: --record <prediction_id> <winner_name>")
    p.add_argument("--closing-odds", type=str, nargs=3, metavar=("ID", "OA", "OB"),
                   help="Record closing odds for CLV: --closing-odds <prediction_id> <odds_a> <odds_b>")
    p.add_argument("--odds-check",  action="store_true",
                   help="Print live odds for the configured match without running the model")
    p.add_argument("--tour",        type=str,   default="",
                   help="Tour for odds check: 'wta' or 'atp' (default: inferred from tournament)")
    p.add_argument("--test-alert",  action="store_true",
                   help="Run the model and force-send a Telegram alert regardless of edge threshold")
    p.add_argument("--scan-today",  action="store_true",
                   help="Scan the full ATP+WTA slate from The Odds API and evaluate every available match")
    p.add_argument("--calibration", action="store_true",
                   help="Print calibration diagnostic (breakdowns by odds/stake/quality)")
    p.add_argument("--cal-date",    type=str, default=None,
                   help="Date for calibration diagnostic (YYYY-MM-DD, default: today)")
    p.add_argument("--blocked-diagnostic", action="store_true",
                   help="Print blocked-matches diagnostic (reason breakdown)")
    p.add_argument("--blocked-date", type=str, default=None,
                   help="Date for blocked diagnostic (YYYY-MM-DD, default: today)")
    p.add_argument("--blocked-range-start", type=str, default=None,
                   help="Start date for blocked diagnostic range (YYYY-MM-DD)")
    p.add_argument("--blocked-range-end", type=str, default=None,
                   help="End date for blocked diagnostic range (YYYY-MM-DD)")
    p.add_argument("--settle", action="store_true",
                   help="Settle unsettled picks (auto-fetch results from API, fallback to manual)")
    p.add_argument("--settle-date", type=str, default=None,
                   help="Date for settlement (YYYY-MM-DD, default: today)")
    p.add_argument("--audit-match", type=str, default=None,
                   help="Audit a specific match: --audit-match 'A vs B' --market_odds OA OB")
    p.add_argument("--verbose", action="store_true",
                   help="Enable verbose pipeline trace in audit mode")
    args = p.parse_args()

    if args.scan_today:
        scan_today(args.config)
        return

    if args.audit_match:
        from tennis_model.scripts.audit_match import run_audit
        if not args.market_odds:
            print("--audit-match requires --market_odds OA OB")
            return
        report = run_audit(
            match_str=args.audit_match,
            market_odds_a=args.market_odds[0],
            market_odds_b=args.market_odds[1],
            surface=args.surface,
            tournament=args.tournament,
            tournament_lvl=args.level,
            tour=args.tour or "atp",
            bookmaker=args.bookmaker,
            verbose=args.verbose,
        )
        print(report)
        return

    if args.settle:
        from tennis_model.tracking.auto_settlement import settle_unsettled_picks
        from tennis_model.tracking.performance import load_and_summarize
        date = args.settle_date
        count = settle_unsettled_picks(date)
        if count == 0:
            print("No picks settled (no new winners available or all already settled).")
        else:
            print(f"Settled {count} pick(s).")
        summary = load_and_summarize(date)
        if summary.settled_picks > 0:
            print(
                f"\nPerformance ({date or 'today'}):\n"
                f"  Settled: {summary.settled_picks}  "
                f"W: {summary.wins}  L: {summary.losses}  "
                f"Win rate: {summary.win_rate:.1%}\n"
                f"  P&L: {summary.total_profit_units:+.2f}u  "
                f"ROI: {summary.roi:.1%}  "
                f"Avg odds: {summary.average_odds:.2f}"
            )
        return

    if args.blocked_diagnostic:
        from tennis_model.tracking.blocked_diagnostic import (
            load_and_summarize_blocked,
            load_and_summarize_blocked_range,
            format_blocked_diagnostic,
        )
        if args.blocked_range_start and args.blocked_range_end:
            diag = load_and_summarize_blocked_range(
                args.blocked_range_start, args.blocked_range_end,
            )
        else:
            diag = load_and_summarize_blocked(args.blocked_date)
        print(format_blocked_diagnostic(diag))
        return

    if args.calibration:
        from tennis_model.tracking.calibration_diagnostic import (
            build_calibration_diagnostic,
            format_calibration_diagnostic,
        )
        diag = build_calibration_diagnostic(args.cal_date)
        print(format_calibration_diagnostic(diag))
        return

    if args.odds_check:
        from tennis_model.odds_feed import print_odds_check
        import re
        if args.match:
            parts = re.split(r"\s+vs\.?\s+", args.match.strip(), flags=re.I)
            if len(parts) == 2:
                tour = args.tour or ("wta" if "wta" in args.tournament.lower() else "atp")
                print_odds_check(parts[0].strip(), parts[1].strip(), tour)
            else:
                print("--odds-check requires --match 'A. Player vs B. Player'")
        else:
            print("--odds-check requires --match")
        return

    if args.results:
        from tennis_model.backtest import generate_report
        generate_report()
        return

    if args.record:
        from tennis_model.backtest import record_result
        pred_id, winner = args.record
        result = record_result(pred_id, winner)
        pl = result["profit_loss"]
        pl_str = f"+{pl:.3f}" if pl > 0 else f"{pl:.3f}"
        clv = result.get("clv")
        clv_str = f"  CLV={clv:+.1%}" if clv is not None else ""
        print(f"Recorded: {result['id']}  winner={result['winner']}  P&L={pl_str}{clv_str}")
        return

    if args.closing_odds:
        from tennis_model.backtest import record_closing_odds
        pred_id, oa, ob = args.closing_odds
        result = record_closing_odds(pred_id, float(oa), float(ob))
        clv = result.get("clv")
        clv_str = f"{clv:+.1%}" if clv is not None else "N/A"
        print(f"Closing odds recorded: {result['id']}  CLV={clv_str}")
        return

    if args.schedule:
        # --schedule --config → boucle toutes les 6h
        schedule.every(6).hours.do(run_from_config, args.config)
        run_from_config(args.config)
        while True:
            schedule.run_pending()
            time.sleep(60)
    elif args.match:
        # --match "X vs Y" → analyse un seul match
        oa, ob = args.market_odds if args.market_odds else (None, None)
        pick = run_match(args.match, args.tournament, args.level, args.surface, oa, ob, args.bookmaker,
                         tour=args.tour)
        if args.test_alert:
            import json
            import tennis_model.telegram as _tg
            from tennis_model.telegram import format_telegram_alert, send_telegram
            if os.path.exists(args.config):
                with open(args.config) as _f:
                    _cfg = json.load(_f)
                tg = _cfg.get("telegram", {})
                if tg.get("bot_token"):
                    _tg.TELEGRAM_BOT_TOKEN = tg["bot_token"]
                if tg.get("chat_id"):
                    _tg.TELEGRAM_CHAT_ID = str(tg["chat_id"])
            msg = format_telegram_alert(pick)
            print("\n--- TELEGRAM ALERT PREVIEW ---")
            print(msg)
            print("------------------------------")
            ok = send_telegram(msg)
            print(f"Telegram send: {'OK' if ok else 'FAILED (check token/chat_id)'}")
    elif os.path.exists(args.config):
        # --config seul → one-shot sur tous les matchs du fichier
        run_from_config(args.config)
    else:
        # aucun argument → mode démo
        log.info("Demo: A. Walton vs C. Rodesch (Miami, Hard, @1.79/@1.93)")
        run_match("A. Walton vs C. Rodesch", "Miami", "ATP 1000", "Hard", 1.79, 1.93, "Unibet")
