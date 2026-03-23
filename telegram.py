import logging
import os
import time
from datetime import datetime, timezone

import requests

from tennis_model.models import MatchPick, PlayerProfile  # noqa: F401
from tennis_model.formatter import _quality_tier

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    _tok_ok  = TELEGRAM_BOT_TOKEN not in ("", "YOUR_BOT_TOKEN_HERE")
    _chat_ok = TELEGRAM_CHAT_ID not in ("", "YOUR_CHAT_ID_HERE")
    log.info(
        f"[TELEGRAM] send_telegram called — "
        f"token={'SET' if _tok_ok else 'NOT SET'}  "
        f"chat_id={TELEGRAM_CHAT_ID!r} ({'SET' if _chat_ok else 'NOT SET'})"
    )
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


def check_telegram_config() -> bool:
    """
    Checks Telegram credentials at startup. Call once before scanning.
    Returns True if configured, False if running in dry-run mode.
    Logs a clear status line either way — no spamming on each match.
    """
    tok_ok  = TELEGRAM_BOT_TOKEN not in ("", "YOUR_BOT_TOKEN_HERE")
    chat_ok = TELEGRAM_CHAT_ID   not in ("", "YOUR_CHAT_ID_HERE")
    if tok_ok and chat_ok:
        log.info(
            f"[TELEGRAM] Configured — chat_id={TELEGRAM_CHAT_ID} "
            f"[TELEGRAM_SEND_OK]"
        )
        return True
    missing = []
    if not tok_ok:  missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_ok: missing.append("TELEGRAM_CHAT_ID")
    log.warning(
        f"[TELEGRAM] NOT CONFIGURED [TELEGRAM_NOT_CONFIGURED]\n"
        f"  Missing env vars: {', '.join(missing)}\n"
        f"  Set via env or config.json → telegram.bot_token / chat_id\n"
        f"  Running in dry_run mode — no alerts will be sent."
    )
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

    picked     = pick.require_picked_side()
    pick_name  = picked["player"].short_name
    pick_prob  = picked["prob"]
    pick_fo    = picked["fair_odds"]
    pick_odds  = picked["market_odds"]
    pick_edge  = picked["edge"] or 0.0

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


