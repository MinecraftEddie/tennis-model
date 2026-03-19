"""
Tennis Alert Evaluator: decision layer for model outputs.

Takes MatchPick + optional match context, applies tennis-specific rules,
returns structured alert decision JSON.

No API calls, no data fetching, no probability recalculation.
Pure rule-based evaluation.
"""

from tennis_model.evaluator.evaluator import evaluate
from tennis_model.evaluator.watchlist import log_watchlist_item, get_watchlist, format_watchlist, clear_watchlist

__all__ = ["evaluate", "log_watchlist_item", "get_watchlist", "format_watchlist", "clear_watchlist"]
