import logging
from dataclasses import dataclass
from tennis_model.validation import ValidationResult

log = logging.getLogger(__name__)

SUSPICIOUS_EDGE_THRESHOLD = 0.50   # edges above 50% are flagged for manual review
MIN_ODDS   = 1.50   # hard floor: below this, vig kills edge sustainability
MAX_ODDS   = 3.00   # soft ceiling: warn; variable threshold already handles edge requirement
PROB_FLOOR = 0.40   # minimum model probability to bet (no deep underdogs)


@dataclass
class EVResult:
    edge:          float
    is_value:      bool
    filter_reason: str | None = None


def _min_edge_for_odds(market_odds: float, tour: str = "atp") -> float:
    """Variable minimum edge by odds tier.
    WTA and Challenger markets carry higher vig (~8-10% vs ~5-6% ATP main draw)
    so require +1% additional edge to remain profitable after juice.

    Spec:
        odds <= 1.75 → 3%
        odds <= 2.20 → 4%
        odds <= 2.80 → 5%
        odds >  2.80 → 6%
    """
    if market_odds <= 1.75:
        base = 0.03
    elif market_odds <= 2.20:
        base = 0.04
    elif market_odds <= 2.80:
        base = 0.05
    else:
        base = 0.06
    if tour.lower() in ("wta", "challenger"):
        base += 0.01
    return base


def compute_ev(market_odds: float,
               fair_odds:   float,
               validation:  ValidationResult,
               confidence:  str,
               days_inactive: int = 0,
               tour: str = "atp") -> EVResult:

    # --- Basic odds sanity: guard before any division or edge computation ---
    if not market_odds or not fair_odds or market_odds <= 1.0 or fair_odds <= 1.0:
        log.warning(
            f"Invalid odds — market={market_odds} fair={fair_odds}: "
            f"skipping edge computation"
        )
        return EVResult(edge=0.0, is_value=False, filter_reason="INVALID_ODDS")

    edge = round((market_odds / fair_odds) - 1, 4)

    # --- Suspicious edge cap (model miscalibration guard) ---
    if edge > SUSPICIOUS_EDGE_THRESHOLD:
        log.warning(
            f"Edge {edge*100:.1f}% exceeds {SUSPICIOUS_EDGE_THRESHOLD*100:.0f}% — "
            f"likely model miscalibration, manual review required"
        )
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"SUSPICIOUS EDGE {edge*100:.1f}% — MANUAL REVIEW REQUIRED")

    # --- Hard blocks (NO BET regardless of edge) ---

    if not validation.passed:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"VALIDATION FAILED: {validation.errors[0]}")

    if days_inactive != -1 and days_inactive > 60:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"INACTIVE {days_inactive} DAYS (> 60)")

    # --- Probability floor: don't bet deep underdogs ---
    # model_prob = 1 / fair_odds (fair_odds is derived from blended model probability)
    model_prob = 1.0 / fair_odds
    if model_prob < PROB_FLOOR:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"MODEL PROB {model_prob:.1%} BELOW FLOOR ({PROB_FLOOR:.0%})")

    # --- Odds range filter ---
    # Hard floor at 1.50: below this, vig relative to potential return is unworkable
    if market_odds < MIN_ODDS:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"ODDS @{market_odds:.2f} BELOW MINIMUM ({MIN_ODDS})")
    # Soft ceiling: warn but do not block (variable threshold enforces higher edge req above 2.80)
    if market_odds > MAX_ODDS:
        log.warning(
            f"Odds @{market_odds:.2f} outside preferred range (≤{MAX_ODDS}) — "
            f"high variance, higher threshold applied"
        )

    # --- LOW confidence: hard-block regardless of edge ---
    # A low-confidence rating indicates insufficient data quality or thin sample;
    # no edge calculation can be trusted at this tier.
    if confidence == "LOW":
        return EVResult(edge=edge, is_value=False,
            filter_reason="LOW CONFIDENCE — no bet")

    # --- Variable minimum threshold by odds tier ---
    _min = _min_edge_for_odds(market_odds, tour)
    if edge < _min:
        return EVResult(edge=edge, is_value=False,
            filter_reason=(
                f"EDGE {edge*100:.1f}% BELOW THRESHOLD "
                f"({_min*100:.0f}% at @{market_odds:.2f} [{tour.upper()}])"
            ))

    return EVResult(edge=edge, is_value=True, filter_reason=None)


def strip_vig(odds_a: float, odds_b: float):
    """Return vig-stripped implied probabilities (sum to 1.0)."""
    raw_a = 1 / odds_a
    raw_b = 1 / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total
