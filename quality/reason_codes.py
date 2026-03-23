"""
quality/reason_codes.py
========================
Standardised reason codes for pipeline events.

Usage
-----
Import ReasonCode wherever a string reason/tag is produced or consumed.
Replace bare string literals progressively — start with the highest-signal
events (identity failures, data degradation, Telegram delivery).

ReasonCode inherits from str so existing code that does
    if reason == "IDENTITY_UNRESOLVED": ...
continues to work without changes during the migration.

Families
--------
IDENTITY_*     How player identity was resolved (or failed)
DATA_*         Network fetch outcomes (rate limit, timeout, empty, cache)
PROFILE_*      Stats quality after all fetch layers
VALIDATION_*   Match validation outcomes
PICK_*         Model / EV filter outcomes
ALERT_*        Alert suppression reasons
TELEGRAM_*     Telegram delivery status
"""
from enum import Enum


class ReasonCode(str, Enum):
    """
    Standardised string codes for pipeline events.

    Each member is also a plain str, so it can be used directly in log
    messages, filter_reason fields, and dict keys without .value.
    """

    # ── Identity resolution ───────────────────────────────────────────────────
    IDENTITY_MAP            = "IDENTITY_MAP"
    IDENTITY_WTA_PROFILES   = "IDENTITY_WTA_PROFILES"
    IDENTITY_ATP_SEARCH     = "IDENTITY_ATP_SEARCH"
    IDENTITY_UNRESOLVED     = "IDENTITY_UNRESOLVED"

    # ── Data fetch ────────────────────────────────────────────────────────────
    DATA_RATE_LIMITED       = "DATA_RATE_LIMITED"       # HTTP 429
    DATA_TIMEOUT            = "DATA_TIMEOUT"            # requests.Timeout
    DATA_EMPTY              = "DATA_EMPTY"              # short / non-200 response
    DATA_CACHE_HIT          = "DATA_CACHE_HIT"         # served from local cache
    DATA_CACHE_MISS         = "DATA_CACHE_MISS"        # no cache entry

    # ── Profile quality ───────────────────────────────────────────────────────
    PROFILE_FULL            = "PROFILE_FULL"            # live data, all fields
    PROFILE_DEGRADED        = "PROFILE_DEGRADED"        # fetch failed / stale cache
    PROFILE_ESTIMATED       = "PROFILE_ESTIMATED"       # WTA estimated defaults

    # ── Validation ────────────────────────────────────────────────────────────
    VALIDATION_SOURCE_UNKNOWN   = "VALIDATION_SOURCE_UNKNOWN"   # hard fail
    VALIDATION_PROFILE_DEGRADED = "VALIDATION_PROFILE_DEGRADED" # warning + penalty
    VALIDATION_STALE_ODDS       = "VALIDATION_STALE_ODDS"       # manual odds >24h
    VALIDATION_THIN_SAMPLE      = "VALIDATION_THIN_SAMPLE"      # <10 surface matches
    VALIDATION_INACTIVE         = "VALIDATION_INACTIVE"         # 0 YTD matches

    # ── Model / pick ──────────────────────────────────────────────────────────
    PICK_NO_EDGE            = "PICK_NO_EDGE"
    PICK_NO_MARKET_ODDS     = "PICK_NO_MARKET_ODDS"
    PICK_INSUFFICIENT_DATA  = "PICK_INSUFFICIENT_DATA"
    PICK_WTA_DATA_GATE      = "PICK_WTA_DATA_GATE"
    PICK_LOW_CONFIDENCE     = "PICK_LOW_CONFIDENCE"
    PICK_WATCHLIST          = "PICK_WATCHLIST"           # P4: evaluator watchlist decision
    PICK_BLOCKED_MODEL      = "PICK_BLOCKED_MODEL"       # P4: evaluator ignore/blocked
    PICK_VALIDATION_FAILED  = "PICK_VALIDATION_FAILED"   # P4: EV blocked, validation failed
    PICK_APPROVED           = "PICK_APPROVED"            # P4: evaluator approved pick

    # ── Alert suppression ─────────────────────────────────────────────────────
    ALERT_SUPPRESSED_FRAGILE      = "ALERT_SUPPRESSED_FRAGILE"
    ALERT_SUPPRESSED_NO_PICK      = "ALERT_SUPPRESSED_NO_PICK"
    ALERT_SUPPRESSED_NO_ODDS      = "ALERT_SUPPRESSED_NO_ODDS"
    ALERT_SUPPRESSED_KELLY_ZERO   = "ALERT_SUPPRESSED_KELLY_ZERO"
    ALERT_DEGRADED_STAKE_REDUCED  = "ALERT_DEGRADED_STAKE_REDUCED"   # P2: stake × 0.5
    DATA_DEFAULTS_APPLIED         = "DATA_DEFAULTS_APPLIED"          # P2: wta_estimated defaults used
    ALERT_SKIPPED_UNKNOWN = "ALERT_SKIPPED_UNKNOWN"  # P3: UNKNOWN profile — hard block
    ALERT_SKIPPED_RISK    = "ALERT_SKIPPED_RISK"      # P3: risk cap blocked alert
    ALERT_WATCHLIST       = "ALERT_WATCHLIST"          # P3: evaluator watchlist decision
    ALERT_SKIPPED_DEDUPE  = "ALERT_SKIPPED_DEDUPE"    # P3: already alerted today

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_NOT_CONFIGURED = "TELEGRAM_NOT_CONFIGURED"
    TELEGRAM_SEND_OK        = "TELEGRAM_SEND_OK"
    TELEGRAM_FAILED         = "TELEGRAM_FAILED"
    TELEGRAM_DRY_RUN        = "TELEGRAM_DRY_RUN"
