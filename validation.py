from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ValidationResult:
    passed: bool = True
    warnings: list = field(default_factory=list)
    errors:   list = field(default_factory=list)
    confidence_penalty: float = 0.0


def validate_match(pa, pb, surface,
                   market_odds_a=None, market_odds_b=None,
                   odds_source: str = "manual", odds_timestamp: str = ""):
    v = ValidationResult()
    surf = surface.lower()

    # 1. Active this season
    # Only flag as inactive when ytd data was actually fetched and is confirmed 0.
    # ytd_wins/losses = None means the data source didn't provide it (e.g. ATP API
    # failure) — that is not the same as genuinely playing 0 matches this season.
    for p in [pa, pb]:
        if p.ytd_wins is not None and p.ytd_wins + p.ytd_losses == 0:
            v.errors.append(
                f"{p.short_name}: 0 matches this season — may be inactive")

    # 2. Surface sample size
    # Cap at 1500: values above that are parsing artifacts, not real sample depth.
    for p in [pa, pb]:
        raw_n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        n = raw_n if raw_n <= 1500 else 0
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
            n_a = n_a if n_a <= 1500 else 0  # guard against parsing artifacts
            n_b = n_b if n_b <= 1500 else 0
            if min(n_a, n_b) < 5:
                v.warnings.append(
                    "Ranking gap > 200 with very thin surface stats")
                v.confidence_penalty += 0.20

    # 6. Odds staleness (manual odds only — live odds have their own timestamp)
    if odds_source == "manual" and odds_timestamp:
        try:
            ts = datetime.fromisoformat(odds_timestamp.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_h > 24:
                v.errors.append(
                    f"STALE_ODDS: manual odds are {age_h:.1f}h old (limit 24h)")
            elif age_h > 6:
                v.warnings.append(
                    f"Manual odds are {age_h:.1f}h old — consider refreshing")
        except ValueError:
            pass

    v.passed = len(v.errors) == 0
    return v
