"""
tennis_model/models.py
======================
Central DataClass definitions: PlayerProfile and MatchPick.

All modules import from here.  pipeline.py no longer defines these classes,
eliminating the circular-import workarounds (from __future__ import annotations)
that existed in formatter.py and telegram.py.

Design principle: this file is a superset of the original pipeline.py definitions
and the new redesigned schema.  All existing field names are preserved for backward
compatibility; new fields from the redesigned schema are added as Optional extras.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# SERVE STATS BOUNDS
# Replaces the triplicated _BOUNDS / _BOUNDS_WTA / _SS_BOUNDS in pipeline.py.
# One canonical reference for valid serve-stat ranges.
# ──────────────────────────────────────────────────────────────────────────────

SERVE_BOUNDS: Dict[str, Tuple[float, float]] = {
    # ── New canonical keys (used by evaluator, serve_utils) ─────────────────
    "ace_rate":        (0.00, 0.30),
    "df_rate":         (0.00, 0.15),
    "first_in_pct":    (0.40, 0.80),
    "first_won_pct":   (0.45, 0.90),
    "second_won_pct":  (0.30, 0.75),
    "hold_pct":        (0.40, 0.98),
    "break_pct":       (0.02, 0.60),
    "return_win_pct":  (0.15, 0.55),
    "break_saved_pct": (0.20, 0.90),
    # ── Legacy keys used by Tennis Abstract ATP/WTA scrapers ─────────────────
    # serve_win_pct lower bound is 0.35 (not 0.45) to cover weak WTA servers
    "serve_win_pct":   (0.35, 0.85),
    "first_serve_in":  (0.30, 0.80),
    "first_serve_won": (0.40, 0.85),
    "second_serve_won":(0.30, 0.75),
}


# ──────────────────────────────────────────────────────────────────────────────
# PLAYER PROFILE
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PlayerProfile:
    # ── Required ────────────────────────────────────────────────────────────
    short_name: str               # display name used throughout the model

    # ── Identity metadata ───────────────────────────────────────────────────
    full_name:        str            = ""       # full name (ATP/WTA API)
    name:             str            = ""       # alias: set to full_name when populated
    atp_id:           str            = ""       # ATP 4-char ID
    slug:             str            = ""       # URL slug (Tennis Abstract, ATP)
    tour:             str            = ""       # "ATP" or "WTA"
    country:          Optional[str]  = None

    # ── Demographics ────────────────────────────────────────────────────────
    ranking:          int            = 9999
    age:              Optional[int]  = None     # None = fetch failed / unknown
    height_cm:        Optional[int]  = None
    plays:            str            = "Right-handed"  # handedness (legacy name)
    hand:             Optional[str]  = None            # alias for plays (new name)
    turned_pro:       Optional[int]  = None
    career_high_rank: Optional[int]  = None

    # ── Career record ───────────────────────────────────────────────────────
    career_wins:      Optional[int]  = None     # None = not populated; 0 = confirmed zero
    career_losses:    Optional[int]  = None

    # ── YTD record (raw counts — used by model + confidence) ────────────────
    ytd_wins:         Optional[int]  = None     # None = not fetched; 0 = confirmed zero
    ytd_losses:       Optional[int]  = None
    ytd_win_pct:      Optional[float] = None    # pre-computed convenience field

    # ── Surface records (raw counts — used by model.py, confidence.py) ──────
    hard_wins:        int            = 0
    hard_losses:      int            = 0
    clay_wins:        int            = 0
    clay_losses:      int            = 0
    grass_wins:       int            = 0
    grass_losses:     int            = 0
    surface_win_pct:  Optional[float] = None    # pre-computed for active surface

    # ── Form (list of "W"/"L" strings — consumed by model.py) ───────────────
    recent_form:      list           = field(default_factory=list)

    # ── ELO (populated by elo.py) ────────────────────────────────────────────
    elo:              Optional[float] = None
    elo_surface:      Optional[float] = None

    # ── H2H convenience (new — mirrors fields moved off MatchPick) ──────────
    h2h_wins:         Optional[int]  = None
    h2h_losses:       Optional[int]  = None

    # ── Data quality ─────────────────────────────────────────────────────────
    data_source:      str            = "unknown"
    # P0: separate identity resolution from data quality
    # identity_source: how the player was identified (never changes after resolution)
    #   "map" | "wta_profiles" | "atp_search" | "unresolved"
    identity_source:  str            = "unresolved"
    # profile_quality: quality of the stats data (set after fetch)
    #   "full" | "degraded" | "unknown"
    profile_quality:  str            = "unknown"

    # ── Stats ────────────────────────────────────────────────────────────────
    serve_stats:      dict           = field(default_factory=dict)

    # ── Catch-all for raw scraped data ───────────────────────────────────────
    raw_data:         dict           = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# MATCH PICK
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchPick:
    # ── Required ────────────────────────────────────────────────────────────
    player_a:             PlayerProfile
    player_b:             PlayerProfile

    # ── Match context ───────────────────────────────────────────────────────
    surface:              str            = "Hard"
    tournament:           str            = "ATP Tour"
    tournament_level:     str            = "ATP 250"
    tour:                 str            = "ATP"   # "ATP" or "WTA"
    round_name:           str            = ""      # e.g. "R32", "QF", "F"
    best_of:              int            = 3

    # ── Probabilities ───────────────────────────────────────────────────────
    prob_a:               float          = 0.50
    prob_b:               float          = 0.50
    fair_odds_a:          float          = 2.00
    fair_odds_b:          float          = 2.00
    market_odds_a:        Optional[float] = None
    market_odds_b:        Optional[float] = None
    edge_a:               Optional[float] = None
    edge_b:               Optional[float] = None

    # ── Expected value (new) ────────────────────────────────────────────────
    ev_a:                 Optional[float] = None
    ev_b:                 Optional[float] = None

    # ── Pick ─────────────────────────────────────────────────────────────────
    pick_player:          str            = ""
    bookmaker:            str            = ""
    stake_units:          Optional[float] = None   # Kelly-sized stake (set at alert time)

    # ── H2H ──────────────────────────────────────────────────────────────────
    h2h_summary:          str            = "No prior meetings"

    # ── Model details ────────────────────────────────────────────────────────
    factor_breakdown:     dict           = field(default_factory=dict)
    simulation:           dict           = field(default_factory=dict)

    # ── Assessment ──────────────────────────────────────────────────────────
    confidence:           str            = "LOW"
    validation_passed:    bool           = True
    filter_reason:        str            = ""
    validation_warnings:  list           = field(default_factory=list)

    # ── Odds metadata ────────────────────────────────────────────────────────
    odds_source:          str            = "manual"   # "live" or "manual"

    # ── Evaluator second-pass ────────────────────────────────────────────────
    evaluator_result:     dict           = field(default_factory=dict)

    # ── Quality tier (operational output layer only) ─────────────────────────
    quality_tier:         str            = ""    # "CLEAN" | "CAUTION" | "FRAGILE"

    # ── Notes and debug ──────────────────────────────────────────────────────
    notes:                list           = field(default_factory=list)
    debug:                dict           = field(default_factory=dict)

    # ─────────────────────────────────────────────────────────────────────────
    # METHODS
    # ─────────────────────────────────────────────────────────────────────────

    def picked_side(self) -> Optional[Dict[str, Any]]:
        """Return a dict describing the picked side, or None if no pick set.

        Keys: side ("A"|"B"), player, opponent, prob, market_odds,
              fair_odds, edge, ev.
        Raises ValueError if pick_player is set but matches neither player.
        """
        if not self.pick_player:
            return None
        if self.pick_player == self.player_a.short_name:
            return {
                "side":        "A",
                "player":      self.player_a,
                "opponent":    self.player_b,
                "prob":        self.prob_a,
                "market_odds": self.market_odds_a,
                "fair_odds":   self.fair_odds_a,
                "edge":        self.edge_a,
                "ev":          self.ev_a,
            }
        if self.pick_player == self.player_b.short_name:
            return {
                "side":        "B",
                "player":      self.player_b,
                "opponent":    self.player_a,
                "prob":        self.prob_b,
                "market_odds": self.market_odds_b,
                "fair_odds":   self.fair_odds_b,
                "edge":        self.edge_b,
                "ev":          self.ev_b,
            }
        raise ValueError(
            f"pick_player={self.pick_player!r} matches neither "
            f"player_a={self.player_a.short_name!r} nor "
            f"player_b={self.player_b.short_name!r}"
        )

    def require_picked_side(self) -> Dict[str, Any]:
        """Like picked_side() but raises ValueError if no pick is set."""
        side = self.picked_side()
        if side is None:
            raise ValueError(
                f"No pick set on this MatchPick "
                f"({self.player_a.short_name} vs {self.player_b.short_name})"
            )
        return side
