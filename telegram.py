from __future__ import annotations  # lazy annotation eval — MatchPick not yet in its own module

import logging
import os
import time
from datetime import datetime, timezone

import requests

from tennis_model.formatter import _quality_tier

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.warning("Telegram not configured.")
        return False
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=10,
            )
            r.raise_for_status()
            log.info(f"Telegram sent (attempt {attempt+1})")
            return True
        except Exception as exc:
            log.warning(f"Telegram attempt {attempt+1}/3 failed: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    log.error("Telegram: all 3 attempts failed")
    return False


def _last_name(short_name: str) -> str:
    """'M. Sakkari' → 'Sakkari',  'Sakkari' → 'Sakkari'."""
    parts = short_name.strip().split()
    return parts[-1] if len(parts) > 1 else short_name


def _kelly_stake(prob: float, decimal_odds: float,
                 fraction: float = 0.5, cap: float = 0.05) -> float:
    b = decimal_odds - 1.0
    q = 1.0 - prob
    full_kelly = (b * prob - q) / b if b > 0 else 0.0
    return max(0.0, min(full_kelly * fraction, cap))


def format_telegram_alert(pick: MatchPick) -> str:
    pa, pb = pick.player_a, pick.player_b

    # Identify the picked side
    if pick.pick_player == pb.short_name:
        pick_name  = pb.short_name
        pick_prob  = pick.prob_b
        pick_fo    = pick.fair_odds_b
        pick_odds  = pick.market_odds_b
        pick_edge  = pick.edge_b or 0.0
    else:
        pick_name  = pa.short_name
        pick_prob  = pick.prob_a
        pick_fo    = pick.fair_odds_a
        pick_odds  = pick.market_odds_a
        pick_edge  = pick.edge_a or 0.0

    er = getattr(pick, "evaluator_result", {}) or {}
    is_cautious = er.get("recommended_action") == "send_with_caution"
    kelly_fraction = 0.25 if is_cautious else 0.5
    kelly_label    = "¼" if is_cautious else "½"

    ks     = _kelly_stake(pick_prob, pick_odds, fraction=kelly_fraction) if pick_odds else 0.0
    ks_pct = f"{ks*100:.2f}%"
    ks_ex  = f"£{ks*1000:.0f} on £1k"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"🎾 {pick.tour} {pick.tournament} — {pick.surface}",
        f"{_last_name(pa.short_name)} vs {_last_name(pb.short_name)}",
        f"✅ BACK: {pick_name} @{pick_odds}",
        f"📊 Model: {pick_prob*100:.1f}% | Fair odds: @{pick_fo:.2f}",
        f"⚡ Edge: +{pick_edge:.1f}%",
        f"💹 Kelly {kelly_label}: {ks_pct} ({ks_ex})",
        f"🎯 Confidence: {pick.confidence}",
    ]
    _tier = getattr(pick, "quality_tier", None) or _quality_tier(pick)
    _tier_lines = {
        "CLEAN":   "🟢 Quality: CLEAN",
        "CAUTION": "🟡 Quality: CAUTION — reduced stake",
        "FRAGILE": "🔴 Quality: FRAGILE — minimal stake only",
    }
    lines.append(_tier_lines.get(_tier, "🟡 Quality: CAUTION — reduced stake"))
    lines.append(f"📡 Odds: {getattr(pick, 'odds_source', 'manual').upper()} | ⏰ {now_str}")
    if is_cautious:
        lines.append("⚠️ CAUTION: Reduced stake — model calibrating (N<30)")
    return "\n".join(lines)


def maybe_alert(pick: MatchPick, card: str) -> None:  # noqa: ARG001 (card kept for callers)
    # Pre-alert sanity guards — abort silently, never crash the pipeline
    if not pick.pick_player:
        log.warning("ALERT SUPPRESSED — no pick_player set")
        return
    if pick.prob_a <= 0.0 or pick.prob_b <= 0.0:
        log.warning(f"ALERT SUPPRESSED — invalid probabilities: "
                    f"prob_a={pick.prob_a}, prob_b={pick.prob_b}")
        return
    if pick.market_odds_a is None and pick.market_odds_b is None:
        log.warning("ALERT SUPPRESSED — no market odds on either side")
        return

    # Quality tier gate: suppress FRAGILE alerts (serve n<5 or suspicious edge)
    _tier = getattr(pick, "quality_tier", None) or _quality_tier(pick)
    if _tier == "FRAGILE":
        log.warning(
            f"ALERT SUPPRESSED — FRAGILE quality tier: "
            f"{pick.player_a.short_name} vs {pick.player_b.short_name} "
            f"(flags: {(getattr(pick, 'evaluator_result', {}) or {}).get('risk_flags', [])})"
        )
        return

    from tennis_model.backtest import store_prediction
    store_prediction(pick)
    send_telegram(format_telegram_alert(pick))
