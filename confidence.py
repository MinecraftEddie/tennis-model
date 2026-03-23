from tennis_model.validation import ValidationResult
from tennis_model.confidence_caps import cap_data_availability


def compute_confidence(pa, pb, surface: str,
                       validation: ValidationResult,
                       edge: float,
                       model_prob: float,
                       days_inactive: int = -1) -> str:

    score = 0.0
    surf = surface.lower()

    # --- Data availability (data_source + surface_n + ytd) — capped below ---
    _pre_data = score

    # --- Data source quality ---
    source_scores = {
        "static_curated":  0.30,
        "wta_static":      0.15,  # was 0.25; -0.10 staleness penalty (manually-maintained data)
        "atp_api":         0.15,
        "tennis_abstract": 0.10,
        "wta_estimated":  -0.25,   # estimated profile — heavy penalty
        "unknown":        -0.20,
        "fallback":       -0.20,
    }
    score += source_scores.get(pa.data_source, 0.0)
    score += source_scores.get(pb.data_source, 0.0)

    # --- Surface sample depth ---
    # Cap at 1500: any value above that is a parsing artifact (no player has
    # more than ~1200 wins on a single surface), and should be treated as 0.
    for p in [pa, pb]:
        raw_n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        n = raw_n if raw_n <= 1500 else 0
        if n >= 50:   score += 0.20
        elif n >= 30: score += 0.12
        elif n >= 15: score += 0.05
        elif n < 10:  score -= 0.10

    # --- Season activity ---
    # None means the data source structurally does not provide YTD (ATP matchmx).
    # Unknown YTD is not the same as confirmed inactive: skip scoring entirely.
    # Only apply the inactive penalty when data was fetched and is confirmed zero
    # (WTA jsfrags real 0-0 = genuinely inactive this season).
    for p in [pa, pb]:
        if p.ytd_wins is None and p.ytd_losses is None:
            continue  # unknown — no bonus, no penalty
        ytd = (p.ytd_wins or 0) + (p.ytd_losses or 0)
        if ytd >= 15:   score += 0.10
        elif ytd >= 8:  score += 0.05
        elif ytd == 0:  score -= 0.20

    # --- Cap data availability contribution ---
    _data_avail = score - _pre_data
    score = _pre_data + cap_data_availability(_data_avail)

    # --- Edge strength ---
    if edge >= 0.25:    score += 0.20
    elif edge >= 0.15:  score += 0.10
    elif edge >= 0.07:  score += 0.05
    elif edge < 0.07:   score -= 0.10

    # --- Model conviction ---
    gap = abs(model_prob - 0.5)
    if gap >= 0.20:     score += 0.15
    elif gap >= 0.12:   score += 0.08
    elif gap < 0.05:    score -= 0.10

    # --- Serve stats quality ---
    # Real serve stats (Tennis Abstract ATP matchmx or WTA jsfrags): no penalty.
    # Proxy (hard-court win% heuristic): -0.15 per player.
    _real_sources = ("tennis_abstract", "tennis_abstract_wta")
    for p in [pa, pb]:
        if p.serve_stats.get("source") not in _real_sources:
            score -= 0.15

    # --- WTA serve surface mismatch ---
    # jsfrags recent-results only cover recent matches — in early season these are
    # all on hard.  When surface is clay/grass and no surface-specific serve key
    # exists, extract_stats() silently falls back to hard-biased career averages.
    # That data still carries source="tennis_abstract_wta" so no penalty fires above.
    # Apply -0.08 per player to correct for the unacknowledged hard-court bias.
    if surf in ("clay", "grass"):
        for p in [pa, pb]:
            ss = p.serve_stats or {}
            if ss.get("source") == "tennis_abstract_wta":
                surf_n = ss.get(surf, {}).get("n", 0)
                if surf_n < 5:
                    score -= 0.08

    # --- WTA serve sample too small ---
    # At n < 8, average-of-averages bias is severe (each match carries 1/n weight
    # regardless of match length).  Apply a modest penalty per player.
    for p in [pa, pb]:
        ss = p.serve_stats or {}
        if ss.get("source") == "tennis_abstract_wta":
            n = ss.get("career", {}).get("n", 0)
            if 0 < n < 8:
                score -= 0.05

    # --- Unknown activity penalty ---
    # days_inactive == -1 means no ELO match history (all WTA players until results
    # are recorded via --record). We can't verify the player is currently active,
    # so apply a small penalty per player with unknown status.
    if days_inactive == -1:
        score -= 0.05

    # --- Validation penalty ---
    score -= validation.confidence_penalty
    if not validation.passed:
        score -= 0.30

    # --- Hard cap: close match cannot be VERY HIGH ---
    # gap < 0.05 = model prob between 0.45–0.55 (near coin-flip)
    # Score penalty alone (-0.10) is insufficient to prevent VERY HIGH
    cap = "HIGH" if gap < 0.05 else "VERY HIGH"

    # --- Classify ---
    raw = (
        "VERY HIGH" if score >= 1.20 else
        "HIGH"      if score >= 0.80 else
        "MEDIUM"    if score >= 0.40 else
        "LOW"
    )
    result = raw if raw != "VERY HIGH" or cap == "VERY HIGH" else cap

    # --- Hard gate: HIGH requires minimum pick quality ---
    # Data-availability bonuses (data_source + surface_n + ytd) can reach +0.90
    # for two well-known players, making HIGH achievable with near-zero edge or
    # conviction.  These two gates enforce a floor on actual pick quality signals,
    # independently of the total score.
    if result == "HIGH":
        if gap < 0.08:    # model within 8pp of 50/50 — not a confident prediction
            result = "MEDIUM"
        if edge < 0.12:   # less than 12% edge — not enough market inefficiency
            result = "MEDIUM"

    return result
