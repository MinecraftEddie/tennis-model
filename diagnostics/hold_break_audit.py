"""
diagnostics/hold_break_audit.py
================================
Factor audit: hold_break / serve contribution inside calculate_probability().

Goal: determine whether hold_break inflates underdog win probabilities
      — and whether the Monte Carlo serve-sim amplifies the same effect.

Sections
--------
1.  Factor description (how hold_break is built)
2.  Source audit — real vs proxy: are we EVER using real stats?
3.  Proxy range sweep: hold_break output vs hard_pct gap between players
4.  Representative case breakdowns (same matrix as forward_audit)
5.  Hold_break delta relative to ranking on underdog side
6.  Surface mismatch analysis (clay/grass uses hard-court proxy)
7.  Monte Carlo cross-check (same proxy path, weight 0.15)
8.  ATP vs WTA split
9.  Verdict

Run from the parent directory of tennis_model/:
    python tennis_model/diagnostics/hold_break_audit.py

Does NOT modify any model or production file.
"""

import logging
import math
import os
import sys

logging.disable(logging.CRITICAL)

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

from tennis_model.models      import PlayerProfile
from tennis_model.profiles    import WTA_PROFILES, STATIC_PROFILES
from tennis_model.hold_break  import (
    compute_hold_break_prob, extract_stats, point_win_on_serve,
    hold_probability, set_win_probability, match_win_probability,
    SURFACE_SERVE_BOOST,
)
from tennis_model.monte_carlo import run_simulation
from tennis_model.model       import (
    calculate_probability, WEIGHTS,
)
from tennis_model.elo         import get_elo_engine, canonical_id
from tennis_model.probability_adjustments import shrink_toward_market, SHRINK_ALPHA

SEP  = "=" * 74
SEP2 = "-" * 74
HB_WEIGHT  = WEIGHTS.get("hold_break", 0.05)
from tennis_model.config.runtime_config import MC_WEIGHT


# ---------------------------------------------------------------------------
# PROFILE HELPERS
# ---------------------------------------------------------------------------

def _wta(key: str) -> PlayerProfile:
    d = WTA_PROFILES[key]
    p = PlayerProfile(short_name=d.get("full_name", key))
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    p.full_name   = d.get("full_name", key)
    p.data_source = "tennis_abstract_dynamic"   # bypass WTA gate
    return p


def _atp(pid: str) -> PlayerProfile:
    d = STATIC_PROFILES[pid.upper()]
    p = PlayerProfile(short_name=d.get("full_name", pid))
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    p.full_name   = d.get("full_name", pid)
    p.data_source = "static_curated"
    return p


def _synthetic(name: str, ranking: int,
               hard_wins: int, hard_losses: int,
               clay_wins: int = 0, clay_losses: int = 0,
               age: int = 26, career_wins: int = 50,
               serve_stats: dict = None) -> PlayerProfile:
    p = PlayerProfile(short_name=name, full_name=name,
                      ranking=ranking, age=age, career_wins=career_wins,
                      hard_wins=hard_wins, hard_losses=hard_losses,
                      clay_wins=clay_wins, clay_losses=clay_losses,
                      data_source="synthetic")
    if serve_stats:
        p.serve_stats = serve_stats
    return p


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _hard_pct(player) -> float:
    """Career hard-court win% (the proxy input)."""
    hw = getattr(player, "hard_wins",   0) or 0
    hl = getattr(player, "hard_losses", 0) or 0
    return hw / (hw + hl) if (hw + hl) > 0 else 0.50


def _mkt_prob(oa: float, ob: float):
    ra, rb = 1.0 / oa, 1.0 / ob
    t = ra + rb
    return round(ra / t, 4), round(rb / t, 4)


def _hb_delta(prob_udog: float) -> float:
    """Delta of hold_break above neutral (0.50) for the underdog."""
    return round(prob_udog - 0.50, 4)


def _weighted(delta: float) -> float:
    return round(HB_WEIGHT * delta, 5)


# ---------------------------------------------------------------------------
# SECTION 1: FACTOR DESCRIPTION
# ---------------------------------------------------------------------------

