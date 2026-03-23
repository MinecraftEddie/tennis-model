"""
Scheduled job functions for the tennis agent.

scan_matches_job   — runs every SCAN_INTERVAL_MINUTES (default 15)
report_job         — placeholder, fires once daily at 07:00 UTC

Dedupe mechanics
----------------
Before calling scan_today(), this module patches pipeline.maybe_alert with a
deduplicating wrapper.  The wrapper intercepts the MatchPick object (which the
pipeline already has) and:
  1. Computes  match_id = YYYY-MM-DD_lastname_a_lastname_b
  2. Checks    DedupeStore.already_sent(match_id, picked_side, model_version)
  3. If new  → calls the original maybe_alert (quality gates + backtest + send)
  4. Marks   → DedupeStore.mark_sent(...)

The patch targets tennis_model.pipeline.maybe_alert (the module-level name that
scan_today / run_match look up at call time), so no changes to pipeline.py are
needed.  The original is always restored in a finally block.
"""
import logging

import tennis_model.pipeline as _pipeline
from tennis_model.alerts.telegram import make_deduped_maybe_alert
from tennis_model.storage.dedupe import DedupeStore

log = logging.getLogger(__name__)

# Module-level singleton — created once, reused across all job invocations
_store = DedupeStore()


def scan_matches_job(dry_run: bool = False) -> None:
    """
    Fetch the current odds slate, run the model pipeline for every match,
    and send Telegram alerts only for picks not already in the dedupe store.

    Parameters
    ----------
    dry_run : bool
        When True, log the formatted alert instead of sending to Telegram.
        The pick is still written to the dedupe store so subsequent dry-run
        invocations also deduplicate correctly.
    """
    log.info(f"[JOB] scan_matches_job start (dry_run={dry_run})")
    if dry_run:
        log.info("[JOB] dry_run=True — Telegram alerts will be LOGGED only, not sent")

    original_maybe_alert = _pipeline.maybe_alert
    _pipeline.maybe_alert = make_deduped_maybe_alert(_store, dry_run=dry_run)

    try:
        _pipeline.scan_today()
    except Exception:
        log.exception("[JOB] scan_matches_job — unhandled error in scan_today()")
    finally:
        _pipeline.maybe_alert = original_maybe_alert

    log.info("[JOB] scan_matches_job done")


def _format_report_summary(days: dict) -> str:
    """
    Build a concise Telegram-ready summary from daily_report.report() output.
    days: {date_str: day_data} as returned by daily_report.report().
    """
    from datetime import date as _date
    from tennis_model.storage.dedupe import MODEL_VERSION

    today = _date.today().strftime("%Y-%m-%d")
    lines = [f"📊 Trading Report — {today}", "─" * 36]

    # Aggregate totals across all days in the report
    total_settled = sum(d.get("settled_count", 0) for d in days.values())
    total_wins    = sum(d.get("wins",          0) for d in days.values())
    total_losses  = sum(d.get("losses",        0) for d in days.values())
    total_voids   = sum(d.get("voids",         0) for d in days.values())
    total_pnl     = sum(d.get("overall", {}).get("pnl", 0.0) for d in days.values())

    # Weighted average edge across days
    edge_sum, edge_n = 0.0, 0
    for d in days.values():
        ov  = d.get("overall", {})
        ae  = ov.get("avg_edge")
        cnt = ov.get("count", 0)
        if ae is not None and cnt > 0:
            edge_sum += ae * cnt
            edge_n   += cnt
    avg_edge = round(edge_sum / edge_n, 2) if edge_n else None

    roi = round(total_pnl / total_settled * 100, 2) if total_settled else None

    if total_settled == 0:
        lines.append("No settled bets.")
    else:
        void_str = f" / {total_voids}V" if total_voids else ""
        wr_str   = f"{total_wins / total_settled * 100:.1f}%"
        pnl_str  = f"{total_pnl:+.2f}u"
        roi_str  = f"{roi:+.1f}%" if roi is not None else "—"
        edge_str = f"{avg_edge:+.2f}%" if avg_edge is not None else "—"

        lines.append(f"Settled : {total_settled}  {total_wins}W / {total_losses}L{void_str}")
        lines.append(f"P&L     : {pnl_str}  |  ROI: {roi_str}")
        lines.append(f"Win rate: {wr_str}  |  Avg edge: {edge_str}")

        # Best win across all days
        best_win = None
        for d in days.values():
            bw = d.get("best_win")
            if bw and (best_win is None or
                       bw.get("pnl_units", 0.0) > best_win.get("pnl_units", 0.0)):
                best_win = bw
        if best_win:
            bw_name = best_win.get("picked_side") or "?"
            bw_pnl  = best_win.get("pnl_units", 0.0)
            bw_odds = best_win.get("settled_odds") or best_win.get("pick_odds") or "?"
            lines.append(f"Best    : {bw_name} WIN {bw_pnl:+.2f}u @{bw_odds}")

    lines.append("─" * 36)
    lines.append(f"Model v{MODEL_VERSION}")
    return "\n".join(lines)


