from __future__ import annotations  # lazy annotation eval — MatchPick not yet in its own module

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# THRESHOLDS (used by formatter; pipeline imports these too)
# ──────────────────────────────────────────────────────────────────────────────

EDGE_ALERT_THRESHOLD   = 7.0
EDGE_DISPLAY_THRESHOLD = 4.0

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
    return "\n".join(lines)


def format_factor_table(pick: MatchPick) -> str:
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
        "recent_form":        "Recent form      (15%)",
        "h2h":                "Head-to-head     (10%)",
        "tournament_exp":     "Career exp       (10%)",
        "career_surface_pct": "Surface career%   (5%)",
        "hold_break":         "Hold/Break prob  (10%)",
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


def format_value_analysis(pick: MatchPick) -> str:
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

    lines.append("⚠️  Analytical output only. Not financial advice.")
    return "\n".join(lines)
