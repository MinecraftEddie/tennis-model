"""
evaluator/risk_engine.py
=========================
Centralised risk / stake decision for the alert pipeline.

Extracted from telegram.maybe_alert() (P2) into a pure function so that:
  - Risk logic is testable in isolation
  - Business rules live in one place (QUALITY_RULES is the single source of truth)
  - maybe_alert() and the dedup wrapper can share the same rules

Rules (evaluated in priority order):
  1. UNKNOWN profile on either player → not allowed (stake_factor = 0.0)
  2. Kelly stake is None or <= 0      → not allowed (no positive edge)
  3. DEGRADED profile on either player → allowed, stake_factor = 0.5 (from QUALITY_RULES)
  4. FULL on both players             → allowed, stake_factor = 1.0 (from QUALITY_RULES)

This module does NOT touch the MatchPick object — it is a pure function.
The caller is responsible for applying stake_units to pick.stake_units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tennis_model.quality.profile_quality import QUALITY_RULES, ProfileQuality
from tennis_model.quality.reason_codes import ReasonCode


@dataclass
class RiskDecision:
    """Result of compute_risk_decision()."""

    allowed:      bool   # False = no alert, no stake
    stake_units:  float  # Final stake (0.0 if not allowed)
    stake_factor: float  # Multiplier applied (1.0, 0.5, or 0.0)
    reason_code:  str    # ReasonCode value (machine-readable cause)


def compute_risk_decision(
    quality_a: str,
    quality_b: str,
    kelly_stake: Optional[float],
) -> RiskDecision:
    """
    Compute the final stake and suppression decision.

    Parameters
    ----------
    quality_a : str
        profile_quality of player A — "full", "degraded", or "unknown".
        Accepts both plain strings and ProfileQuality enum values
        (ProfileQuality inherits from str so equality holds either way).
    quality_b : str
        profile_quality of player B — same as quality_a.
    kelly_stake : float | None
        Kelly-sized stake already computed by the caller.
        None means Kelly <= 0 (no positive edge).

    Returns
    -------
    RiskDecision
        allowed=False → the alert must NOT be sent.
        Never raises.
    """
    # Rule 1: UNKNOWN profile → hard block (no bet, no alert)
    if quality_a == ProfileQuality.UNKNOWN or quality_b == ProfileQuality.UNKNOWN:
        return RiskDecision(
            allowed=False,
            stake_units=0.0,
            stake_factor=QUALITY_RULES[ProfileQuality.UNKNOWN]["stake_factor"],
            reason_code=ReasonCode.ALERT_SKIPPED_UNKNOWN,
        )

    # Rule 2: Kelly <= 0 → no positive edge
    if kelly_stake is None or kelly_stake <= 0.0:
        return RiskDecision(
            allowed=False,
            stake_units=0.0,
            stake_factor=1.0,  # No reduction applied — edge just not there
            reason_code=ReasonCode.ALERT_SUPPRESSED_KELLY_ZERO,
        )

    # Rule 3: DEGRADED profile on either player → halve stake
    if quality_a == ProfileQuality.DEGRADED or quality_b == ProfileQuality.DEGRADED:
        factor = QUALITY_RULES[ProfileQuality.DEGRADED]["stake_factor"]  # 0.5
        return RiskDecision(
            allowed=True,
            stake_units=round(kelly_stake * factor, 6),
            stake_factor=factor,
            reason_code=ReasonCode.ALERT_DEGRADED_STAKE_REDUCED,
        )

    # Rule 4: FULL on both → full stake
    factor = QUALITY_RULES[ProfileQuality.FULL]["stake_factor"]  # 1.0
    return RiskDecision(
        allowed=True,
        stake_units=round(kelly_stake * factor, 6),
        stake_factor=factor,
        reason_code=ReasonCode.PROFILE_FULL,
    )
