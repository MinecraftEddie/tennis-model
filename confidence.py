from tennis_model.validation import ValidationResult


def compute_confidence(pa, pb, surface: str,
                       validation: ValidationResult,
                       edge: float,
                       model_prob: float) -> str:

    score = 0.0
    surf = surface.lower()

    # --- Data source quality ---
    source_scores = {
        "static_curated": 0.30,
        "wta_static":     0.25,
        "atp_api":        0.15,
        "tennis_abstract":0.10,
        "unknown":       -0.20,
        "fallback":      -0.20,
    }
    score += source_scores.get(pa.data_source, 0.0)
    score += source_scores.get(pb.data_source, 0.0)

    # --- Surface sample depth ---
    for p in [pa, pb]:
        n = getattr(p, f"{surf}_wins", 0) + getattr(p, f"{surf}_losses", 0)
        if n >= 50:   score += 0.20
        elif n >= 30: score += 0.12
        elif n >= 15: score += 0.05
        elif n < 10:  score -= 0.10

    # --- Season activity ---
    for p in [pa, pb]:
        ytd = p.ytd_wins + p.ytd_losses
        if ytd >= 15:   score += 0.10
        elif ytd >= 8:  score += 0.05
        elif ytd == 0:  score -= 0.20

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

    # --- Validation penalty ---
    score -= validation.confidence_penalty
    if not validation.passed:
        score -= 0.30

    # --- Hard cap: close match cannot be VERY HIGH ---
    # gap < 0.05 = model prob between 0.45–0.55 (near coin-flip)
    # Score penalty alone (-0.10) is insufficient to prevent VERY HIGH
    cap = "HIGH" if gap < 0.05 else "VERY HIGH"

    # --- Classify ---
    _labels = ["LOW", "MEDIUM", "HIGH", "VERY HIGH"]
    raw = (
        "VERY HIGH" if score >= 1.20 else
        "HIGH"      if score >= 0.80 else
        "MEDIUM"    if score >= 0.40 else
        "LOW"
    )
    return raw if raw != "VERY HIGH" or cap == "VERY HIGH" else cap
