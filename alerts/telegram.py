"""
Deduplicating Telegram notifier.

Wraps tennis_model.telegram.maybe_alert with a dedupe check so that the same
pick is never alerted twice across scheduler runs — even if scan_today() is
called repeatedly within the same day.

Usage (orchestration/jobs.py injects this into the pipeline before scanning):

    import tennis_model.pipeline as _pipeline
    from tennis_model.alerts.telegram import make_deduped_maybe_alert

    _pipeline.maybe_alert = make_deduped_maybe_alert(store, dry_run=False)
    _pipeline.scan_today()
    _pipeline.maybe_alert = original  # restore afterwards
"""
import logging
import unicodedata
from datetime import date

import tennis_model.telegram as _tg
from tennis_model.alerts import kelly as _kelly
from tennis_model.alerts import risk_caps
from tennis_model.models import MatchPick
from tennis_model.storage.dedupe import DedupeStore, MODEL_VERSION

log = logging.getLogger(__name__)


def _canon_last(name: str) -> str:
    """
    Canonical last-name token for dedupe keys.

    Rules applied in order:
      1. Strip surrounding whitespace, collapse internal runs
      2. Take the last whitespace-separated token (handles "C. Alcaraz",
         "Carlos Alcaraz", "Alcaraz", "B. Haddad Maia")
      3. Lowercase
      4. Strip Unicode diacritics via NFKD decomposition so that
         "Muñoz" == "Munoz", "García" == "Garcia", "Šaric" == "Saric"

    Returns a plain ASCII-ish string suitable as a stable dict / DB key.
    """
    name = " ".join(name.strip().split())          # normalise whitespace
    last = name.split()[-1] if name else name       # rightmost token = surname
    last = last.lower()
    nfkd = unicodedata.normalize("NFKD", last)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _match_id(pick: MatchPick) -> str:
    """
    Stable, order-independent dedupe key: YYYY-MM-DD_<last_x>_<last_y>

    Player last names are sorted alphabetically so that swapped player order
    (common across bookmaker API responses between polls) always yields the
    same string.  Uses full_name when available to stay consistent with
    backtest._make_id().
    """
    today = date.today().strftime("%Y-%m-%d")
    name_a = pick.player_a.full_name or pick.player_a.short_name
    name_b = pick.player_b.full_name or pick.player_b.short_name
    parts = sorted([_canon_last(name_a), _canon_last(name_b)])
    return f"{today}_{parts[0]}_{parts[1]}"


def make_deduped_maybe_alert(store: DedupeStore, dry_run: bool = False):
    """
    Return a replacement for pipeline's maybe_alert that:
      - Skips picks already in the dedupe store (same match + picked side + model version)
      - In dry_run mode: logs the formatted alert without sending or storing backtest entry
      - Otherwise: delegates to the original maybe_alert (quality gates + backtest store + send)
      - Marks the pick as sent after the first attempt so re-scans skip it

    Returns an AlertDecision for every exit path (P3).
    """
    from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision
    from tennis_model.evaluator.risk_engine import compute_risk_decision
    from tennis_model.quality.reason_codes import ReasonCode

    original = _tg.maybe_alert  # snapshot the real function before patch

    def deduped_maybe_alert(pick: MatchPick, card: str) -> AlertDecision:
        if not pick.pick_player:
            return AlertDecision(
                status=AlertStatus.SKIPPED_NO_PICK,
                reason_code=ReasonCode.ALERT_SUPPRESSED_NO_PICK,
                stake_units=None, stake_factor=0.0,
                telegram_attempted=False, telegram_sent=False,
            )

        mid = _match_id(pick)
        side = pick.pick_player
        log.info(f"[ALERT] evaluating {mid} → side={side!r}")

        if store.already_sent(mid, side, MODEL_VERSION):
            log.info(f"[DEDUPE] skip already-alerted: {mid} → {side}")
            return AlertDecision(
                status=AlertStatus.SKIPPED_DEDUPE,
                reason_code=ReasonCode.ALERT_SKIPPED_DEDUPE,
                stake_units=None, stake_factor=0.0,
                telegram_attempted=False, telegram_sent=False,
            )

        blocked, reason = risk_caps.check()
        if blocked:
            log.warning(f"[RISK] alert blocked ({reason}): {mid} → {side}")
            return AlertDecision(
                status=AlertStatus.SKIPPED_RISK,
                reason_code=ReasonCode.ALERT_SKIPPED_RISK,
                stake_units=None, stake_factor=0.0,
                telegram_attempted=False, telegram_sent=False,
            )
        log.info(f"[RISK] caps OK for {mid}")

        stake = _kelly.stake_for_pick(pick)
        if stake is None:
            log.warning(f"[KELLY] Kelly <= 0 — alert blocked: {mid} → {side}")
            return AlertDecision(
                status=AlertStatus.SKIPPED_KELLY,
                reason_code=ReasonCode.ALERT_SUPPRESSED_KELLY_ZERO,
                stake_units=None, stake_factor=0.0,
                telegram_attempted=False, telegram_sent=False,
            )
        pick.stake_units = stake

        if dry_run:
            # Apply risk decision (includes DEGRADED reduction) before logging
            quality_a = getattr(pick.player_a, "profile_quality", "full")
            quality_b = getattr(pick.player_b, "profile_quality", "full")
            risk = compute_risk_decision(quality_a, quality_b, pick.stake_units)
            if not risk.allowed:
                return AlertDecision(
                    status=AlertStatus.SKIPPED_UNKNOWN if risk.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN else AlertStatus.SKIPPED_KELLY,
                    reason_code=risk.reason_code,
                    stake_units=0.0, stake_factor=risk.stake_factor,
                    telegram_attempted=False, telegram_sent=False,
                )
            pick.stake_units = risk.stake_units
            er = getattr(pick, "evaluator_result", {}) or {}
            msg = _tg.format_telegram_alert(pick)
            log.info(
                f"[DRY-RUN] Would send Telegram alert "
                f"(evaluator={er.get('recommended_action', 'n/a')}):\n{msg}"
            )
            store.mark_sent(mid, side, MODEL_VERSION)
            return AlertDecision(
                status=AlertStatus.DRY_RUN,
                reason_code=ReasonCode.TELEGRAM_DRY_RUN,
                stake_units=risk.stake_units,
                stake_factor=risk.stake_factor,
                telegram_attempted=False,
                telegram_sent=False,
                message_preview=msg[:100] if msg else None,
            )

        # Delegate to the real maybe_alert (quality gates → backtest store → send)
        log.info(f"[ALERT] → dispatching to real maybe_alert: {mid} → {side!r}")
        decision = original(pick, card)
        store.mark_sent(mid, side, MODEL_VERSION)
        return decision

    return deduped_maybe_alert