def maybe_alert(pick: MatchPick, card: str) -> "AlertDecision":  # noqa: ARG001
    """
    Run final quality gates, compute stake, and dispatch Telegram alert.

    Returns an AlertDecision describing the outcome — never raises.
    The return value should be passed to DailyAudit.record_alert_decision().
    """
    from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision
    from tennis_model.evaluator.risk_engine import compute_risk_decision
    from tennis_model.quality.reason_codes import ReasonCode

    # ── Pre-alert sanity guards ────────────────────────────────────────────────
    if not pick.pick_player:
        log.warning("ALERT SUPPRESSED — no pick_player set [ALERT_SUPPRESSED_NO_PICK]")
        return AlertDecision(
            status=AlertStatus.SKIPPED_NO_PICK,
            reason_code=ReasonCode.ALERT_SUPPRESSED_NO_PICK,
            stake_units=None, stake_factor=0.0,
            telegram_attempted=False, telegram_sent=False,
        )

    if pick.prob_a <= 0.0 or pick.prob_b <= 0.0:
        log.warning(
            f"ALERT SUPPRESSED — invalid probabilities: "
            f"prob_a={pick.prob_a}, prob_b={pick.prob_b} [ALERT_SUPPRESSED_NO_ODDS]"
        )
        return AlertDecision(
            status=AlertStatus.SKIPPED_NO_PICK,
            reason_code=ReasonCode.ALERT_SUPPRESSED_NO_ODDS,
            stake_units=None, stake_factor=0.0,
            telegram_attempted=False, telegram_sent=False,
        )

    if pick.market_odds_a is None and pick.market_odds_b is None:
        log.warning("ALERT SUPPRESSED — no market odds on either side [ALERT_SUPPRESSED_NO_ODDS]")
        return AlertDecision(
            status=AlertStatus.SKIPPED_NO_PICK,
            reason_code=ReasonCode.ALERT_SUPPRESSED_NO_ODDS,
            stake_units=None, stake_factor=0.0,
            telegram_attempted=False, telegram_sent=False,
        )

    # ── Quality tier gate: suppress FRAGILE ───────────────────────────────────
    _tier = getattr(pick, "quality_tier", None) or _quality_tier(pick)
    if _tier == "FRAGILE":
        log.warning(
            f"ALERT SUPPRESSED — FRAGILE quality tier: "
            f"{pick.player_a.short_name} vs {pick.player_b.short_name} "
            f"(flags: {(getattr(pick, 'evaluator_result', {}) or {}).get('risk_flags', [])})"
            f" [ALERT_SUPPRESSED_FRAGILE]"
        )
        return AlertDecision(
            status=AlertStatus.SUPPRESSED,
            reason_code=ReasonCode.ALERT_SUPPRESSED_FRAGILE,
            stake_units=None, stake_factor=0.0,
            telegram_attempted=False, telegram_sent=False,
        )

    # ── Kelly stake (compute if not already set by dedup wrapper) ─────────────
    kelly_stake = pick.stake_units
    if kelly_stake is None:
        from tennis_model.alerts.kelly import stake_for_pick as _kelly_fn
        kelly_stake = _kelly_fn(pick)

    # ── Risk engine: UNKNOWN / DEGRADED / Kelly gate ──────────────────────────
    quality_a = getattr(pick.player_a, "profile_quality", "full")
    quality_b = getattr(pick.player_b, "profile_quality", "full")
    risk = compute_risk_decision(quality_a, quality_b, kelly_stake)

    if not risk.allowed:
        if risk.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN:
            status = AlertStatus.SKIPPED_UNKNOWN
            log.warning(
                f"ALERT SUPPRESSED — UNKNOWN profile quality "
                f"(pa={quality_a}, pb={quality_b}) [ALERT_SKIPPED_UNKNOWN]"
            )
        else:
            status = AlertStatus.SKIPPED_KELLY
            log.warning("ALERT SUPPRESSED — Kelly <= 0 (no positive edge) [ALERT_SUPPRESSED_KELLY_ZERO]")
        return AlertDecision(
            status=status,
            reason_code=risk.reason_code,
            stake_units=0.0, stake_factor=risk.stake_factor,
            telegram_attempted=False, telegram_sent=False,
            risk_decision=risk,  # P6: expose RiskDecision in AlertDecision
        )

    # Apply final stake (includes any DEGRADED reduction)
    pick.stake_units = risk.stake_units
    if risk.stake_factor < 1.0:
        log.info(
            f"[QUALITY] DEGRADED profile — stake reduced: "
            f"{kelly_stake:.4f} × {risk.stake_factor} = {risk.stake_units:.4f} "
            f"[{ReasonCode.ALERT_DEGRADED_STAKE_REDUCED}] (pa={quality_a}, pb={quality_b})"
        )

    # ── Store prediction + dispatch Telegram ──────────────────────────────────
    from tennis_model.backtest import store_prediction
    store_prediction(pick)

    msg = format_telegram_alert(pick)
    tg_configured = TELEGRAM_BOT_TOKEN not in ("", "YOUR_BOT_TOKEN_HERE")
    tg_sent = send_telegram(msg)

    if not tg_configured:
        status = AlertStatus.DRY_RUN
        rc     = ReasonCode.TELEGRAM_NOT_CONFIGURED
    elif tg_sent:
        status = AlertStatus.SENT
        rc     = ReasonCode.TELEGRAM_SEND_OK
    else:
        status = AlertStatus.FAILED
        rc     = ReasonCode.TELEGRAM_FAILED

    return AlertDecision(
        status=status,
        reason_code=rc,
        stake_units=pick.stake_units,
        stake_factor=risk.stake_factor,
        telegram_attempted=tg_configured,
        telegram_sent=tg_sent,
        message_preview=msg[:100] if msg else None,
        risk_decision=risk,  # P6: expose RiskDecision in AlertDecision
    )
