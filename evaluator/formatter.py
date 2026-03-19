"""
Output formatting for alert decisions.
Builds strict JSON with all required fields.
"""
import json
from typing import Optional


def build_alert_decision(
    match_id: str,
    alert_level: str,  # "low" | "medium" | "high"
    confidence: float,
    recommended_action: str,  # "send" | "watchlist" | "ignore"
    reasons: list[str],
    risk_flags: list[str],
    short_message: str,
) -> dict:
    """
    Build strict alert decision JSON.
    
    Args:
        match_id: unique match identifier
        alert_level: "low", "medium", or "high"
        confidence: 0.0-1.0 confidence score
        recommended_action: "send", "watchlist", or "ignore"
        reasons: list of reasoning strings (max 5)
        risk_flags: list of risk signal strings
        short_message: one-liner alert text
        
    Returns:
        dict with all required fields
    """
    confidence = max(0.0, min(1.0, confidence))
    
    return {
        "match_id": match_id,
        "alert_level": alert_level,
        "confidence": round(confidence, 2),
        "recommended_action": recommended_action,
        "reasons": reasons[:5],  # cap at 5 reasons
        "risk_flags": risk_flags,
        "short_message": short_message,
    }


def serialize_alert(alert_dict: dict) -> str:
    """Serialize alert decision to JSON string (compact)."""
    return json.dumps(alert_dict, separators=(',', ':'))


def format_alert_readable(alert_dict: dict) -> str:
    """Format alert for human consumption (pretty)."""
    return json.dumps(alert_dict, indent=2)