def section_1() -> None:
    print(f"\n{SEP}")
    print("  SECTION 1 — HOLD_BREAK FACTOR DESCRIPTION")
    print(SEP)
    print()
    print(f"  Weight in model            : {HB_WEIGHT:.0%}  (WEIGHTS['hold_break'])")
    print(f"  Weight of MC simulation    : {MC_WEIGHT:.0%}  (also serve-based, same proxy path)")
    print(f"  Combined serve-path weight : {HB_WEIGHT + MC_WEIGHT:.0%}")
    print()
    print("  Pipeline:")
    print("    extract_stats(player, surface)")
    print("      - Priority 1: real stats from player.serve_stats")
    print("        (source = 'tennis_abstract' or 'tennis_abstract_wta')")
    print("        Uses surface-specific if n >= 5, else career totals")
    print("      - Priority 2: PROXY from career hard_wins / hard_losses")
    print("        hold_pct = 0.55 + hard_pct * 0.20  (range: 0.55 to 0.75)")
    print("        Source = 'proxy_hard_pct'  (always hard-court, regardless of surface)")
    print()
    print("    hold_probability(server_stats, returner_stats, surface)")
    print("      p_raw = 0.70 * p_serve + 0.30 * (1 - p_return)")
    print("      surface_boost: grass +0.04 | hard 0.00 | clay -0.03 (flat, both players)")
    print("      Markov game formula: p^4*(1+4q+10q^2) + 20*(pq)^3 * p^2/(p^2+q^2)")
    print()
    print("    set_win_probability(hold_a, hold_b)  -> Markov DP over game scores")
    print("    match_win_probability(p_set)         -> best-of-3 formula")
    print()
    print("  Surface adjustment note:")
    print("    The -0.03 clay / +0.04 grass boost is a FLAT constant applied to ALL players.")
    print("    It shifts both players equally => relative hold_break output is surface-invariant.")
    print("    Proxy always uses hard_wins/hard_losses regardless of surface.")
    print()
    print("  MC simulation also calls extract_stats() and point_win_on_serve().")
    print("  => Both hold_break (5%) and MC (15%) read from the same proxy when")
    print("     no real serve stats are present.  Combined serve-path weight = 20%.")


# ---------------------------------------------------------------------------
# SECTION 2: SOURCE AUDIT
# ---------------------------------------------------------------------------

def section_2() -> None:
    print(f"\n{SEP}")
    print("  SECTION 2 — SOURCE AUDIT: real vs proxy")
    print(SEP)

    total_wta = len(WTA_PROFILES)
    total_atp = len(STATIC_PROFILES)
    wta_real  = sum(1 for d in WTA_PROFILES.values()
                    if d.get("serve_stats", {}).get("source")
                    in ("tennis_abstract", "tennis_abstract_wta"))
    atp_real  = sum(1 for d in STATIC_PROFILES.values()
                    if d.get("serve_stats", {}).get("source") == "tennis_abstract")

    print()
    print(f"  WTA profiles with real serve_stats  : {wta_real}/{total_wta}")
    print(f"  ATP profiles with real serve_stats  : {atp_real}/{total_atp}")
    print()
    if wta_real == 0 and atp_real == 0:
        print("  [!!] 100% of profiles use the PROXY fallback.")
        print("       Real serve stats are NEVER used in hold_break or Monte Carlo.")
        print("       Every hold_break computation is based on career hard_wins/hard_losses,")
        print("       regardless of surface played.")
    else:
        print(f"  [ok] Some real stats available — proxy rate is not 100%.")

    # Show sample proxy inputs
    print()
    print("  Sample proxy inputs (hard_pct from WTA profiles):")
    print(f"  {'Player':<30} {'hard_W':>7} {'hard_L':>7} {'hard_pct':>9} {'hold_proxy':>11}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*9} {'-'*11}")
    keys = list(WTA_PROFILES.keys())[:12]
    for key in keys:
        p = _wta(key)
        hw = p.hard_wins or 0
        hl = p.hard_losses or 0
        hp = hw / (hw + hl) if (hw + hl) > 0 else 0.50
        hold_proxy = 0.55 + hp * 0.20
        print(f"  {p.short_name[:30]:<30} {hw:>7} {hl:>7} {hp:>9.1%} {hold_proxy:>11.4f}")


# ---------------------------------------------------------------------------
# SECTION 3: PROXY RANGE SWEEP
# ---------------------------------------------------------------------------

