import argparse
import logging
import os
import time

import schedule

from tennis_model.pipeline import run_match, run_from_config

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
    args = p.parse_args()

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
        print(f"Recorded: {result['id']}  winner={result['winner']}  P&L={pl_str}")
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
        run_match(args.match, args.tournament, args.level, args.surface, oa, ob, args.bookmaker)
    elif os.path.exists(args.config):
        # --config seul → one-shot sur tous les matchs du fichier
        run_from_config(args.config)
    else:
        # aucun argument → mode démo
        log.info("Demo: A. Walton vs C. Rodesch (Miami, Hard, @1.79/@1.93)")
        run_match("A. Walton vs C. Rodesch", "Miami", "ATP 1000", "Hard", 1.79, 1.93, "Unibet")
