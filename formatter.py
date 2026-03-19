from __future__ import annotations  # lazy annotation eval — MatchPick not yet in its own module

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# THRESHOLDS (used by formatter; pipeline imports these too)
# ──────────────────────────────────────────────────────────────────────────────

EDGE_ALERT_THRESHOLD   = 7.0
EDGE_DISPLAY_THRESHOLD = 4.0

_SRC_ABBREV = {
    "tennis_abstract_dynamic": "ta_dyn",
    "tennis_abstract_wta":     "ta_wta",
    "tennis_abstract":         "ta",
    "static_curated":          "static",
    "atp_api":                 "atp_api",
    "wta_static":              "wta_s",
    "wta_estimated":           "est",
    "unknown":                 "?",
}

def _src(s: str) -> str:
    return _SRC_ABBREV.get(s, s[:10] if s else "?")

def _serve_provenance(player) -> str:
    ss = getattr(player, "serve_stats", {}) or {}
    src = _src(ss.get("source", "?"))
    n   = ss.get("career", {}).get("n") or ss.get("hard", {}).get("n") or "?"
    return f"{src} n={n}"


def _quality_tier(pick: "MatchPick") -> str:
    """
    Operational quality tier derived from visible data signals only.
    Returns "CLEAN", "CAUTION", or "FRAGILE".  No model logic.

    FRAGILE  — critical data gaps: suspicious edge, WTA serve n<5, unknown data source
    CLEAN    — real data, adequate samples, alert_level medium/high, credible edge (≤35%)
    CAUTION  — all other alerts (low confidence, thin sample, unmapped players, etc.)
    """
    er    = getattr(pick, "evaluator_result", {}) or {}
    flags = er.get("risk_flags", [])
    level = er.get("alert_level", "low")

    def _n(player) -> "int | None":
        ss = getattr(player, "serve_stats", {}) or {}
        return ss.get("career", {}).get("n") or ss.get("hard", {}).get("n") or None

    # ── FRAGILE ────────────────────────────────────────────────────────────
    if "suspicious_edge_magnitude" in flags:
        return "FRAGILE"
    if pick.player_a.data_source in ("unknown", "wta_estimated") or \
       pick.player_b.data_source in ("unknown", "wta_estimated"):
        return "FRAGILE"
    if "wta_serve_sample_too_small" in flags:
        na, nb = _n(pick.player_a), _n(pick.player_b)
        if (na is not None and na < 5) or (nb is not None and nb < 5):
            return "FRAGILE"

    # ── CLEAN ──────────────────────────────────────────────────────────────
    _GOOD_SRC = {"tennis_abstract", "tennis_abstract_dynamic", "static_curated", "atp_api"}
    best_edge = max(pick.edge_a or 0.0, pick.edge_b or 0.0)
    if (level in ("medium", "high")
            and "wta_serve_sample_too_small" not in flags
            and pick.player_a.data_source in _GOOD_SRC
            and pick.player_b.data_source in _GOOD_SRC
            and best_edge <= 35.0):
        return "CLEAN"

    return "CAUTION"


# ──────────────────────────────────────────────────────────────────────────────
# FORMATTING HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _pct(w: int, l: int) -> str:
    t = w + l
    return f"{int(100*w/t)}" if t > 0 else "N/A"

# ──────────────────────────────────────────────────────────────────────────────
# OUTPUT FORMATTING
# ──────────────────────────────────────────────────────────────────────────────

