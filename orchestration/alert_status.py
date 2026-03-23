"""
orchestration/alert_status.py
==============================
AlertStatus enum + AlertDecision dataclass.

These types give maybe_alert() a stable, typed return value that:
  - Replaces the ambiguous None return of P2
  - Enables scan_today() / DailyAudit to track alert outcomes precisely
  - Makes the final decision explicit and auditable

Used by:
  telegram.maybe_alert()   → produces AlertDecision
  alerts/telegram.py       → produces AlertDecision (dedup wrapper)
  orchestration/audit.py   → consumes AlertDecision (record_alert_decision)
  pipeline.scan_today()    → consumes AlertDecision via run_match(_audit)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AlertStatus(str, Enum):
    """Stable outcome codes for the alert dispatch pipeline."""

    SENT            = "SENT"             # Telegram dispatched & confirmed
    FAILED          = "FAILED"           # Telegram attempted but all retries failed
    WATCHLIST       = "WATCHLIST"        # Evaluator flagged watchlist — not sent
    SUPPRESSED      = "SUPPRESSED"       # FRAGILE quality tier — suppressed
    DRY_RUN         = "DRY_RUN"          # No Telegram credentials — dry run
    SKIPPED_UNKNOWN = "SKIPPED_UNKNOWN"  # UNKNOWN profile quality — hard block
    SKIPPED_NO_PICK = "SKIPPED_NO_PICK"  # No pick_player set or no valid odds
    SKIPPED_RISK    = "SKIPPED_RISK"     # Risk cap hit
    SKIPPED_DEDUPE  = "SKIPPED_DEDUPE"   # Already alerted today (dedup store)
    SKIPPED_KELLY   = "SKIPPED_KELLY"    # Kelly fraction <= 0 (no positive edge)


@dataclass
class AlertDecision:
    """
    Structured outcome of the alert pipeline for one MatchPick.

    Produced by maybe_alert() and the dedup wrapper.
    Consumed by DailyAudit.record_alert_decision() for audit tracking.

    Fields
    ------
    status             Stable AlertStatus enum value
    reason_code        ReasonCode string (machine-readable cause, loggable)
    stake_units        Final stake after reduction (None if suppressed)
    stake_factor       Multiplier applied: 1.0 (FULL), 0.5 (DEGRADED), 0.0 (blocked)
    telegram_attempted Was Telegram API called?
    telegram_sent      Did the Telegram send succeed?
    message_preview    First 100 chars of formatted alert message (optional)
    """

    status:             AlertStatus
    reason_code:        str
    stake_units:        Optional[float]
    stake_factor:       float
    telegram_attempted: bool
    telegram_sent:      bool
    message_preview:    Optional[str] = None
    risk_decision:      Optional[object] = None  # P6: RiskDecision from compute_risk_decision()
