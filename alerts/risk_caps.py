"""
alerts/risk_caps.py
===================
Bankroll risk caps for live alert dispatch.

Reads today's exposure and realized P&L from the prediction logs and blocks
an alert if a daily cap is breached.  All checks are read-only.

Env vars (all optional):
  MAX_STAKE_UNITS            — units staked per pick (default 1.0)
  MAX_DAILY_EXPOSURE_UNITS   — cumulative units staked cap per day (default 3.0)
  MAX_DAILY_DRAWDOWN_UNITS   — cumulative loss cap per day before stop (default 3.0)

Reason codes returned when blocked:
  DAILY_EXPOSURE_CAP   — total staked today >= MAX_DAILY_EXPOSURE_UNITS
  DAILY_DRAWDOWN_STOP  — realized loss today >= MAX_DAILY_DRAWDOWN_UNITS
"""
import json
import logging
import os
from datetime import date
from typing import Optional, Tuple

from tennis_model.config.runtime_config import (
    MAX_STAKE_UNITS,
    MAX_DAILY_EXPOSURE_UNITS,
    MAX_DAILY_DRAWDOWN_UNITS,
)

log = logging.getLogger(__name__)

_DATA_DIR     = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
_FORWARD_FILE = os.path.join(_DATA_DIR, "forward_predictions.jsonl")
_SETTLED_FILE = os.path.join(_DATA_DIR, "settled_predictions.jsonl")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _iter_jsonl(path: str):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def daily_stake_units(today: Optional[str] = None) -> float:
    """Total units already staked today, using stored stake_units from forward_predictions.

    Falls back to MAX_STAKE_UNITS per pick for legacy records without a stake_units field.
    """
    today        = today or _today()
    default_stake = MAX_STAKE_UNITS
    total = 0.0
    for rec in _iter_jsonl(_FORWARD_FILE):
        if rec.get("date") == today and rec.get("is_pick") is True:
            total += rec.get("stake_units") or default_stake
    return total


def daily_realized_pnl(today: Optional[str] = None) -> float:
    """Sum of pnl_units for settled real bets today (WIN/LOSS only)."""
    today = today or _today()
    return sum(
        rec.get("pnl_units", 0.0)
        for rec in _iter_jsonl(_SETTLED_FILE)
        if (rec.get("date") == today
            and rec.get("is_pick") is True
            and rec.get("result") in ("WIN", "LOSS"))
    )


def check() -> Tuple[bool, Optional[str]]:
    """
    Run all daily risk cap checks.

    Returns:
        (blocked, reason_code)

        blocked     — True if the next alert should be suppressed
        reason_code — "DAILY_EXPOSURE_CAP" | "DAILY_DRAWDOWN_STOP" | None
    """
    today   = _today()
    exposure = daily_stake_units(today)

    if exposure >= MAX_DAILY_EXPOSURE_UNITS:
        log.warning(
            f"[RISK] DAILY_EXPOSURE_CAP: staked {exposure:.1f}u today "
            f"(cap={MAX_DAILY_EXPOSURE_UNITS:.1f}u) — alert blocked"
        )
        return True, "DAILY_EXPOSURE_CAP"

    pnl = daily_realized_pnl(today)
    log.info(
        f"[RISK] {today}  exposure={exposure:.2f}u / cap={MAX_DAILY_EXPOSURE_UNITS:.1f}u  "
        f"realized_pnl={pnl:.3f}u / stop={-abs(MAX_DAILY_DRAWDOWN_UNITS):.1f}u"
    )
    if pnl <= -abs(MAX_DAILY_DRAWDOWN_UNITS):
        log.warning(
            f"[RISK] DAILY_DRAWDOWN_STOP: realized P&L today = {pnl:.3f}u "
            f"(stop={-abs(MAX_DAILY_DRAWDOWN_UNITS):.1f}u) — alert blocked"
        )
        return True, "DAILY_DRAWDOWN_STOP"

    return False, None
