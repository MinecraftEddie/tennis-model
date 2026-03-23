"""
orchestration/audit.py
=======================
DailyAudit — operational run summary collected during scan_today().

Instantiated at the start of each scan_today() run and updated as matches
are processed.  Logged in full at the end of the run via log_summary().

Fields
------
date               — ISO date of the run (auto-filled)
profiles_full      — players whose profile is "full" (live data)
profiles_degraded  — players whose profile is "degraded" (cache / error)
profiles_failed    — players from skipped matches (exception / unresolved)
no_pick_reasons    — {reason_str: count} breakdown of why picks were blocked
picks_generated    — matches where a pick_player was assigned
alerts_eligible    — EV passed (sent + qualified_only)
alerts_sent        — actually passed to Telegram (not FRAGILE, evaluator OK)
alerts_suppressed  — FRAGILE tier — suppressed, not sent
alerts_failed      — Telegram send errors (updated externally if needed)
watchlist_count    — evaluator flagged as watchlist
telegram_configured — True if Telegram credentials are set
telegram_dry_run   — True if running without sending alerts
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

# Audit JSON directory: <repo_root>/data/audits/
_HERE       = os.path.dirname(os.path.abspath(__file__))  # orchestration/
_PKG_DIR    = os.path.dirname(_HERE)                       # tennis_model/
_REPO_ROOT  = os.path.dirname(_PKG_DIR)                   # <repo_root>/
_AUDITS_DIR = os.path.join(_REPO_ROOT, "data", "audits")


@dataclass
class DailyAudit:
    date: str = field(default_factory=lambda: date.today().isoformat())

    # ── Scan scope ────────────────────────────────────────────────────────────
    matches_scanned: int = 0  # total match attempts (resolved + skipped)

    # ── Profile pipeline (per-player, so up to 2× number of matches) ─────────
    profiles_full:     int = 0
    profiles_degraded: int = 0
    profiles_failed:   int = 0   # exception / unresolved identity

    # ── No-pick breakdown: reason_str → count ────────────────────────────────
    no_pick_reasons: dict = field(default_factory=dict)

    # ── Picks and alerts ──────────────────────────────────────────────────────
    picks_generated:   int = 0   # matches with a non-empty pick_player
    alerts_eligible:   int = 0   # EV passed (sent + qualified_only)
    alerts_sent:       int = 0   # dispatched to Telegram (evaluator approved)
    alerts_suppressed: int = 0   # FRAGILE — suppressed, not sent
    alerts_failed:     int = 0   # Telegram delivery errors

    # ── P3: per-AlertStatus breakdown ─────────────────────────────────────────
    alerts_dry_run:         int = 0   # DRY_RUN (Telegram not configured)
    alerts_skipped_unknown: int = 0   # SKIPPED_UNKNOWN (UNKNOWN profile)
    alerts_skipped_risk:    int = 0   # SKIPPED_RISK (risk cap)
    alerts_skipped_kelly:   int = 0   # SKIPPED_KELLY (Kelly <= 0)
    stake_reduced_count:    int = 0   # alerts where stake_factor < 1.0
    alert_status_breakdown: dict = field(default_factory=dict)  # {status: count}

    # ── P4: evaluator decision breakdown ──────────────────────────────────────
    no_pick_count:          int = 0   # EV filter blocked
    validation_block_count: int = 0   # EV blocked + validation failed
    model_block_count:      int = 0   # EV passed, evaluator: ignore
    evaluator_watchlist_count: int = 0  # EV passed, evaluator: watchlist
    evaluator_status_breakdown: dict = field(default_factory=dict)  # {status: count}

    # ── Evaluator (legacy — unified with evaluator_watchlist_count in P5) ─────
    watchlist_count: int = 0

    # ── P5: normalised MatchFinalStatus breakdown ──────────────────────────────
    final_status_breakdown: dict = field(default_factory=dict)  # {status: count}

    # ── P6: reason code breakdown ──────────────────────────────────────────────
    reason_code_breakdown: dict = field(default_factory=dict)   # {reason_code: count}

    # ── P6: risk_decision gating counter ──────────────────────────────────────
    risk_decision_blocked_count: int = 0  # PICK path where risk engine blocked alert

    # ── Telegram state ────────────────────────────────────────────────────────
    telegram_configured: bool = False
    telegram_dry_run:    bool = False

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def record_profile(self, quality: str) -> None:
        """Increment the appropriate profile quality counter."""
        if quality == "full":
            self.profiles_full += 1
        elif quality == "degraded":
            self.profiles_degraded += 1
        else:
            self.profiles_failed += 1

    def record_no_pick(self, reason: str) -> None:
        """Increment the no-pick reason counter for *reason*."""
        if reason:
            self.no_pick_reasons[reason] = self.no_pick_reasons.get(reason, 0) + 1

    def record_alert_decision(self, decision) -> None:
        """
        Record an AlertDecision into the P3 audit counters.

        Called from pipeline.run_match(_audit=audit) during scan_today().
        Only increments P3-specific counters — the legacy counters
        (alerts_sent, alerts_failed, alerts_suppressed) are still managed
        by populate_from_scan_results() for backward compatibility.
        """
        from tennis_model.orchestration.alert_status import AlertStatus
        status = decision.status
        # Per-status breakdown (all statuses) — use .value so key is "SENT" not "AlertStatus.SENT"
        key = status.value
        self.alert_status_breakdown[key] = self.alert_status_breakdown.get(key, 0) + 1
        # P3-specific counters only
        if status == AlertStatus.DRY_RUN:
            self.alerts_dry_run += 1
        elif status == AlertStatus.SKIPPED_UNKNOWN:
            self.alerts_skipped_unknown += 1
        elif status == AlertStatus.SKIPPED_RISK:
            self.alerts_skipped_risk += 1
        elif status == AlertStatus.SKIPPED_KELLY:
            self.alerts_skipped_kelly += 1
        # Stake reduction: track when factor < 1.0 and stake was actually applied
        if (decision.stake_factor is not None
                and 0.0 < decision.stake_factor < 1.0
                and decision.stake_units is not None
                and decision.stake_units > 0.0):
            self.stake_reduced_count += 1

    def record_evaluator_decision(self, decision) -> None:
        """
        Record an EvaluatorDecision into the P4 audit counters.

        Called from pipeline.run_match(_audit=audit) for every match outcome.
        Tracks evaluator status separately from alert status (record_alert_decision).
        """
        from tennis_model.evaluator.evaluator_decision import EvaluatorStatus
        status = decision.status
        # Per-status breakdown
        key = status.value
        self.evaluator_status_breakdown[key] = (
            self.evaluator_status_breakdown.get(key, 0) + 1
        )
        # Named counters
        if status == EvaluatorStatus.NO_PICK:
            self.no_pick_count += 1
        elif status == EvaluatorStatus.BLOCKED_VALIDATION:
            self.validation_block_count += 1
        elif status == EvaluatorStatus.BLOCKED_MODEL:
            self.model_block_count += 1
        elif status == EvaluatorStatus.WATCHLIST:
            self.evaluator_watchlist_count += 1
            # Note: legacy watchlist_count is still driven by populate_from_scan_results()

    def record_match_result(self, result) -> None:
        """
        P5/P6: Record a MatchRunResult into all audit counters.

        Single entry point that replaces the previous two-call pattern of
        record_evaluator_decision() + record_alert_decision().  Also populates
        final_status_breakdown (P5), profile counters, pick/alert counters,
        and reason_code_breakdown (P6 additions).

        Called from orchestration/match_runner.run_match_core() when _audit is provided.
        """
        from tennis_model.orchestration.match_runner import MatchFinalStatus, ALERT_SENT_STATUSES

        # Delegate to existing per-decision recorders (unchanged logic)
        self.record_evaluator_decision(result.evaluator_decision)
        if result.alert_decision is not None:
            self.record_alert_decision(result.alert_decision)

        # P5: final_status breakdown — one entry per match
        key = result.final_status.value
        self.final_status_breakdown[key] = (
            self.final_status_breakdown.get(key, 0) + 1
        )

        # Unify watchlist_count: single source driven by MatchFinalStatus
        if result.final_status == MatchFinalStatus.WATCHLIST:
            self.watchlist_count += 1

        # P6: Profile quality — 2 players per match
        self.record_profile(result.profile_quality_a)
        self.record_profile(result.profile_quality_b)

        # P6: No-pick reason (when filter_reason is set)
        if result.filter_reason:
            self.record_no_pick(result.filter_reason)

        # P6: Incremental pick/alert counters
        if result.pick and getattr(result.pick, "pick_player", ""):
            self.picks_generated += 1
        if result.final_status in ALERT_SENT_STATUSES:
            self.alerts_eligible += 1
            if result.final_status == MatchFinalStatus.PICK_ALERT_SENT:
                self.alerts_sent += 1
            elif result.final_status == MatchFinalStatus.PICK_SUPPRESSED:
                self.alerts_suppressed += 1

        # P6: reason_code_breakdown
        for rc in result.reason_codes:
            if rc:
                self.reason_code_breakdown[rc] = (
                    self.reason_code_breakdown.get(rc, 0) + 1
                )

        # P6: risk_decision blocked counter — PICK path where risk engine said no
        if (result.risk_decision is not None
                and not getattr(result.risk_decision, "allowed", True)):
            self.risk_decision_blocked_count += 1

    def populate_from_scan_results(
        self,
        picks: list,     # list of MatchPick objects from successful run_match() calls
        alerts: list,    # scan_today alerts list (dicts)
        blocked: list,   # scan_today blocked list (dicts)
        skipped: list,   # scan_today skipped list (dicts)
    ) -> None:
        """
        P6: Populate counters from scan_today() data structures.

        When record_match_result() was called for all matches (final_status_breakdown
        non-empty), only profiles_failed is incremented here (for exception-skipped
        matches that never reached run_match_core).  All other counters are already
        populated incrementally by record_match_result().

        Legacy fallback: if final_status_breakdown is empty (e.g. unit tests
        bypassing run_match_with_result), fall back to bulk counting logic.
        """
        # Always: skipped matches (exception mid-pipeline) → 2 lost profiles each
        self.profiles_failed += len(skipped) * 2

        if self.final_status_breakdown:
            # P6: all counters populated incrementally — nothing more to do
            return

        # Legacy fallback for callers that bypass run_match_with_result()
        for pick in picks:
            for player in (pick.player_a, pick.player_b):
                self.record_profile(getattr(player, "profile_quality", "unknown"))
        for b in blocked:
            self.record_no_pick(b.get("reason", "unknown"))
        sent_ok      = [a for a in alerts if not a.get("qualified_only")
                        and a.get("quality_tier") != "FRAGILE"]
        sent_fragile = [a for a in alerts if a.get("quality_tier") == "FRAGILE"]
        qualified    = [a for a in alerts if a.get("qualified_only")]
        self.picks_generated   = sum(1 for a in alerts if a.get("pick"))
        self.alerts_eligible   = len(sent_ok) + len(qualified)
        self.alerts_sent       = len(sent_ok)
        self.alerts_suppressed = len(sent_fragile)
        if self.watchlist_count == 0:
            for a in alerts:
                if a.get("rec_action", "").lower() == "watchlist":
                    self.watchlist_count += 1

    def save_audit_json(self, audits_dir: Optional[str] = None) -> None:
        """
        Write audit data to audits_dir/<date>.json.

        Silent on disk failures — never blocks the pipeline.
        Default directory: <repo_root>/data/audits/
        """
        target_dir = audits_dir if audits_dir is not None else _AUDITS_DIR
        try:
            os.makedirs(target_dir, exist_ok=True)
            path = os.path.join(target_dir, f"{self.date}.json")
            payload = {
                "date":                self.date,
                "matches_scanned":     self.matches_scanned,
                "profiles_full":       self.profiles_full,
                "profiles_degraded":   self.profiles_degraded,
                "profiles_failed":     self.profiles_failed,
                "no_pick_reasons":     self.no_pick_reasons,
                "picks_generated":     self.picks_generated,
                "alerts_eligible":     self.alerts_eligible,
                "alerts_sent":         self.alerts_sent,
                "alerts_suppressed":   self.alerts_suppressed,
                "alerts_failed":       self.alerts_failed,
                "alerts_dry_run":         self.alerts_dry_run,
                "alerts_skipped_unknown": self.alerts_skipped_unknown,
                "alerts_skipped_risk":    self.alerts_skipped_risk,
                "alerts_skipped_kelly":   self.alerts_skipped_kelly,
                "stake_reduced_count":    self.stake_reduced_count,
                "alert_status_breakdown": self.alert_status_breakdown,
                "no_pick_count":          self.no_pick_count,
                "validation_block_count": self.validation_block_count,
                "model_block_count":      self.model_block_count,
                "evaluator_watchlist_count": self.evaluator_watchlist_count,
                "evaluator_status_breakdown": self.evaluator_status_breakdown,
                "watchlist_count":     self.watchlist_count,
                "final_status_breakdown":      self.final_status_breakdown,
                "reason_code_breakdown":       self.reason_code_breakdown,
                "risk_decision_blocked_count": self.risk_decision_blocked_count,
                "telegram_configured": self.telegram_configured,
                "telegram_dry_run":    self.telegram_dry_run,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            log.info(f"[AUDIT] JSON saved: {path}")
        except Exception as exc:
            log.warning(f"[AUDIT] JSON write failed (non-blocking): {exc}")

    def log_summary(self) -> None:
        """Emit the full audit summary to the logger at INFO level."""
        reasons_str = (
            "\n".join(f"      {r}: {n}" for r, n in self.no_pick_reasons.items())
            if self.no_pick_reasons
            else "      (none)"
        )
        tg_status = (
            "dry_run (not configured)"
            if not self.telegram_configured
            else ("dry_run (flag set)" if self.telegram_dry_run else "configured — alerts sent")
        )
        log.info(
            f"\n{'─'*56}\n"
            f"[AUDIT] Daily run — {self.date}\n"
            f"{'─'*56}\n"
            f"  Profiles :\n"
            f"    full      : {self.profiles_full}\n"
            f"    degraded  : {self.profiles_degraded}\n"
            f"    failed    : {self.profiles_failed}\n"
            f"  Picks generated  : {self.picks_generated}\n"
            f"  Alerts eligible  : {self.alerts_eligible}\n"
            f"  Alerts sent      : {self.alerts_sent}\n"
            f"  Alerts suppressed: {self.alerts_suppressed}  (FRAGILE)\n"
            f"  Alerts failed    : {self.alerts_failed}\n"
            f"  Alerts dry-run   : {self.alerts_dry_run}\n"
            f"  Alerts unk.skipped:{self.alerts_skipped_unknown}\n"
            f"  Stake reduced    : {self.stake_reduced_count}\n"
            f"  No-pick          : {self.no_pick_count}\n"
            f"  Validation block : {self.validation_block_count}\n"
            f"  Model block      : {self.model_block_count}\n"
            f"  Eval watchlist   : {self.evaluator_watchlist_count}\n"
            f"  Watchlist        : {self.watchlist_count}\n"
            f"  Telegram         : {tg_status}\n"
            f"  No-pick reasons  :\n{reasons_str}\n"
            + (
                f"  Final statuses   : {self.final_status_breakdown}\n"
                if self.final_status_breakdown else ""
            )
            + (
                f"  Reason codes     : {self.reason_code_breakdown}\n"
                if self.reason_code_breakdown else ""
            )
            + (
                f"  Risk blocked     : {self.risk_decision_blocked_count}\n"
                if self.risk_decision_blocked_count else ""
            )
            + f"{'─'*56}"
        )
