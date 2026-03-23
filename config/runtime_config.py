"""
tennis_model/config/runtime_config.py
======================================
Single source of truth for all tunable runtime constants.

Pure constants (no env override) are plain literals.
Operationally configurable values read their env var once at import time;
restart the process to pick up env-var changes.

Import pattern
--------------
    from tennis_model.config.runtime_config import ELO_SHRINK, MC_WEIGHT, ...
"""
import os

# ── Model / probability ───────────────────────────────────────────────────────
# Versioning: bump MODEL_VERSION to reset the alert dedupe namespace so all
# previously-alerted picks can be re-evaluated on the next scheduler run.
MODEL_VERSION = os.getenv("MODEL_VERSION", "2.0")

ELO_SHRINK    = 0.80   # ELO prior anchor: 80% model + 20% ELO (applied before market/MC blend)
MARKET_WEIGHT = 0.15   # market blend: 85% model + 15% vig-stripped market
MC_WEIGHT     = 0.15   # Monte Carlo simulation weight (tapered when gap > 12%)

# ── EV filter (ev.py) ─────────────────────────────────────────────────────────
PROB_FLOOR               = 0.40   # minimum model probability to consider betting
SUSPICIOUS_EDGE_THRESHOLD = 0.50  # edges above 50% flagged for manual review
MIN_ODDS                 = 1.50   # hard floor on market odds
MAX_ODDS                 = 3.00   # soft ceiling on market odds (warn, not block)

# ── Evaluator guards (evaluator/evaluator.py) ────────────────────────────────
LONGSHOT_GUARD_THRESHOLD          = 0.15   # market_prob < 0.15 → watchlist only
UNDERDOG_EDGE_THRESHOLD_LOW_ODDS  = 0.15   # underdog @≤3.00 requires edge >= 15%
UNDERDOG_EDGE_THRESHOLD_HIGH_ODDS = 0.18   # underdog @>3.00  requires edge >= 18%

# ── Kelly / staking (alerts/kelly.py) ────────────────────────────────────────
KELLY_FRACTION  = 0.25                                        # 1/4 Kelly safety fraction
MIN_STAKE_UNITS = float(os.getenv("MIN_STAKE_UNITS",  "0.05"))  # floor stake in units
MAX_STAKE_UNITS = float(os.getenv("MAX_STAKE_UNITS",  "1.0"))   # cap stake in units

# ── Risk caps (alerts/risk_caps.py) ──────────────────────────────────────────
MAX_DAILY_EXPOSURE_UNITS = float(os.getenv("MAX_DAILY_EXPOSURE_UNITS", "3.0"))
MAX_DAILY_DRAWDOWN_UNITS = float(os.getenv("MAX_DAILY_DRAWDOWN_UNITS", "3.0"))

# ── Scheduler (orchestration/scheduler.py) ───────────────────────────────────
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))

# ── Bankroll (tracking/settle_predictions.py, reporting/trading_dashboard.py) ─
BANKROLL_START = float(os.getenv("BANKROLL_START", "1000.0"))
