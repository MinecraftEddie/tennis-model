"""
Scheduling layer for the tennis agent.

Uses the `schedule` library when available (pip install schedule).
Falls back to a simple sleep loop if the package is not installed.

Jobs registered
---------------
  scan_matches_job   every SCAN_INTERVAL_MINUTES minutes (env, default 15)
  report_job         every day at 07:00 (schedule library only)
"""
import logging
import os
import time

log = logging.getLogger(__name__)

from tennis_model.config.runtime_config import SCAN_INTERVAL_MINUTES


def run_scheduler(dry_run: bool = False) -> None:
    """
    Start the continuous scheduler loop (blocking).

    Parameters
    ----------
    dry_run : bool
        Passed through to scan_matches_job — logs alerts instead of sending.
    """
    # Late import keeps startup fast and avoids circular imports at module load
    from tennis_model.orchestration.jobs import scan_matches_job, report_job, settlement_job

    log.info(
        f"[SCHEDULER] scan every {SCAN_INTERVAL_MINUTES} min | "
        f"settlement every 30 min | "
        f"daily report at 07:00 UTC | dry_run={dry_run}"
    )

    try:
        import schedule

        schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(
            scan_matches_job, dry_run=dry_run
        )
        schedule.every(30).minutes.do(settlement_job)
        schedule.every().day.at("07:00").do(report_job)

        log.info("[SCHEDULER] Jobs registered via 'schedule'. Running...")
        while True:
            schedule.run_pending()
            time.sleep(30)

    except ImportError:
        log.warning(
            "[SCHEDULER] 'schedule' package not installed — "
            "using simple sleep loop. "
            "Install with: pip install schedule"
        )
        _loop_count = 0
        while True:
            scan_matches_job(dry_run=dry_run)
            if _loop_count % 2 == 0:   # every ~30 min (2 × SCAN_INTERVAL=15)
                settlement_job()
            _loop_count += 1
            log.info(
                f"[SCHEDULER] sleeping {SCAN_INTERVAL_MINUTES} min until next scan"
            )
            time.sleep(SCAN_INTERVAL_MINUTES * 60)
