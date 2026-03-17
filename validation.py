from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    passed: bool = True
    warnings: list = field(default_factory=list)
    errors:   list = field(default_factory=list)
    confidence_penalty: float = 0.0


def validate_match(pa, pb, surface,
                   market_odds_a=None, market_odds_b=None):
    v = ValidationResult()
    surf = surface.lower()

    # 1. Active this season
    for p in [pa, pb]:
        if p.ytd_wins + p.ytd_losses == 0:
            v.errors.append(
                f"{p.short_name}: 0 matches this season — may be inactive")

    # 2. Surface sample size
    for p in [pa, pb]:
        n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        if n < 10:
            v.warnings.append(
                f"{p.short_name}: thin {surface} sample ({n} matches)")
            v.confidence_penalty += 0.15
        elif n < 20:
            v.warnings.append(
                f"{p.short_name}: moderate {surface} sample ({n} matches)")
            v.confidence_penalty += 0.05

    # 3. Data source quality
    bad_sources = ("unknown", "fallback")
    for p in [pa, pb]:
        if p.data_source in bad_sources:
            v.errors.append(
                f"{p.short_name}: unreliable data source ({p.data_source})")

    # 4. Odds sanity check
    if market_odds_a and market_odds_b:
        implied = 1/market_odds_a + 1/market_odds_b
        if implied < 0.85 or implied > 1.40:
            v.errors.append(
                f"Odds sanity failed: implied={implied:.3f} "
                f"(expected 0.85-1.40)")

    # 5. Ranking gap + thin stats
    if pa.ranking < 9999 and pb.ranking < 9999:
        if abs(pa.ranking - pb.ranking) > 200:
            n_a = getattr(pa, f"{surf}_wins", 0) + getattr(pa, f"{surf}_losses", 0)
            n_b = getattr(pb, f"{surf}_wins", 0) + getattr(pb, f"{surf}_losses", 0)
            if min(n_a, n_b) < 5:
                v.warnings.append(
                    "Ranking gap > 200 with very thin surface stats")
                v.confidence_penalty += 0.20

    v.passed = len(v.errors) == 0
    return v