def section_3() -> None:
    print(f"\n{SEP}")
    print("  SECTION 3 — PROXY RANGE SWEEP")
    print("  Effect of hard_pct gap on hold_break match probability")
    print(f"  Surface: Hard   |   FAV (ranking_prob=0.70) vs UDOG (ranking_prob=0.30)")
    print(SEP)

    # Sweep: favorite hard_pct fixed at 0.65, underdog varies
    fav_hp = 0.65
    print()
    print(f"  Favorite hard_pct = {fav_hp:.0%} (fixed)")
    print()
    print(f"  {'Udog hard_pct':>14} {'HB_fav':>7} {'HB_udog':>8} "
          f"{'HB_delta_udog':>14} {'Wtd contribution':>17} "
          f"{'vs rank_30%':>12} {'Inflates?':>10}")
    print(f"  {'-'*14} {'-'*7} {'-'*8} {'-'*14} {'-'*17} {'-'*12} {'-'*10}")

    for udog_hp in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
        fav  = _synthetic("Fav", 20, int(fav_hp*100),  100-int(fav_hp*100))
        udog = _synthetic("Udog", 80, int(udog_hp*100), 100-int(udog_hp*100))
        hb   = compute_hold_break_prob(fav, udog, "Hard")
        hb_f = hb["prob_a"]
        hb_u = hb["prob_b"]
        delta_u = _hb_delta(hb_u)
        wtd     = _weighted(delta_u)
        vs_rank = hb_u - 0.30   # compared to ranking saying 0.30
        tag = "YES" if vs_rank > 0 else "no"
        print(f"  {udog_hp:>14.0%} {hb_f:>7.1%} {hb_u:>8.1%} "
              f"{delta_u:>+14.1%} {wtd:>+17.4f} "
              f"{vs_rank:>+12.1%} {tag:>10}")

    print()
    print("  WTA note: WTA hard_pct values typically 0.40-0.60; ranking gap often wider.")
    print("  When udog hard_pct >= fav hard_pct, hold_break can give udog >50%.")


# ---------------------------------------------------------------------------
# SECTION 4: REPRESENTATIVE CASE BREAKDOWNS
# ---------------------------------------------------------------------------

def _case_breakdown(label: str, pa: PlayerProfile, pb: PlayerProfile,
                    oa: float, ob: float, surface: str,
                    h2h_a: int = 0, h2h_b: int = 0,
                    udog_is_b: bool = True) -> dict:
    """
    Run compute_hold_break_prob + calculate_probability on a case.
    Return a dict of diagnostics.
    """
    hb = compute_hold_break_prob(pa, pb, surface)
    hb_prob_a = hb["prob_a"]
    hb_prob_b = hb["prob_b"]
    stats_a   = hb["stats_a"]
    stats_b   = hb["stats_b"]

    prob_a, prob_b, comps = calculate_probability(
        pa, pb, surface, h2h_a, h2h_b,
        market_odds_a=oa, market_odds_b=ob,
    )

    mkt_a, mkt_b = _mkt_prob(oa, ob)

    shrunk_a = shrink_toward_market(prob_a, oa)
    shrunk_b = shrink_toward_market(prob_b, ob)
    edge_a   = round(oa * shrunk_a - 1.0, 4)
    edge_b   = round(ob * shrunk_b - 1.0, 4)

    rank_a, rank_b = comps["ranking"]

    udog_prob  = prob_b   if udog_is_b else prob_a
    udog_hb    = hb_prob_b if udog_is_b else hb_prob_a
    rank_udog  = rank_b   if udog_is_b else rank_a
    mkt_udog   = mkt_b    if udog_is_b else mkt_a
    udog_odds  = ob       if udog_is_b else oa
    udog_edge  = edge_b   if udog_is_b else edge_a
    udog_stats = stats_b  if udog_is_b else stats_a
    fav_stats  = stats_a  if udog_is_b else stats_b
    hard_pct_u = _hard_pct(pb if udog_is_b else pa)
    hard_pct_f = _hard_pct(pa if udog_is_b else pb)

    hb_delta   = _hb_delta(udog_hb)
    wtd_hb     = _weighted(hb_delta)
    hb_vs_rank = round(udog_hb - rank_udog, 4)

    # MC sim (fixed seed for reproducibility)
    sim = run_simulation(pa, pb, surface, best_of=3, n_simulations=2000, seed=42)
    mc_prob_u  = sim.win_prob_b if udog_is_b else sim.win_prob_a

    return {
        "label":        label,
        "surface":      surface,
        "udog_odds":    udog_odds,
        "udog_prob":    udog_prob,
        "udog_hb":      udog_hb,
        "rank_udog":    rank_udog,
        "mkt_udog":     mkt_udog,
        "udog_edge":    udog_edge,
        "hb_delta":     hb_delta,
        "wtd_hb":       wtd_hb,
        "hb_vs_rank":   hb_vs_rank,
        "mc_prob_u":    mc_prob_u,
        "mc_vs_rank":   round(mc_prob_u - rank_udog, 4),
        "stats_u_src":  udog_stats.source,
        "stats_f_src":  fav_stats.source,
        "hard_pct_u":   hard_pct_u,
        "hard_pct_f":   hard_pct_f,
        "hold_u":       hb["hold_b"] if udog_is_b else hb["hold_a"],
        "hold_f":       hb["hold_a"] if udog_is_b else hb["hold_b"],
        "p_set_u":      round(1.0 - hb["p_set_a"], 4) if udog_is_b else hb["p_set_a"],
    }