def format_pick_card(pick: MatchPick, number: int = 1) -> str:
    pa, pb = pick.player_a, pick.player_b

    # Blocked pick: show reason prominently; skip misleading edge/prob display
    if getattr(pick, "filter_reason", ""):
        return "\n".join([
            f"🔥 {number}. {pa.short_name} vs {pb.short_name}",
            f"🌍 {pick.tournament} · {pick.tour}  ·  {pick.surface}",
            f"⛔ BLOCKED: {pick.filter_reason}",
            f"   {pa.short_name}: {pa.data_source}  |  {pb.short_name}: {pb.data_source}",
        ])

    if pick.pick_player == pb.short_name:
        fav, prob, fo, mo, edge = pb.short_name, pick.prob_b, pick.fair_odds_b, pick.market_odds_b, pick.edge_b
    else:
        fav, prob, fo, mo, edge = pa.short_name, pick.prob_a, pick.fair_odds_a, pick.market_odds_a, pick.edge_a

    edge_str = (f"+{edge:.1f}%" if edge and edge > 0 else f"{edge:.1f}%" if edge else "— no market odds")
    mkt_str  = f"@{mo}" if mo else "—"

    lines = [
        f"🔥 {number}. {pa.short_name} vs {pb.short_name}",
        f"🌍 {pick.tournament} · {pick.tour}",
        f"📊 Edge: {edge_str}  ·  Prob: {int(prob * 100)}%",
        f"💰 Odds: {mkt_str}  ·  Fair: @{fo:.2f}",
    ]
    if edge and edge >= EDGE_ALERT_THRESHOLD:
        lines.append(f"🚀 VALUE: Back {fav} {mkt_str}")
    if pick.bookmaker:
        lines.append(f"📖 {pick.bookmaker}")
    lines.append(f"🔒 Confidence: {getattr(pick, 'confidence', 'LOW')}")
    _tier    = getattr(pick, "quality_tier", None) or _quality_tier(pick)
    _tierstr = {"CLEAN": "🟢 CLEAN", "CAUTION": "🟡 CAUTION", "FRAGILE": "🔴 FRAGILE"}.get(_tier, "CAUTION")
    lines.append(f"🏷  Quality:    {_tierstr}")
    for w in getattr(pick, 'validation_warnings', []):
        lines.append(f"⚠️  {w}")
    if pick.simulation:
        sim = pick.simulation
        if pick.pick_player == pick.player_b.short_name:
            mc_name = pick.player_b.short_name
            mc_prob = sim['win_prob_b']
        else:
            mc_name = pick.player_a.short_name
            mc_prob = sim['win_prob_a']
        lines.append(
            f"🎲 MC: {mc_name} wins {mc_prob*100:.0f}%"
            f" | 3-sets {sim['three_set_prob']*100:.0f}%"
            f" | TB {sim['tiebreak_prob']*100:.0f}%"
        )
    lines += [
        f"⚔️  H2H: {pick.h2h_summary}",
        f"📅 Surface: {pick.surface}",
    ]
    er = getattr(pick, "evaluator_result", {})
    if er:
        n_flags = len(er.get("risk_flags", []))
        lines.append(
            f"🔍 Evaluator: {er.get('alert_level', '—').upper()} | "
            f"{er.get('recommended_action', '—').upper()} | "
            f"Flags: {n_flags}"
        )
        if er.get("recommended_action") == "send_with_caution":
            lines.append("⚠️ CAUTION: Reduced stake — model calibrating (N<30)")
    lines.append(
        f"🔬 {pa.short_name}: {_src(pa.data_source)} / serve={_serve_provenance(pa)}"
        f"  |  {pb.short_name}: {_src(pb.data_source)} / serve={_serve_provenance(pb)}"
    )
    return "\n".join(lines)


def format_factor_table(pick: MatchPick) -> str:
    if getattr(pick, "filter_reason", ""):
        return ""  # suppressed for blocked picks
    pa, pb = pick.player_a, pick.player_b
    na = pa.short_name[:14]
    nb = pb.short_name[:14]
    sep = "─" * 72
    lines = [
        f"\n┌{sep}┐",
        f"│ {'Factor (weight)':<28} │ {'Score A':^10} │ {'Score B':^10} │ {'Edge':^12} │",
        f"├{sep}┤",
    ]
    labels = {
        "ranking":            "Ranking          (20%)",
        "surface_form":       "Surface form     (20%)",
        "recent_form":        "Recent form      (20%)",
        "h2h":                "Head-to-head     (10%)",
        "tournament_exp":     "Career exp       (10%)",
        "career_surface_pct": "Surface career%   (5%)",
        "hold_break":         "Hold/Break prob   (5%)",
        "physical":           "Physical          (5%)",
        "rest":               "Rest factor       (5%)",
    }
    for k, label in labels.items():
        sa, sb = pick.factor_breakdown.get(k, (0.5, 0.5))
        arrow = f"{na} ◀" if sa > sb + 0.02 else (f"▶ {nb}" if sb > sa + 0.02 else "  even  ")
        lines.append(f"│ {label:<28} │ {sa:^10.3f} │ {sb:^10.3f} │ {arrow:^12} │")
    lines += [
        f"├{sep}┤",
        f"│ {'FINAL PROBABILITY':<28} │ {pick.prob_a:^10.1%} │ {pick.prob_b:^10.1%} │ {'':^12} │",
        f"│ {'FAIR ODDS':<28} │ {('@'+str(pick.fair_odds_a)):^10} │ {('@'+str(pick.fair_odds_b)):^10} │ {'':^12} │",
        f"└{sep}┘",
    ]
    return "\n".join(lines)


