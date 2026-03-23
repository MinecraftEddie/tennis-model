"""
evaluator/evaluator_decision.py
================================
EvaluatorStatus enum + EvaluatorDecision dataclass + build_evaluator_decision().

These types unify the pipeline's mid-decision layer so that:
  - WATCHLIST, NO_PICK, PICK, BLOCKED_* are stable typed statuses
  - pipeline.py no longer needs inline string building for filter_reason
  - DailyAudit can track evaluator outcomes by status, not by string inspection

Chain: ProfileQualityResult → EvaluatorDecision → RiskDecision → AlertDecision

Usage
-----
    ed = build_evaluator_decision(best_ev, eval_result, validation_passed)
    if ed.status == EvaluatorStatus.PICK:
        decision = maybe_alert(pick, card)
    elif ed.status == EvaluatorStatus.WATCHLIST:
        log.info(f"WATCHLIST: ...")
    else:
        log.info(f"FILTERED: {ed.filter_reason}")

Backward compat
---------------
EvaluatorDecision.filter_reason returns the SAME strings that pipeline.py was
building inline before P4 ("EVALUATOR_WATCHLIST", best_ev.filter_reason, etc.),
so scan_today() list-building logic is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from tennis_model.quality.reason_codes import ReasonCode


class EvaluatorStatus(str, Enum):
    """Stable outcome codes for the evaluator decision layer."""

    PICK               = "PICK"               # EV + evaluator approved → send alert
    WATCHLIST          = "WATCHLIST"           # EV passed, evaluator: watchlist
    BLOCKED_MODEL      = "BLOCKED_MODEL"       # EV passed, evaluator: ignore
    NO_PICK            = "NO_PICK"             # EV filter blocked (no edge, low conf, etc.)
    BLOCKED_VALIDATION = "BLOCKED_VALIDATION"  # EV blocked and validation also failed


@dataclass
class EvaluatorDecision:
    """
    Structured outcome of the evaluator decision layer for one MatchPick.

    Produced by build_evaluator_decision(). Consumed by:
      - pipeline.run_match()  → routes to maybe_alert() or not
      - DailyAudit.record_evaluator_decision() → audit counters
      - pick.filter_reason    → set from self.filter_reason (same strings as before P4)

    Fields
    ------
    status              Stable EvaluatorStatus enum value
    reason_code         ReasonCode string (machine-readable cause)
    filter_reason       Human-readable block reason; None for PICK (no block)
    confidence          Evaluator confidence float (None if evaluator unavailable)
    message             Evaluator short_message (None if evaluator unavailable)
    recommended_action  Legacy string from evaluator dict ("send", "watchlist", etc.)
    eval_result         Full evaluator dict (preserved for compatibility)
    """

    status:             EvaluatorStatus
    reason_code:        str
    filter_reason:      Optional[str]   # None for PICK; "EVALUATOR_*" or EV reason for others
    confidence:         Optional[float]
    message:            Optional[str]
    recommended_action: str             # "send" | "send_with_caution" | "watchlist" | "ignore" | "blocked"
    eval_result:        dict = field(default_factory=dict)


# ── Helper: map EV filter_reason string to a stable ReasonCode ────────────────

def _filter_reason_to_code(filter_reason: Optional[str]) -> str:
    """
    Map an EVResult.filter_reason string to the closest ReasonCode.

    Falls back to PICK_NO_EDGE when no match is found.
    """
    if not filter_reason:
        return ReasonCode.PICK_NO_EDGE
    fr = filter_reason.upper()
    if "VALIDATION" in fr:
        return ReasonCode.PICK_VALIDATION_FAILED
    if "WTA DATA GATE" in fr:
        return ReasonCode.PICK_WTA_DATA_GATE
    if "INSUFFICIENT" in fr:
        return ReasonCode.PICK_INSUFFICIENT_DATA
    if "ODDS" in fr:
        return ReasonCode.PICK_NO_MARKET_ODDS
    if "CONFIDENCE" in fr or "LOW CONF" in fr:
        return ReasonCode.PICK_LOW_CONFIDENCE
    return ReasonCode.PICK_NO_EDGE


# ── Main builder ──────────────────────────────────────────────────────────────

def build_evaluator_decision(
    best_ev,
    eval_result: dict,
    validation_passed: bool,
) -> EvaluatorDecision:
    """
    Build an EvaluatorDecision from the raw pipeline signals.

    Parameters
    ----------
    best_ev : EVResult
        Best EV result from compute_ev() (the side with the higher edge).
    eval_result : dict
        Dict returned by evaluator.evaluate(), or {} if unavailable / errored.
    validation_passed : bool
        Whether validate_match() returned passed=True.

    Returns
    -------
    EvaluatorDecision
        Never raises.

    Decision rules (in priority order)
    ------------------------------------
    1. EV filter blocked (best_ev.is_value = False):
       - If validation also failed → BLOCKED_VALIDATION
       - Otherwise → NO_PICK
    2. EV passed, evaluator says "send" or "send_with_caution" → PICK
    3. EV passed, evaluator says "watchlist" → WATCHLIST
    4. EV passed, evaluator says "ignore" or empty → BLOCKED_MODEL
    5. No evaluator available (eval_result = {}) and EV passed → PICK (existing behavior)
    """
    rec_action = eval_result.get("recommended_action", "") if eval_result else ""
    confidence = eval_result.get("confidence") if eval_result else None
    message    = eval_result.get("short_message") if eval_result else None

    # ── Rule 1: EV filter blocked ─────────────────────────────────────────────
    if not best_ev.is_value:
        if not validation_passed:
            return EvaluatorDecision(
                status=EvaluatorStatus.BLOCKED_VALIDATION,
                reason_code=ReasonCode.PICK_VALIDATION_FAILED,
                filter_reason=best_ev.filter_reason or "VALIDATION FAILED",
                confidence=None,
                message=None,
                recommended_action="blocked",
                eval_result=eval_result,
            )
        return EvaluatorDecision(
            status=EvaluatorStatus.NO_PICK,
            reason_code=_filter_reason_to_code(best_ev.filter_reason),
            filter_reason=best_ev.filter_reason,
            confidence=None,
            message=None,
            recommended_action="blocked",
            eval_result=eval_result,
        )

    # ── Rule 2: No evaluator / evaluator errored → default approve ────────────
    if not eval_result or not rec_action:
        return EvaluatorDecision(
            status=EvaluatorStatus.PICK,
            reason_code=ReasonCode.PICK_APPROVED,
            filter_reason=None,
            confidence=confidence,
            message=message,
            recommended_action="send",
            eval_result=eval_result,
        )

    # ── Rule 3: Evaluator approved ────────────────────────────────────────────
    if rec_action in ("send", "send_with_caution"):
        return EvaluatorDecision(
            status=EvaluatorStatus.PICK,
            reason_code=ReasonCode.PICK_APPROVED,
            filter_reason=None,
            confidence=confidence,
            message=message,
            recommended_action=rec_action,
            eval_result=eval_result,
        )

    # ── Rule 4: Evaluator watchlist ───────────────────────────────────────────
    if rec_action == "watchlist":
        return EvaluatorDecision(
            status=EvaluatorStatus.WATCHLIST,
            reason_code=ReasonCode.PICK_WATCHLIST,
            filter_reason="EVALUATOR_WATCHLIST",     # same string as pre-P4 pipeline
            confidence=confidence,
            message=message,
            recommended_action=rec_action,
            eval_result=eval_result,
        )

    # ── Rule 5: Evaluator blocked (ignore / unknown) ──────────────────────────
    return EvaluatorDecision(
        status=EvaluatorStatus.BLOCKED_MODEL,
        reason_code=ReasonCode.PICK_BLOCKED_MODEL,
        filter_reason=f"EVALUATOR_{rec_action.upper()}",  # same string as pre-P4
        confidence=confidence,
        message=message,
        recommended_action=rec_action,
        eval_result=eval_result,
    )