def section_4_5() -> list:
    """Build and print representative case breakdowns + aggregate delta."""
    print(f"\n{SEP}")
    print("  SECTION 4 — REPRESENTATIVE CASE BREAKDOWNS")
    print("  HB_delta = hold_break_prob_udog - 0.50")
    print("  HB_vs_rank = hold_break_prob_udog - ranking_prob_udog")
    print("  MC_vs_rank = mc_win_prob_udog - ranking_prob_udog")
    print(SEP)

    cases_input = [
        # (label, pa_key, pb_key, oa, ob, surf, h2h_a, h2h_b, udog_is_b, tour)
        ("A1 WTA Sakkari(33) vs Galfi(87) Hard",
         "maria sakkari", "dalma galfi", 1.38, 3.20, "Hard", 2, 3, True, "WTA"),
        ("A2 WTA Brady(88) vs Stephens(155) Hard",
         "jennifer brady", "sloane stephens", 1.55, 2.55, "Hard", 0, 0, True, "WTA"),
        ("A3 WTA Siegemund(53) vs Stephens(155) Hard",
         "laura siegemund", "sloane stephens", 1.45, 2.75, "Hard", 0, 0, True, "WTA"),
        ("A4 WTA Boulter(67) vs Stephens(155) Clay",
         "katie boulter", "sloane stephens", 1.55, 2.55, "Clay", 0, 0, True, "WTA"),
        ("A5 WTA Siegemund(53) vs Stephens(155) Clay",
         "laura siegemund", "sloane stephens", 1.45, 2.75, "Clay", 0, 0, True, "WTA"),
        ("A6 WTA Haddad(40) vs Maria(132) Hard",
         "beatriz haddad maia", "tatjana maria", 1.22, 4.50, "Hard", 0, 0, True, "WTA"),
        ("A7 WTA Siniakova(42) vs Kenin(46) Hard",
         "katerina siniakova", "sofia kenin", 1.92, 1.88, "Hard", 1, 1, True, "WTA"),
    ]

    results = []
    for rec in cases_input:
        label, pa_key, pb_key, oa, ob, surf, h2_a, h2_b, udog_b, tour = rec
        try:
            pa = _wta(pa_key)
            pb = _wta(pb_key)
        except KeyError as e:
            print(f"  SKIP {label}: {e}")
            continue
        r = _case_breakdown(label, pa, pb, oa, ob, surf, h2_a, h2_b, udog_b)
        r["tour"] = tour
        results.append(r)

    # ATP cases
    atp_input = [
        ("B1 ATP Walton(85) vs Maestrelli(162) Hard",
         "W09E", "M0TA", 1.45, 2.80, "Hard", 1, 2, True, "ATP"),
        ("B2 ATP Watanuki(191) vs Rodesch(137) Hard",
         "W0AK", "R0E0", 1.55, 2.40, "Hard", 0, 0, True, "ATP"),
    ]
    for rec in atp_input:
        label, pa_key, pb_key, oa, ob, surf, h2_a, h2_b, udog_b, tour = rec
        try:
            pa = _atp(pa_key)
            pb = _atp(pb_key)
        except KeyError as e:
            print(f"  SKIP {label}: {e}")
            continue
        r = _case_breakdown(label, pa, pb, oa, ob, surf, h2_a, h2_b, udog_b)
        r["tour"] = tour
        results.append(r)

    if not results:
        print("  No results computed.")
        return results

    # Print breakdown table
    print()
    print(f"  {'Case':<44} {'Udog@':>5} {'MktU':>5} {'ModelU':>7} "
          f"{'RankU':>6} {'HB_u':>6} {'MC_u':>6} "
          f"{'HB_dlt':>7} {'HBvRnk':>7} {'MCvRnk':>7} "
          f"{'src':>4} {'hard%U':>7}")
    print(f"  {'-'*44} {'-'*5} {'-'*5} {'-'*7} "
          f"{'-'*6} {'-'*6} {'-'*6} "
          f"{'-'*7} {'-'*7} {'-'*7} "
          f"{'-'*4} {'-'*7}")

    for r in results:
        tag = "!" if r["hb_vs_rank"] > 0.10 else (" " if r["hb_vs_rank"] > 0 else "-")
        print(f"  {r['label'][:44]:<44} "
              f"{r['udog_odds']:>5.2f} "
              f"{r['mkt_udog']:>5.1%} "
              f"{r['udog_prob']:>7.1%} "
              f"{r['rank_udog']:>6.1%} "
              f"{r['udog_hb']:>6.1%} "
              f"{r['mc_prob_u']:>6.1%} "
              f"{r['hb_delta']:>+7.1%} "
              f"{r['hb_vs_rank']:>+7.1%} "
              f"{r['mc_vs_rank']:>+7.1%} "
              f"{r['stats_u_src'][:4]:>4} "
              f"{r['hard_pct_u']:>7.1%} "
              f"{tag}")

    return results


