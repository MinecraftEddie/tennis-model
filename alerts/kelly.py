"""
alerts/kelly.py
===============
Fractional Kelly stake sizing.

Formula:
    kelly_full = (p * odds - 1) / (odds - 1)
    stake      = kelly_full * 0.25           (1/4 Kelly)

Constraints:
    kelly_full <= 0  →  return None  (no positive edge — do not bet)
    stake < MIN_STAKE_UNITS  →  clamp up to MIN_STAKE_UNITS (env, default 0.05)
    stake > MAX_STAKE_UNITS  →  clamp down to MAX_STAKE_UNITS (env, default 1.0)

Env vars:
    MIN_STAKE_UNITS  (default 0.05)
    MAX_STAKE_UNITS  (default 1.0)
"""
import logging
from typing import Optional

from tennis_model.config.runtime_config import KELLY_FRACTION, MIN_STAKE_UNITS, MAX_STAKE_UNITS
from tennis_model.models import MatchPick

log = logging.getLogger(__name__)


def compute_stake(prob: float, odds: float) -> Optional[float]:
    """
    Return the 1/4-Kelly stake in units, or None if Kelly <= 0.

    Args:
        prob: model win probability for the picked side (0 < p < 1)
        odds: decimal market odds for the picked side (> 1.0)

    Returns:
        stake clamped to [MIN_STAKE_UNITS, MAX_STAKE_UNITS] from runtime_config, or None if no edge.
    """
    if odds <= 1.0 or prob <= 0.0 or prob >= 1.0:
        return None
    kelly = (prob * odds - 1.0) / (odds - 1.0)
    if kelly <= 0.0:
        return None
    raw   = kelly * KELLY_FRACTION
    stake = max(MIN_STAKE_UNITS, min(raw, MAX_STAKE_UNITS))
    return round(stake, 4)


def stake_for_pick(pick: MatchPick) -> Optional[float]:
    """Derive Kelly stake from the pick's chosen side probability and market odds."""
    ps = pick.picked_side()
    if ps is None:
        return None
    return compute_stake(ps["prob"], ps["market_odds"] or 0.0)
