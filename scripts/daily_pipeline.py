#!/usr/bin/env python3
"""
scripts/daily_pipeline.py
==========================
Daily automation orchestrator for the tennis model.

Runs the full pipeline, settles finished matches, updates performance,
and writes diagnostics.  Safe to run when files are missing or empty.

Usage:
    python scripts/daily_pipeline.py                # today
    python scripts/daily_pipeline.py --date 2026-03-24  # specific date (settlement only)

Scheduling examples:

    # Mac / Linux cron (every day at 09:00)
    # 0 9 * * * cd /path/to/project && python scripts/daily_pipeline.py

    # Windows Task Scheduler
    #   Program:           python
    #   Arguments:          scripts/daily_pipeline.py
    #   Start in (working directory): C:\\path\\to\\project
"""
import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date as _date

# Ensure project root is on sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tennis_model.tracking.auto_settlement import settle_unsettled_picks
from tennis_model.tracking.performance import load_and_summarize, PerformanceSummary
from tennis_model.tracking.calibration_diagnostic import (
    build_calibration_diagnostic,
    format_calibration_diagnostic,
)
from tennis_model.tracking.blocked_diagnostic import (
    load_and_summarize_blocked,
    format_blocked_diagnostic,
)


# ── Paths ────────────────────────────────────────────────────────────────────

_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
_PERF_DIR = os.path.join(_DATA_DIR, "performance")
_LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs")


# ── Logging setup ────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    """Configure file + console logging; return the root logger."""
    os.makedirs(_LOGS_DIR, exist_ok=True)
    log_path = os.path.join(_LOGS_DIR, "daily_pipeline.log")

    logger = logging.getLogger("daily_pipeline")
    logger.setLevel(logging.DEBUG)

    # Clear existing handlers to avoid duplicates (e.g. across test runs)
    logger.handlers.clear()

    # File handler — append, detailed
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console handler — info and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    logger.addHandler(ch)

    return logger


# ── Steps ────────────────────────────────────────────────────────────────────

def step_scan(log: logging.Logger) -> bool:
    """Run scan_today.  Returns True if scan completed, False on error."""
    log.info("=== STEP 1: scan_today ===")
    try:
        from tennis_model.pipeline import scan_today
        scan_today()
        log.info("scan_today completed")
        return True
    except Exception as exc:
        log.error("scan_today failed: %s", exc, exc_info=True)
        return False


def step_settle(log: logging.Logger, date_str: str) -> int:
    """Settle unsettled picks.  Returns count of newly settled."""
    log.info("=== STEP 2: settle_unsettled_picks (%s) ===", date_str)
    try:
        count = settle_unsettled_picks(date_str)
        log.info("Settled %d pick(s)", count)
        return count
    except Exception as exc:
        log.error("Settlement failed: %s", exc, exc_info=True)
        return 0


def step_performance(log: logging.Logger, date_str: str) -> None:
    """Update performance summary and write to data/performance/summary.json."""
    log.info("=== STEP 3: performance summary ===")
    try:
        summary = load_and_summarize(date_str)
        os.makedirs(_PERF_DIR, exist_ok=True)
        out_path = os.path.join(_PERF_DIR, "summary.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(asdict(summary), f, indent=2, ensure_ascii=False)
        log.info(
            "Performance: %d settled, win_rate=%.1f%%, roi=%.1f%%, profit=%.2f units",
            summary.settled_picks,
            summary.win_rate * 100,
            summary.roi * 100,
            summary.total_profit_units,
        )
    except Exception as exc:
        log.error("Performance summary failed: %s", exc, exc_info=True)


def step_blocked_diagnostic(log: logging.Logger, date_str: str) -> None:
    """Run blocked diagnostic and log output."""
    log.info("=== STEP 4: blocked diagnostic ===")
    try:
        diag = load_and_summarize_blocked(date_str)
        output = format_blocked_diagnostic(diag)
        log.info("Blocked diagnostic:\n%s", output)
    except Exception as exc:
        log.error("Blocked diagnostic failed: %s", exc, exc_info=True)


def step_calibration_diagnostic(log: logging.Logger, date_str: str) -> None:
    """Run calibration diagnostic and log output."""
    log.info("=== STEP 5: calibration diagnostic ===")
    try:
        diag = build_calibration_diagnostic(date_str)
        output = format_calibration_diagnostic(diag)
        log.info("Calibration diagnostic:\n%s", output)
    except Exception as exc:
        log.error("Calibration diagnostic failed: %s", exc, exc_info=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main(date_str: str | None = None, skip_scan: bool = False) -> dict:
    """Run the full daily pipeline.

    Parameters
    ----------
    date_str : str | None
        ISO date string.  Defaults to today.
    skip_scan : bool
        If True, skip the scan_today step (useful for settlement-only runs).

    Returns
    -------
    dict with keys: date, scan_ok, settled_count, steps_completed
    """
    log = _setup_logging()
    date_str = date_str or _date.today().isoformat()
    log.info("Daily pipeline started for %s", date_str)

    result = {
        "date": date_str,
        "scan_ok": None,
        "settled_count": 0,
        "steps_completed": [],
    }

    # Step 1: Scan (only for today, not historical dates)
    if not skip_scan:
        result["scan_ok"] = step_scan(log)
        result["steps_completed"].append("scan")
    else:
        log.info("Skipping scan (--skip-scan or historical date)")
        result["scan_ok"] = True

    # Step 2: Settle
    result["settled_count"] = step_settle(log, date_str)
    result["steps_completed"].append("settle")

    # Step 3: Performance
    step_performance(log, date_str)
    result["steps_completed"].append("performance")

    # Step 4: Blocked diagnostic
    step_blocked_diagnostic(log, date_str)
    result["steps_completed"].append("blocked_diagnostic")

    # Step 5: Calibration diagnostic
    step_calibration_diagnostic(log, date_str)
    result["steps_completed"].append("calibration_diagnostic")

    log.info("Daily pipeline finished for %s — %d step(s) completed",
             date_str, len(result["steps_completed"]))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily tennis model automation")
    parser.add_argument("--date", default=None, help="ISO date (default: today)")
    parser.add_argument("--skip-scan", action="store_true",
                        help="Skip scan_today (settlement + diagnostics only)")
    args = parser.parse_args()
    main(date_str=args.date, skip_scan=args.skip_scan)