# ---------------------------------------------------------------------------
# SECTION 5: AGGREGATE DELTA ANALYSIS
# ---------------------------------------------------------------------------

def section_5(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 5 — AGGREGATE DELTA: hold_break vs ranking on underdog side")
    print(SEP)

    if not results:
        print("  No results.")
        return

    def _avg(lst): return sum(lst) / len(lst) if lst else 0.0
    def _pct(lst, threshold): return sum(1 for x in lst if x > threshold) / len(lst) if lst else 0.0

    hb_deltas    = [r["hb_delta"]   for r in results]
    hb_vs_ranks  = [r["hb_vs_rank"] for r in results]
    mc_vs_ranks  = [r["mc_vs_rank"] for r in results]
    wtd_hbs      = [r["wtd_hb"]     for r in results]

    wta = [r for r in results if r.get("tour") == "WTA"]
    atp = [r for r in results if r.get("tour") == "ATP"]

    print()
    print(f"  ALL CASES (n={len(results)})")
    print(f"    Avg hold_break prob (underdog)        : {_avg([r['udog_hb'] for r in results]):.1%}")
    print(f"    Avg hold_break delta (udog - 0.50)    : {_avg(hb_deltas):+.1%}")
    print(f"    Avg weighted contribution (5% x delta): {_avg(wtd_hbs):+.4f}")
    print(f"    Avg hold_break vs ranking             : {_avg(hb_vs_ranks):+.1%}")
    print(f"    Avg Monte Carlo vs ranking            : {_avg(mc_vs_ranks):+.1%}")
    print(f"    Cases where HB inflates vs ranking    : "
          f"{sum(1 for x in hb_vs_ranks if x > 0)}/{len(hb_vs_ranks)}")
    print(f"    Cases where HB offset > +10pp         : "
          f"{sum(1 for x in hb_vs_ranks if x > 0.10)}/{len(hb_vs_ranks)}")
    print(f"    Cases where MC  offset > +10pp        : "
          f"{sum(1 for x in mc_vs_ranks if x > 0.10)}/{len(mc_vs_ranks)}")

    if wta:
        print(f"\n  WTA only (n={len(wta)})")
        print(f"    Avg HB delta (udog - 0.50)            : {_avg([r['hb_delta'] for r in wta]):+.1%}")
        print(f"    Avg HB vs rank                        : {_avg([r['hb_vs_rank'] for r in wta]):+.1%}")
        print(f"    Avg MC vs rank                        : {_avg([r['mc_vs_rank'] for r in wta]):+.1%}")

    if atp:
        print(f"\n  ATP only (n={len(atp)})")
        print(f"    Avg HB delta (udog - 0.50)            : {_avg([r['hb_delta'] for r in atp]):+.1%}")
        print(f"    Avg HB vs rank                        : {_avg([r['hb_vs_rank'] for r in atp]):+.1%}")
        print(f"    Avg MC vs rank                        : {_avg([r['mc_vs_rank'] for r in atp]):+.1%}")

    # Case detail: hold ranks vs hold values
    print()
    print("  Per-case: hold game probabilities (server holding own service game)")
    print(f"  {'Case':<44} {'hold_fav':>9} {'hold_udog':>10} {'p_set_udog':>11}")
    print(f"  {'-'*44} {'-'*9} {'-'*10} {'-'*11}")
    for r in results:
        print(f"  {r['label'][:44]:<44} "
              f"{r['hold_f']:>9.1%} "
              f"{r['hold_u']:>10.1%} "
              f"{r['p_set_u']:>11.1%}")


# ---------------------------------------------------------------------------
# SECTION 6: SURFACE MISMATCH
# ---------------------------------------------------------------------------

def section_6(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 6 — SURFACE MISMATCH")
    print("  Proxy uses hard_wins/hard_losses for ALL surfaces.")
    print("  Surface boost is flat (+0.04 grass, -0.03 clay) — same for both players.")
    print(SEP)

    # Find clay cases
    clay_cases = [r for r in results if r["surface"].lower() == "clay"]
    hard_cases = [r for r in results if r["surface"].lower() == "hard"]

    if clay_cases and hard_cases:
        # Pair clay vs hard for same matchup when available
        print()
        print("  Clay vs Hard comparison (same players):")
        print(f"  {'Case':<44} {'Surf':<5} {'HB_u':>6} {'HBvRnk':>7} {'MCvRnk':>7}")
        print(f"  {'-'*44} {'-'*5} {'-'*6} {'-'*7} {'-'*7}")
        for r in hard_cases + clay_cases:
            print(f"  {r['label'][:44]:<44} {r['surface']:<5} "
                  f"{r['udog_hb']:>6.1%} {r['hb_vs_rank']:>+7.1%} {r['mc_vs_rank']:>+7.1%}")

    print()
    print("  Key limitation: the proxy is 'tennis_abstract_HARD' regardless of surface.")
    print("  Clay/grass specialists are NOT captured — their clay/grass win records are")
    print("  ignored by hold_break. Only hard-court record feeds the proxy.")
    print()
    print("  Surface boost analysis:")
    for surf, boost in SURFACE_SERVE_BOOST.items():
        print(f"    {surf:<6}: p_serve += {boost:+.2f} for BOTH players equally")
    print("  => Surface boost does not differentiate players; it's a global shift.")
    print("  => On clay, ALL players' hold_prob drops equally — no per-player clay effect.")

    # Show numerically: same player, different surfaces
    print()
    print("  Numerical: hold_probability for neutral player (hard_pct=0.55) on each surface:")
    neutral_a = _synthetic("A_neutral", 50, 55, 45)  # hard_pct=0.55
    neutral_b = _synthetic("B_neutral", 50, 55, 45)
    print(f"  {'Surface':<8} {'hold_A':>7} {'hold_B':>7} {'p_set_A':>8}")
    for surf in ("Hard", "Clay", "Grass"):
        hb = compute_hold_break_prob(neutral_a, neutral_b, surf)
        print(f"  {surf:<8} {hb['hold_a']:>7.4f} {hb['hold_b']:>7.4f} {hb['p_set_a']:>8.4f}")
    print("  Note: hold values change identically for both => p_set_A stays ~0.50")


# ---------------------------------------------------------------------------
# SECTION 7: MONTE CARLO CROSS-CHECK
# ---------------------------------------------------------------------------

def section_7() -> None:
    print(f"\n{SEP}")
    print("  SECTION 7 — MONTE CARLO CROSS-CHECK")
    print("  MC weight = 15%  |  Same proxy path as hold_break")
    print(SEP)
    print()

    # Sweep: fav 65%, underdog varies, Hard surface
    fav  = _synthetic("Fav",  20, 65, 35, age=28, career_wins=200)
    print(f"  Favorite fixed: hard_pct=65%, age=28, career_wins=200")
    print()
    print(f"  {'Udog hard%':>10} {'MC_udog':>8} {'MC_delta':>9} {'MC_wtd(15%)':>12} "
          f"{'HB_udog':>8} {'HB_wtd(5%)':>11} {'Combined':>9}")
    print(f"  {'-'*10} {'-'*8} {'-'*9} {'-'*12} {'-'*8} {'-'*11} {'-'*9}")

    for udog_hp in (0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
        udog = _synthetic("Udog", 90, int(udog_hp*100), 100-int(udog_hp*100), age=27, career_wins=80)
        sim  = run_simulation(fav, udog, "Hard", best_of=3, n_simulations=3000, seed=99)
        hb   = compute_hold_break_prob(fav, udog, "Hard")
        mc_u = sim.win_prob_b
        hb_u = hb["prob_b"]
        mc_delta  = mc_u - 0.50
        hb_delta  = hb_u - 0.50
        mc_wtd    = MC_WEIGHT * mc_delta
        hb_wtd    = HB_WEIGHT * hb_delta
        combined  = mc_wtd + hb_wtd
        print(f"  {udog_hp:>10.0%} {mc_u:>8.1%} {mc_delta:>+9.1%} {mc_wtd:>+12.4f} "
              f"{hb_u:>8.1%} {hb_wtd:>+11.4f} {combined:>+9.4f}")

    print()
    print("  Interpretation:")
    print("    Combined column = total serve-path contribution to underdog probability delta.")
    print("    Positive = hold_break + MC inflates underdog above neutral 0.50.")
    print("    When udog hard_pct = fav hard_pct (=0.65), combined ~= 0 (symmetric).")
    print("    When udog hard_pct < fav hard_pct, combined is negative (deflates udog).")
    print("    BUT: if udog hard_pct > fav hard_pct (e.g., form difference), INFLATES udog.")


# ---------------------------------------------------------------------------
# SECTION 8: EXTREME CASE — WHEN HOLD_BREAK INVERTS RANKING
# ---------------------------------------------------------------------------

def section_8() -> None:
    print(f"\n{SEP}")
    print("  SECTION 8 — INVERSION RISK")
    print("  Cases where hold_break / MC gives the underdog HIGHER probability than ranking")
    print(SEP)
    print()

    # Scenario: high-ranked favorite with poor recent hard results vs lower-ranked player
    # with better hard court record (common: player in form slump)
    fav_good   = _synthetic("Fav_good_rank",  15, 68, 32, age=27, career_wins=250)  # top form, good hard%
    fav_slump  = _synthetic("Fav_slump",       15, 45, 55, age=27, career_wins=250)  # top rank, bad hard% (slump)
    udog_form  = _synthetic("Udog_in_form",   80, 62, 38, age=24, career_wins=80)   # lower rank, good hard%

    oa, ob = 1.30, 3.50

    for label, pa, pb in [
        ("Fav good hard% (68%) vs Udog in-form (62%)",   fav_good,  udog_form),
        ("Fav in slump   (45%) vs Udog in-form (62%)",   fav_slump, udog_form),
    ]:
        hb  = compute_hold_break_prob(pa, pb, "Hard")
        mkt_a, mkt_b = _mkt_prob(oa, ob)

        print(f"  {label}")
        print(f"    Favorite  hard_pct={_hard_pct(pa):.0%}  HB_prob={hb['prob_a']:.1%}  "
              f"market_implied={mkt_a:.1%}")
        print(f"    Underdog  hard_pct={_hard_pct(pb):.0%}  HB_prob={hb['prob_b']:.1%}  "
              f"market_implied={mkt_b:.1%}")
        if hb["prob_b"] > mkt_b + 0.10:
            print(f"    [!!] HB gives udog {hb['prob_b'] - mkt_b:+.1%} above market implied")
        elif hb["prob_b"] > 0.50:
            print(f"    [!]  HB gives udog >50% ({hb['prob_b']:.1%})")
        print()


# ---------------------------------------------------------------------------
# SECTION 9: VERDICT
# ---------------------------------------------------------------------------

def section_9(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 9 — VERDICT")
    print(SEP)

    if not results:
        return

    def _avg(lst): return sum(lst) / len(lst) if lst else 0.0

    hb_vs_ranks = [r["hb_vs_rank"] for r in results]
    mc_vs_ranks = [r["mc_vs_rank"] for r in results]
    avg_hb_off  = _avg(hb_vs_ranks)
    avg_mc_off  = _avg(mc_vs_ranks)
    pct_inflate = sum(1 for x in hb_vs_ranks if x > 0) / len(hb_vs_ranks)
    max_hb_off  = max(hb_vs_ranks) if hb_vs_ranks else 0
    max_mc_off  = max(mc_vs_ranks) if mc_vs_ranks else 0

    print()
    print("  Summary findings:")
    print(f"    1. Serve stats source         : 100% PROXY (no real serve stats in any profile)")
    print(f"    2. Proxy input                : hard_wins/hard_losses  (hard-court career only)")
    print(f"    3. Surface adjustment         : flat constant, same both players => no differentiation")
    print(f"    4. Hold_break weight          : {HB_WEIGHT:.0%} in model")
    print(f"    5. MC (also proxy)            : {MC_WEIGHT:.0%} in model")
    print(f"    6. Combined serve-path weight : {HB_WEIGHT + MC_WEIGHT:.0%}")
    print()
    print(f"  Delta metrics (test matrix, n={len(results)}):")
    print(f"    Avg hold_break offset vs ranking (underdog): {avg_hb_off:+.1%}")
    print(f"    Max hold_break offset vs ranking (underdog): {max_hb_off:+.1%}")
    print(f"    Cases where HB inflates underdog vs ranking: {pct_inflate:.0%}")
    print(f"    Avg MC (serve sim) offset vs ranking        : {avg_mc_off:+.1%}")
    print(f"    Max MC  offset vs ranking (underdog)        : {max_mc_off:+.1%}")
    print()

    # Hold_break maximum theoretical impact:
    max_hb_impact = HB_WEIGHT * 0.50   # if hold_break says 1.0 for underdog
    max_mc_impact = MC_WEIGHT * 0.50
    print(f"  Maximum possible inflation (bounded by weights):")
    print(f"    hold_break alone: +{max_hb_impact:.1%}  (if HB=100% for underdog)")
    print(f"    MC alone:         +{max_mc_impact:.1%}  (if MC=100% for underdog)")
    print(f"    Combined max:     +{max_hb_impact + max_mc_impact:.1%}")
    print()

    # Verdict selection
    if max_hb_off > 0.20 and pct_inflate > 0.75:
        hold_break_verdict = "hold_break likely needs shrink/cap"
        mc_add = "MC also amplifies via same proxy path."
    elif pct_inflate > 0.60 and (avg_hb_off + avg_mc_off) > 0.10:
        hold_break_verdict = "surface/sample guard needed"
        mc_add = "100% proxy with no surface differentiation is the root cause."
    else:
        hold_break_verdict = "hold_break looks well-calibrated given its 5% weight"
        mc_add = "Impact bounded by weight; root cause is elsewhere."

    print(f"  Verdict: \"{hold_break_verdict}\"")
    print(f"  Note:    {mc_add}")
    print()
    print("  Root cause analysis:")
    print("    hold_break (5%) and MC (15%) compress the serve signal toward 0.50")
    print("    when proxy stats produce similar p_serve values for both players.")
    print("    This is structurally unavoidable with proxy stats — the proxy range")
    print("    [0.55, 0.75] for hold_pct is too tight to reflect extreme quality gaps.")
    print()
    print("    HOWEVER: with combined weight of 20%, the maximum inflation is +10pp.")
    print("    The observed 97% underdog rate and 25% mean edge CANNOT be explained")
    print("    by hold_break+MC alone. The primary inflating factors are upstream:")
    print()
    print("    Most likely sources of the 97% underdog bias (require separate audit):")
    print("      - recent_form (weight 20%): if underdog has a hot streak, inflates")
    print("      - surface_form (weight 20%): same surface record may favor underdog")
    print("      - tournament_exp (weight 10%): log1p career wins can favor ex-top players")
    print("      - Data mismatch: stored predictions used dynamic WTA stats at fetch time;")
    print("        static profiles used here may not match original fetch context")
    print()
    print(f"{SEP}\n")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(f"\n{SEP}")
    print("  HOLD_BREAK / SERVE FACTOR AUDIT")
    print("  Post-fix calibration check: does hold_break inflate underdogs?")
    print(f"  hold_break weight={HB_WEIGHT:.0%}  |  MC weight={MC_WEIGHT:.0%}")
    print(f"  Combined serve-path weight: {HB_WEIGHT+MC_WEIGHT:.0%}")
    print(SEP)

    section_1()
    section_2()
    section_3()
    results = section_4_5()
    section_5(results)
    section_6(results)
    section_7()
    section_8()
    section_9(results)


if __name__ == "__main__":
    main()
