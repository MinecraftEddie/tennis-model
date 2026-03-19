"""
Watchlist management for alert evaluator.
Logs watchlist picks separately from value picks (send).
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "watchlist.json")


def log_watchlist_item(alert_decision: dict) -> None:
    """
    Log a watchlist item to data/watchlist.json.
    
    Args:
        alert_decision: dict from evaluator.evaluate() with recommended_action='watchlist'
    """
    if alert_decision.get("recommended_action") != "watchlist":
        return
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(WATCHLIST_FILE), exist_ok=True)
    
    # Load existing watchlist
    watchlist = []
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
                watchlist = json.load(f)
        except (json.JSONDecodeError, IOError):
            watchlist = []
    
    # Add timestamp and entry
    entry = {
        **alert_decision,
        "timestamp": datetime.utcnow().isoformat(),
    }
    watchlist.append(entry)
    
    # Write back (keep most recent 100 items)
    try:
        with open(WATCHLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(watchlist[-100:], f, indent=2, ensure_ascii=False)
        log.info(f"WATCHLIST: {alert_decision['short_message']}")
    except IOError as e:
        log.error(f"Failed to write watchlist: {e}")


def get_watchlist(limit: int = 50) -> list[dict]:
    """
    Retrieve recent watchlist items.
    
    Args:
        limit: max number of items to return (most recent first)
        
    Returns:
        list of watchlist entries
    """
    if not os.path.exists(WATCHLIST_FILE):
        return []
    
    try:
        with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
            items = json.load(f)
        return sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
    except (json.JSONDecodeError, IOError):
        return []


def format_watchlist(items: list[dict] = None) -> str:
    """
    Format watchlist for display in CLI or logging.
    
    Args:
        items: list of watchlist entries (if None, reads from file)
        
    Returns:
        formatted string for display
    """
    if items is None:
        items = get_watchlist(20)
    
    if not items:
        return "Watchlist is empty."
    
    lines = [
        f"\n{'='*80}",
        f"WATCHLIST ({len(items)} items)",
        f"{'='*80}\n",
    ]
    
    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. {item.get('short_message', 'N/A')}\n"
            f"   Match: {item.get('match_id', 'N/A')}\n"
            f"   Confidence: {item.get('confidence', 0):.0%}\n"
            f"   Reason: {', '.join(item.get('reasons', [])[:2])}\n"
            f"   Risks: {', '.join(item.get('risk_flags', [])[:2]) if item.get('risk_flags') else 'None'}\n"
            f"   Timestamp: {item.get('timestamp', 'N/A')}\n"
        )
    
    lines.append(f"{'='*80}\n")
    return "".join(lines)


def clear_watchlist() -> None:
    """Clear the watchlist file."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            os.remove(WATCHLIST_FILE)
            log.info("Watchlist cleared")
        except IOError as e:
            log.error(f"Failed to clear watchlist: {e}")