def report_job() -> None:
    """
    Daily summary: fires once per day at 07:00 UTC.
    Calls daily_report.report(), calibration, and blocked_picks_audit in sequence.
    Each section is wrapped independently so a single failure does not crash the scheduler.
    Sends a concise summary to Telegram after all reports have run.
    """
    log.info("[JOB] report_job start")

    days: dict = {}
    try:
        from tennis_model.tracking.daily_report import report
        days = report() or {}
    except Exception:
        log.exception("[JOB] report_job — daily_report.report() failed")

    try:
        from tennis_model.reporting.calibration import compute_calibration, print_calibration
        from tennis_model.tracking.daily_report import _load_jsonl, _SETTLED_FILE
        preds = _load_jsonl(_SETTLED_FILE)
        cal = compute_calibration(preds)
        print_calibration(cal)
    except Exception:
        log.exception("[JOB] report_job — calibration failed")

    try:
        from tennis_model.tracking.blocked_picks_audit import audit
        audit()
    except Exception:
        log.exception("[JOB] report_job — blocked_picks_audit.audit() failed")

    try:
        from tennis_model.telegram import send_telegram
        msg = _format_report_summary(days)
        sent = send_telegram(msg)
        if not sent:
            log.warning("[JOB] report_job — Telegram delivery failed or not configured")
    except Exception:
        log.exception("[JOB] report_job — Telegram send failed")

    log.info("[JOB] report_job done")


# ──────────────────────────────────────────────────────────────────────────────
# SETTLEMENT JOB
# ──────────────────────────────────────────────────────────────────────────────

def _notify_settlement(match_id: str, result: str, pnl) -> None:
    """Send a short Telegram message for one settled match. Never raises."""
    try:
        from tennis_model.telegram import send_telegram
        icon    = {"WIN": "✅", "LOSS": "❌", "VOID": "↩️", "NO_BET": "⏭️"}.get(result, "📋")
        pnl_str = f"  P&L: {pnl:+.2f}u" if pnl is not None else ""
        send_telegram(f"{icon} Settled: {match_id}\nResult: {result}{pnl_str}")
    except Exception:
        log.warning(f"[JOB] settlement notification failed for {match_id}")


def settlement_job() -> None:
    """
    Periodic settlement check — runs every 30 minutes.

    Loads unsettled forward predictions via pending() and attempts to settle
    each one using a result source.

    Result source: TheSportsDB free API (eventsday, leagues ATP 4464 / WTA 4517).
    Coverage is community-maintained; not_found / ambiguous → safe skip.

    Safety rules enforced:
    - Never settles on ambiguous name match (settle() rejects those internally)
    - Each match is wrapped in its own try/except — one failure never stops the rest
    - Fully idempotent: pending() excludes already-settled IDs
    """
    log.info("[JOB] settlement_job start")

    try:
        from tennis_model.tracking.settle_predictions import pending, settle, void_match
    except Exception:
        log.exception("[JOB] settlement_job — failed to import settle_predictions")
        return

    try:
        unsettled = pending()
    except Exception:
        log.exception("[JOB] settlement_job — pending() failed")
        return

    n_checked = len(unsettled)
    n_settled = n_voided = n_skipped = n_errors = 0

    log.info(f"[JOB] settlement_job — {n_checked} unsettled prediction(s) found")

    # ── RESULT SOURCE — TheSportsDB ──────────────────────────────────────────
    # fetch_match_result returns {"status", "winner", "source", "event_id"}.
    # The adapter below maps that to the {"winner", "void"} shape the loop
    # expects, and returns None for any non-final status (safe skip).
    result_source = None
    try:
        from tennis_model.integrations.thesportsdb_results import fetch_match_result as _tsdb

        def result_source(mid, player_a, player_b, date):  # noqa: F811
            res    = _tsdb(player_a, player_b, date)
            status = res.get("status")
            if status == "final":
                return {"winner": res["winner"], "void": False}
            if status == "void":
                return {"winner": None, "void": True}
            return None   # not_finished / ambiguous / not_found → skip

        log.debug("[JOB] settlement_job — result_source: TheSportsDB")
    except Exception:
        log.exception("[JOB] settlement_job — could not load TheSportsDB result source")
    # ─────────────────────────────────────────────────────────────────────────

    if result_source is None:
        for rec in unsettled:
            mid = rec.get("match_id", "?")
            log.info(f"[JOB] settlement_job SKIPPED {mid} — result_source not configured")
            n_skipped += 1
    else:
        for rec in unsettled:
            mid = rec.get("match_id", "?")
            try:
                result = result_source(
                    mid,
                    rec.get("player_a", ""),
                    rec.get("player_b", ""),
                    rec.get("date",     ""),
                )

                if result is None:
                    log.info(f"[JOB] settlement_job SKIPPED {mid} — result not yet available")
                    n_skipped += 1
                    continue

                if result.get("void"):
                    void_match(mid, notes="auto-settled: void")
                    log.info(f"[JOB] settlement_job VOID {mid}")
                    n_voided += 1
                    _notify_settlement(mid, "VOID", None)
                    continue

                winner = result.get("winner")
                if not winner:
                    log.warning(f"[JOB] settlement_job SKIPPED {mid} — result_source returned no winner")
                    n_skipped += 1
                    continue

                out     = settle(mid, winner)
                outcome = out.get("result", "?")
                pnl     = out.get("pnl_units")
                pnl_str = f"  P&L={pnl:+.3f}" if pnl is not None else ""
                log.info(f"[JOB] settlement_job SETTLED {mid} → {outcome}{pnl_str}")
                n_settled += 1
                _notify_settlement(mid, outcome, pnl)

            except Exception:
                log.exception(f"[JOB] settlement_job ERROR processing {mid}")
                n_errors += 1

    log.info(
        f"[JOB] settlement_job done — "
        f"checked={n_checked} settled={n_settled} voided={n_voided} "
        f"skipped={n_skipped} errors={n_errors}"
    )
