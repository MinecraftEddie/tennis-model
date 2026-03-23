"""
probability_adjustments.py
===========================
Post-model probability adjustments applied BEFORE edge calculation.

These functions do NOT modify the model output or confidence logic.
They are inserted between model probability and fair-odds/edge computation
in pipeline.py only.
"""

SHRINK_ALPHA = 0.70   # 70% model, 30% market


def logit_stretch(p: float, gamma: float = 1.35) -> float:
    """
    Logit-space stretch: expand compressed probabilities toward realistic ranges.

    Applied AFTER market shrink, BEFORE fair_odds computation.
    gamma > 1.0 pushes strong favourites higher and underdogs lower.
    gamma = 1.0 is identity; p = 0.50 is always a fixed point.

    Typical: p=0.70 → 0.77, p=0.80 → 0.88, p=0.50 → 0.50

    Moved from a local lambda inside run_match_with_result() in P6 so that
    orchestration/match_runner.run_match_core() can import it cleanly.
    """
    import math
    p = max(0.01, min(0.99, p))
    return 1.0 / (1.0 + math.exp(-math.log(p / (1.0 - p)) * gamma))


def shrink_toward_market(model_prob: float, odds: float) -> float:
    """
    Shrink model probability toward the raw market-implied probability.

    Reduces inflated edges when the model is far from the market price,
    using a simple linear interpolation (standard calibration technique).

    Args:
        model_prob: model win probability for this side (0 < p < 1)
        odds:       decimal market odds for this side (> 1.0)

    Returns:
        alpha * model_prob + (1 - alpha) * market_prob
        where market_prob = 1 / odds  and  alpha = SHRINK_ALPHA
    """
    if odds <= 1.0:
        return model_prob          # degenerate odds — no adjustment
    market_prob = 1.0 / odds
    return SHRINK_ALPHA * model_prob + (1.0 - SHRINK_ALPHA) * market_prob
