from dataclasses import dataclass
from tennis_model.validation import ValidationResult


@dataclass
class EVResult:
    edge:          float
    is_value:      bool
    filter_reason: str | None = None


def compute_ev(market_odds: float,
               fair_odds:   float,
               validation:  ValidationResult,
               confidence:  str,
               days_inactive: int = 0) -> EVResult:

    edge = round((market_odds / fair_odds) - 1, 4)

    # --- Hard blocks (NO BET regardless of edge) ---

    if not validation.passed:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"VALIDATION FAILED: {validation.errors[0]}")

    if days_inactive > 60:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"INACTIVE {days_inactive} DAYS (> 60)")

    if confidence == "LOW" and edge < 0.15:
        return EVResult(edge=edge, is_value=False,
            filter_reason="LOW CONFIDENCE + WEAK EDGE (< 15%)")

    # --- Minimum threshold ---
    if edge < 0.07:
        return EVResult(edge=edge, is_value=False,
            filter_reason=f"EDGE {edge*100:.1f}% BELOW THRESHOLD (7%)")

    return EVResult(edge=edge, is_value=True, filter_reason=None)


def strip_vig(odds_a: float, odds_b: float):
    """Return vig-stripped implied probabilities (sum to 1.0)."""
    raw_a = 1 / odds_a
    raw_b = 1 / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total
