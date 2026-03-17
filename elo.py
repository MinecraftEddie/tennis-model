from dataclasses import dataclass, field
from datetime import datetime, date
import json, os, math

ELO_FILE = "data/elo_ratings.json"

# K-factors by tournament level
K_FACTORS = {
    "grand_slam":   40,
    "wta_1000":     32,
    "atp_1000":     32,
    "wta_500":      24,
    "atp_500":      24,
    "wta_250":      20,
    "atp_250":      20,
    "challenger":   16,
    "qualifying":    8,
    "itf":           8,
}


def canonical_id(name: str) -> str:
    """Canonical ELO player ID: strip, lowercase, spaces→underscore, dots removed.
    Used in both elo_win_probability() and record_result() so IDs always match."""
    return name.strip().lower().replace(" ", "_").replace(".", "")


def ranking_to_elo(ranking: int) -> float:
    if ranking <= 0 or ranking >= 9999: return 1500.0
    if ranking <= 5:    return 2300.0
    if ranking <= 10:   return 2200.0
    if ranking <= 20:   return 2100.0
    if ranking <= 30:   return 2000.0
    if ranking <= 50:   return 1900.0
    if ranking <= 75:   return 1800.0
    if ranking <= 100:  return 1750.0
    if ranking <= 150:  return 1700.0
    if ranking <= 200:  return 1650.0
    if ranking <= 500:  return 1600.0
    return 1500.0


@dataclass
class PlayerELO:
    player_id:      str
    overall:        float = 1500.0
    hard:           float = 1500.0
    clay:           float = 1500.0
    grass:          float = 1500.0
    recent:         float = 1500.0   # last 90 days
    last_updated:   str   = ""
    matches_played: int   = 0


class TennisELO:
    def __init__(self):
        self.ratings: dict[str, PlayerELO] = {}
        self._load()

    def _load(self):
        if os.path.exists(ELO_FILE):
            try:
                with open(ELO_FILE) as f:
                    data = json.load(f)
                for pid, vals in data.items():
                    self.ratings[pid] = PlayerELO(**vals)
            except Exception:
                pass

    def _save(self):
        os.makedirs("data", exist_ok=True)
        with open(ELO_FILE, "w") as f:
            data = {k: vars(v) for k, v in self.ratings.items()}
            json.dump(data, f, indent=2)

    def get_or_init(self, player_id: str, ranking: int = 9999) -> PlayerELO:
        if player_id not in self.ratings:
            base = ranking_to_elo(ranking)
            self.ratings[player_id] = PlayerELO(
                player_id=player_id,
                overall=base, hard=base, clay=base,
                grass=base, recent=base,
                last_updated=date.today().isoformat()
            )
        return self.ratings[player_id]

    def win_probability(self, elo_a: float, elo_b: float) -> float:
        """P(A beats B) using standard ELO formula."""
        return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400))

    def update(self, winner_id: str, loser_id: str,
               surface: str, tournament_level: str,
               winner_ranking: int = 9999,
               loser_ranking: int = 9999):
        """Update ELO ratings after a match result."""

        w = self.get_or_init(winner_id, winner_ranking)
        l = self.get_or_init(loser_id,  loser_ranking)

        k = K_FACTORS.get(tournament_level.lower().replace(" ", "_"), 20)

        surf = surface.lower()
        w_surf_elo = getattr(w, surf, w.overall)
        l_surf_elo = getattr(l, surf, l.overall)

        # Expected scores
        e_w_overall = self.win_probability(w.overall,  l.overall)
        e_w_surface = self.win_probability(w_surf_elo, l_surf_elo)
        e_w_recent  = self.win_probability(w.recent,   l.recent)

        # Update overall
        w.overall = round(w.overall + k * (1 - e_w_overall), 2)
        l.overall = round(l.overall + k * (0 - (1 - e_w_overall)), 2)

        # Update surface-specific
        setattr(w, surf, round(w_surf_elo + k * (1 - e_w_surface), 2))
        setattr(l, surf, round(l_surf_elo + k * (0 - (1 - e_w_surface)), 2))

        # Update recent (higher K for recent — more volatile)
        k_recent = min(k * 1.5, 48)
        w.recent = round(w.recent + k_recent * (1 - e_w_recent), 2)
        l.recent = round(l.recent + k_recent * (0 - (1 - e_w_recent)), 2)

        # Track matches
        w.matches_played += 1
        l.matches_played += 1
        today = date.today().isoformat()
        w.last_updated = today
        l.last_updated = today

        self._save()
        return w, l

    def get_final_rating(self, player_id: str, surface: str,
                         ranking: int = 9999) -> float:
        """
        Final blended ELO used by the model:
        0.50 * surface_elo + 0.30 * overall_elo + 0.20 * recent_elo
        """
        p = self.get_or_init(player_id, ranking)
        surf_elo = getattr(p, surface.lower(), p.overall)
        return round(
            0.50 * surf_elo +
            0.30 * p.overall +
            0.20 * p.recent,
            2
        )

    def elo_win_probability(self, id_a: str, id_b: str,
                            surface: str,
                            ranking_a: int = 9999,
                            ranking_b: int = 9999) -> tuple[float, float]:
        """
        Return (prob_a, prob_b) using final blended ELO ratings.
        This replaces _ranking_score() in model.py.
        """
        elo_a = self.get_final_rating(id_a, surface, ranking_a)
        elo_b = self.get_final_rating(id_b, surface, ranking_b)
        prob_a = self.win_probability(elo_a, elo_b)
        return round(prob_a, 4), round(1 - prob_a, 4)


# Global singleton — loaded once, reused across the session
_elo_engine = None

def get_elo_engine() -> TennisELO:
    global _elo_engine
    if _elo_engine is None:
        _elo_engine = TennisELO()
    return _elo_engine
