# Tennis Model – Project Skills / Context

Status: refactor completed up to P6
Last checkpoint commit: P0→P6 full pipeline refactor

This file describes the architecture and rules of the tennis betting model,
so the project can be resumed later without rediscovering the pipeline.


------------------------------------------------------------
ARCHITECTURE OVERVIEW
------------------------------------------------------------

Full decision chain:

ProfileQualityResult
→ EvaluatorDecision
→ RiskDecision
→ AlertDecision
→ MatchRunResult
→ MatchFinalStatus

Main idea:
Each stage has a single responsibility.
No stage should infer state from strings when a typed result exists.


------------------------------------------------------------
MAIN MODULES
------------------------------------------------------------

pipeline.py
- High level orchestrator
- Thin wrapper after P6
- Delegates match execution to orchestration.match_runner

orchestration/match_runner.py
- Canonical run_match_with_result()
- Builds MatchRunResult
- Calls evaluator, risk engine, alerting

quality/
- profile_quality.py
- reason_codes.py
- QUALITY_RULES is the authority for degraded/full/unknown

evaluator/
- evaluator_decision.py
- risk_engine.py

ingestion/
- identity.py
- profile_fetcher.py
- profile_cache.py
- http_utils.py

orchestration/
- audit.py
- match_runner.py
- alert_status.py

telegram.py
- maybe_alert()
- builds AlertDecision

tests/
- full coverage for P0→P6


------------------------------------------------------------
PROFILE QUALITY RULES
------------------------------------------------------------

FULL
- identity resolved
- stats fresh
- allow_pick = True
- stake_factor = 1.0

DEGRADED
- identity resolved but data partial / cache / rate limit
- allow_pick = True
- confidence penalty
- stake_factor = 0.5

UNKNOWN
- identity unresolved
- allow_pick = False
- no alert
- no stake


------------------------------------------------------------
DECISION FLOW RULES
------------------------------------------------------------

EvaluatorDecision
- PICK
- WATCHLIST
- NO_PICK
- BLOCKED_VALIDATION
- BLOCKED_MODEL

RiskDecision
- allowed / not allowed
- stake adjusted using QUALITY_RULES
- Kelly <= 0 → block
- UNKNOWN → block

AlertDecision
- SENT
- DRY_RUN
- SUPPRESSED
- SKIPPED_UNKNOWN
- SKIPPED_RISK
- FAILED

MatchFinalStatus
- PICK_ALERT_SENT
- PICK_DRY_RUN
- PICK_SUPPRESSED
- WATCHLIST
- NO_PICK
- BLOCKED_VALIDATION
- BLOCKED_MODEL
- FAILED


------------------------------------------------------------
AUDIT RULES
------------------------------------------------------------

record_match_result() is source of truth

Audit tracks:
- matches_scanned
- evaluator status breakdown
- final_status_breakdown
- alert_status_breakdown
- risk_decision_blocked_count
- stake_reduced_count

populate_from_scan_results()
- legacy
- kept for compatibility
- should not be primary source


------------------------------------------------------------
P6 STATE
------------------------------------------------------------

Completed:
- run_match_with_result moved to match_runner
- MatchRunResult unified
- final_status normalized
- audit unified
- risk_decision exposed
- pipeline thin wrapper

Remaining (optional future work):
- move fetch_h2h / _days_inactive out of pipeline
- clean scan_today routing
- improve alert quality
- improve EV calibration
- improve model features
- track real outcomes / ROI


------------------------------------------------------------
WHEN RESUMING PROJECT
------------------------------------------------------------

Start from:
- match_runner
- evaluator
- risk_engine
- alert logic
- audit output

Do NOT modify pipeline structure unless necessary.
Focus on model quality, not plumbing.