def _kelly_stake(prob: float, decimal_odds: float,
                 fraction: float = 0.5, cap: float = 0.05) -> float:
    """Half-Kelly stake as fraction of bankroll, capped at `cap` (default 5%)."""
    b = decimal_odds - 1.0
    q = 1.0 - prob
    full_kelly = (b * prob - q) / b if b > 0 else 0.0
    return round(max(0.0, min(full_kelly * fraction, cap)), 4)


def format_value_analysis(pick: MatchPick) -> str:
    if getattr(pick, "filter_reason", ""):
        return ""  # suppressed for blocked picks
    pa, pb = pick.player_a, pick.player_b
    lines  = ["\n📈 VALUE ANALYSIS:"]

    r_lead  = pa.short_name if pa.ranking <= pb.ranking else pb.short_name
    r_other = pb.short_name if r_lead == pa.short_name else pa.short_name
    lines.append(f"✅ Ranking: {r_lead} #{min(pa.ranking,pb.ranking)} vs {r_other} #{max(pa.ranking,pb.ranking)}")

    s = pick.surface.lower()
    lines.append(
        f"🎾 {pick.surface} W%: {pa.short_name} {_pct(getattr(pa,f'{s}_wins',0),getattr(pa,f'{s}_losses',0))}% "
        f"| {pb.short_name} {_pct(getattr(pb,f'{s}_wins',0),getattr(pb,f'{s}_losses',0))}%"
    )

    def f5(p): return "-".join(p.recent_form[-5:]) or "N/A"
    lines.append(f"🔥 Form L5: {pa.short_name} {f5(pa)} | {pb.short_name} {f5(pb)}")

    max_e = max(pick.edge_a or -999, pick.edge_b or -999)
    if max_e >= EDGE_ALERT_THRESHOLD:
        lines.append(f"⚡ STRONG EDGE: +{max_e:.1f}% (threshold {EDGE_ALERT_THRESHOLD}%)")
    elif max_e > 10:
        lines.append(f"ℹ️  Moderate edge: +{max_e:.1f}%")
    elif max_e > 0:
        lines.append(f"📉 Weak edge: +{max_e:.1f}%")
    else:
        lines.append("⚠️  No positive edge vs these odds")

    # Kelly stake — half-Kelly normally, quarter-Kelly for cautious picks
    er = getattr(pick, "evaluator_result", {})
    is_cautious = er.get("recommended_action") == "send_with_caution"
    kelly_fraction = 0.25 if is_cautious else 0.5
    kelly_label    = "¼" if is_cautious else "½"
    best_prob, best_odds = None, None
    if (pick.edge_a or 0) >= (pick.edge_b or 0) and pick.edge_a and pick.edge_a > 0:
        best_prob, best_odds = pick.prob_a, pick.market_odds_a
    elif pick.edge_b and pick.edge_b > 0:
        best_prob, best_odds = pick.prob_b, pick.market_odds_b
    if best_prob is not None and best_odds is not None:
        ks = _kelly_stake(best_prob, best_odds, fraction=kelly_fraction)
        lines.append(
            f"💹 Kelly ({kelly_label}): {ks*100:.2f}% of bankroll  "
            f"[e.g. £{ks*1000:.2f} on £1,000 bank]"
        )
    else:
        lines.append(f"💹 Kelly ({kelly_label}): — no positive edge")

    lines.append(f"📡 Odds source: {getattr(pick, 'odds_source', 'manual').upper()}")
    er = getattr(pick, "evaluator_result", {})
    if er and er.get("recommended_action") not in ("send", None, ""):
        action = er.get("recommended_action", "").upper()
        reasons = er.get("reasons", [])
        flags   = er.get("risk_flags", [])
        lines.append(f"🔍 Evaluator verdict: {action}")
        if reasons:
            lines.append(f"   └ Reasons: {'; '.join(reasons[:3])}")
        if flags:
            lines.append(f"   └ Flags: {', '.join(flags[:3])}")
    lines.append("⚠️  Analytical output only. Not financial advice.")
    return "\n".join(lines)
