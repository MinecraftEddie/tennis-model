from __future__ import annotations  # lazy annotation eval — MatchPick not yet in its own module

import logging
import os
import time

import requests

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
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
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


def maybe_alert(pick: MatchPick, card: str) -> None:
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

    from tennis_model.backtest import store_prediction
    store_prediction(pick)
    send_telegram(f"⚡ <b>EDGE ALERT</b>\n\n{card}")
