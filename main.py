"""
Tennis agent — safe startup entry point.

Run from the parent directory of tennis_model/ (same convention as cli.py):

    python tennis_model/main.py               # continuous scheduler
    python tennis_model/main.py --dry-run     # log alerts, don't send Telegram
    python tennis_model/main.py --once        # single scan then exit
    python tennis_model/main.py --once --dry-run

Required environment variables:
    TELEGRAM_BOT_TOKEN      Bot token from @BotFather
    TELEGRAM_CHAT_ID        Target chat or channel ID

Optional environment variables:
    SCAN_INTERVAL_MINUTES   Scan frequency in minutes (default: 15)
    MODEL_VERSION           Dedupe namespace — bump to reset (default: 1.0)
"""
import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure the parent of tennis_model/ is on sys.path so all tennis_model.* imports work.
# This mirrors the PYTHONPATH convention described in CLAUDE.md.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _configure_logging() -> None:
    log_dir = Path(__file__).resolve().parent / "data"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "agent.log", encoding="utf-8"),
        ],
    )


def main() -> None:
    _configure_logging()
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(
        description="Tennis model automated agent — scheduled scanner + Telegram alerts"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log alerts instead of sending to Telegram (no side effects)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan then exit (skips the continuous scheduler)",
    )
    args = parser.parse_args()

    # Safety: auto-enable dry-run if Telegram is not configured
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not args.dry_run and (not token or token == "YOUR_BOT_TOKEN_HERE"):
        log.warning(
            "[STARTUP] TELEGRAM_BOT_TOKEN not set — dry-run mode activated automatically"
        )
        args.dry_run = True

    if args.once:
        from tennis_model.orchestration.jobs import scan_matches_job

        log.info(f"[STARTUP] Single scan (dry_run={args.dry_run})")
        scan_matches_job(dry_run=args.dry_run)
    else:
        interval = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
        log.info(
            f"[STARTUP] Scheduler — scan every {interval} min, dry_run={args.dry_run}"
        )
        from tennis_model.orchestration.scheduler import run_scheduler

        run_scheduler(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
