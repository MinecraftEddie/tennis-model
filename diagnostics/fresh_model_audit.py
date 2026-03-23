"""
diagnostics/fresh_model_audit.py
==================================
Post-fix model audit using FRESHLY CONSTRUCTED test inputs only.
Does NOT read data/predictions.json.

Test matrix (12 cases):
  Group A — both players have ELO history (fallback must NOT fire)
  Group B — both players have no ELO history (fallback fires, uses market prior)
  Group C — one player has ELO, other does not (fallback must NOT fire)

Sections
--------
1. Test matrix summary + raw model output
2. Edge distribution across the matrix
3. Factor decomposition for selected cases
4. Aggregate factor analysis (underdog-side deltas)
5. ELO fallback specific audit (with vs without odds)
6. Verdict (4 diagnostic questions)

Run from the parent directory of tennis_model/:
    python tennis_model/diagnostics/fresh_model_audit.py

Does NOT modify any model, scoring, or production file.
"""

import logging
import os
import sys

logging.disable(logging.CRITICAL)

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
_PARENT = os.path.dirname(_ROOT)
sys.path.insert(0, _PARENT)

from tennis_model.models   import PlayerProfile
from tennis_model.profiles import STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
from tennis_model.model    import (
    calculate_probability, WEIGHTS,
    _surface_form_score, _form_score, _h2h_score,
    _exp_score, _surface_score, _physical_score, _rest_score,
)
from tennis_model.elo      import get_elo_engine, canonical_id
from tennis_model.probability_adjustments import shrink_toward_market, SHRINK_ALPHA

SEP  = "=" * 74
SEP2 = "-" * 74


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _wta(key: str) -> PlayerProfile:
    d = WTA_PROFILES[key]
    p = PlayerProfile(short_name=d.get("full_name", key))
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    p.full_name   = d.get("full_name", key)
    p.data_source = "wta_static"
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


def _qualifier(name: str = "Qualifier") -> PlayerProfile:
    return PlayerProfile(short_name=name, full_name=name,
                         ranking=9999, data_source="qualifier")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _mkt_prob(oa: float, ob: float) -> tuple:
    ra, rb = 1.0 / oa, 1.0 / ob
    t = ra + rb
    return round(ra / t, 4), round(rb / t, 4)


def _edge(prob: float, odds: float) -> float:
    return round(odds * prob - 1.0, 4)


def _elo_history(name: str) -> int:
    """Return matches_played for this player in the ELO store (0 if absent)."""
    _elo = get_elo_engine()
    pid  = canonical_id(name)
    e    = _elo.ratings.get(pid)
    return e.matches_played if e else 0


def _decompose(comps: dict) -> dict:
    out = {}
    for k, w in WEIGHTS.items():
        if k not in comps or not isinstance(comps[k], tuple):
            continue
        a, b = comps[k]
        out[k] = {
            "weight":  w,
            "a_raw":   round(a, 4),
            "b_raw":   round(b, 4),
            "delta_a": round(w * (a - 0.50), 4),
            "delta_b": round(w * (b - 0.50), 4),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TEST MATRIX
# ─────────────────────────────────────────────────────────────────────────────
# Each case: (label, pa, pb, oa, ob, surface, h2h_a, h2h_b, underdog_is_b)
#   underdog_is_b = True → B has longer market odds
# ─────────────────────────────────────────────────────────────────────────────

def build_test_cases() -> list:
    cases = []

    # ── GROUP A: both ELO available — fallback must NOT fire ──────────────────
    # Sakkari + Galfi both have mp=2 in elo_ratings.json
    cases.append((
        "A1 [WTA both-ELO] Sakkari(33) vs Galfi(87) — real ELO both sides",
        _wta("maria sakkari"), _wta("dalma galfi"),
        1.38, 3.20, "Hard", 2, 3, True,
    ))
    # Stephens + Brady both have mp≥1
    cases.append((
        "A2 [WTA both-ELO] Brady(88) vs Stephens(155) — real ELO both sides",
        _wta("jennifer brady"), _wta("sloane stephens"),
        1.55, 2.55, "Hard", 0, 0, True,
    ))
    # Siegemund + Stephens both have mp≥1
    cases.append((
        "A3 [WTA both-ELO] Siegemund(53) vs Stephens(155) — veteran contrast",
        _wta("laura siegemund"), _wta("sloane stephens"),
        1.45, 2.75, "Hard", 0, 0, True,
    ))

    # ── GROUP B: both ELO absent — fallback FIRES ─────────────────────────────
    # WTA — strong favorite vs veteran underdog (no ELO for either)
    cases.append((
        "B1 [WTA no-ELO] Baptiste(45) vs T.Maria(132) — strong fav, 1.22/4.50",
        _wta("hailey baptiste"), _wta("tatjana maria"),
        1.22, 4.50, "Hard", 0, 0, True,
    ))
    # WTA — moderate gap
    cases.append((
        "B2 [WTA no-ELO] Boulter(67) vs Stephens(155) — moderate 1.55/2.55",
        _wta("katie boulter"), _wta("sloane stephens"),
        1.55, 2.55, "Hard", 0, 0, True,
    ))
    # WTA — near-even market
    cases.append((
        "B3 [WTA no-ELO] Siniakova(42) vs Kenin(46) — near-even 1.92/1.88",
        _wta("katerina siniakova"), _wta("sofia kenin"),
        1.92, 1.88, "Hard", 1, 1, True,
    ))
    # WTA — qualifier extreme (no profile for qualifier)
    cases.append((
        "B4 [WTA no-ELO] Cristian(36) vs Qualifier — extreme 1.18/5.80",
        _wta("jaqueline cristian"), _qualifier(),
        1.18, 5.80, "Hard", 0, 0, True,
    ))
    # WTA — Venus Williams extreme age-decay test
    cases.append((
        "B5 [WTA no-ELO] Linette(50) vs Venus(730) — age+career 1.08/10.00",
        _wta("magda linette"), _wta("venus williams"),
        1.08, 10.00, "Hard", 0, 0, True,
    ))
    # ATP — both from STATIC_PROFILES, no ELO
    cases.append((
        "B6 [ATP no-ELO] Walton(85) vs Maestrelli(162) — ATP gap 1.45/2.80",
        _atp("W09E"), _atp("M0TA"),
        1.45, 2.80, "Hard", 1, 2, True,
    ))
    # ATP — hot streak underdog (Rodesch 10W streak)
    cases.append((
        "B7 [ATP no-ELO] Watanuki(191) vs Rodesch(137) — hot streak udog",
        _atp("W0AK"), _atp("R0E0"),
        1.55, 2.40, "Hard", 0, 0, True,
    ))

    # ── GROUP C: one ELO, one not — fallback must NOT fire ────────────────────
    # Brady has ELO (mp≥1), Boulter does not
    cases.append((
        "C1 [WTA mixed-ELO] Brady(88) vs Boulter(67) — Brady has ELO",
        _wta("jennifer brady"), _wta("katie boulter"),
        2.10, 1.72, "Hard", 0, 0, False,
    ))
    # Galfi has ELO (mp=2), Siniakova does not
    cases.append((
        "C2 [WTA mixed-ELO] Galfi(87) vs Siniakova(42) — Galfi has ELO",
        _wta("dalma galfi"), _wta("katerina siniakova"),
        2.85, 1.40, "Hard", 0, 0, False,
    ))

    return cases


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — RAW MODEL OUTPUT TABLE
# ─────────────────────────────────────────────────────────────────────────────

def run_output_table(cases: list) -> list:
    print(f"\n{SEP}")
    print("  SECTION 1 — RAW MODEL OUTPUT  (12 fresh test cases, no predictions.json)")
    print(SEP)

    results = []
    for label, pa, pb, oa, ob, surf, h2h_a, h2h_b, udog_is_b in cases:
        idx = label.split()[0]
        try:
            prob_a, prob_b, comps = calculate_probability(
                pa, pb, surf, h2h_a, h2h_b,
                market_odds_a=oa, market_odds_b=ob,
            )
        except Exception as e:
            print(f"  {idx:<4}  ERROR: {e}")
            results.append(None)
            continue

        mkt_a, mkt_b = _mkt_prob(oa, ob)
        gap_a = prob_a - mkt_a

        shrunk_a = shrink_toward_market(prob_a, oa)
        shrunk_b = shrink_toward_market(prob_b, ob)
        edge_a   = _edge(shrunk_a, oa)
        edge_b   = _edge(shrunk_b, ob)

        pick_edge = edge_b if udog_is_b else edge_a
        pick_prob = shrunk_b if udog_is_b else shrunk_a
        pick_mkt  = mkt_b   if udog_is_b else mkt_a
        pick_odds = ob       if udog_is_b else oa

        mp_a = _elo_history(pa.full_name or pa.short_name)
        mp_b = _elo_history(pb.full_name or pb.short_name)
        if mp_a > 0 and mp_b > 0:
            elo_tag = "BOTH"
        elif mp_a > 0 or mp_b > 0:
            elo_tag = "ONE"
        else:
            elo_tag = "NONE"

        results.append({
            "label": label, "idx": idx,
            "pa": pa, "pb": pb,
            "oa": oa, "ob": ob, "surf": surf,
            "h2h_a": h2h_a, "h2h_b": h2h_b,
            "udog_is_b": udog_is_b,
            "prob_a": prob_a, "prob_b": prob_b,
            "mkt_a": mkt_a, "mkt_b": mkt_b, "gap_a": gap_a,
            "shrunk_a": shrunk_a, "shrunk_b": shrunk_b,
            "edge_a": edge_a, "edge_b": edge_b,
            "pick_edge": pick_edge, "pick_prob": pick_prob,
            "pick_mkt": pick_mkt, "pick_odds": pick_odds,
            "elo_tag": elo_tag, "mp_a": mp_a, "mp_b": mp_b,
            "comps": comps,
        })

    # Print summary table
    print(f"\n  {'Idx':<4}  {'ModelA':>7}  {'MktA':>6}  {'GapA':>6}  "
          f"{'PickEdge':>9}  {'PickProb':>9}  {'PickMkt':>8}  ELO  Group+Label")
    print(f"  {'-'*4}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*9}  {'-'*9}  {'-'*8}  {'-'*4}  {'-'*42}")
    for r in results:
        if r is None:
            continue
        flag = " !" if r["pick_edge"] > 0.20 else ""
        print(f"  {r['idx']:<4}  {r['prob_a']:>7.1%}  {r['mkt_a']:>6.1%}  {r['gap_a']:>+6.1%}  "
              f"{r['pick_edge']:>9.1%}  {r['pick_prob']:>9.1%}  {r['pick_mkt']:>8.1%}  "
              f"{r['elo_tag']:<4}  {r['label'][3:].strip()[:42]}{flag}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — EDGE DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def run_edge_distribution(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 2 — EDGE DISTRIBUTION  (pick-side edge after shrink alpha=0.70)")
    print(SEP)

    valid = [r for r in results if r]

    def _stats(lst, label):
        if not lst:
            print(f"  {label}: no data")
            return
        s = sorted(lst)
        n = len(s)
        mean = sum(s) / n
        print(f"  {label} (n={n}): "
              f"mean={mean:.1%}  median={s[n//2]:.1%}  "
              f"min={s[0]:.1%}  max={s[-1]:.1%}  "
              f">10%:{sum(1 for x in s if x>0.10)}/{n}  "
              f">20%:{sum(1 for x in s if x>0.20)}/{n}  "
              f"neg:{sum(1 for x in s if x<0)}/{n}")

    _stats([r["pick_edge"] for r in valid],                         "ALL")
    _stats([r["pick_edge"] for r in valid if r["elo_tag"] == "BOTH"], "GROUP A (both ELO)")
    _stats([r["pick_edge"] for r in valid if r["elo_tag"] == "NONE"], "GROUP B (no ELO)")
    _stats([r["pick_edge"] for r in valid if r["elo_tag"] == "ONE"],  "GROUP C (mixed ELO)")

    # Gap from market
    print()
    _stats([abs(r["pick_prob"] - r["pick_mkt"]) for r in valid],
           "ALL  |prob-mkt| gap")
    _stats([abs(r["pick_prob"] - r["pick_mkt"]) for r in valid if r["elo_tag"] == "BOTH"],
           "A    |prob-mkt| gap")
    _stats([abs(r["pick_prob"] - r["pick_mkt"]) for r in valid if r["elo_tag"] == "NONE"],
           "B    |prob-mkt| gap")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — FACTOR DECOMPOSITION (selected cases)
# ─────────────────────────────────────────────────────────────────────────────

def run_decomposition(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 3 — FACTOR DECOMPOSITION  (key cases)")
    print(f"  delta = weight * (factor_prob - 0.50)  [positive = boosts that side]")
    print(SEP)

    # A1 (control, real ELO), B1 (strong-fav no-ELO), B4 (qualifier), B5 (Venus)
    show = {"A1", "B1", "B4", "B5", "B7"}

    for r in results:
        if r is None or r["idx"] not in show:
            continue

        decomp = _decompose(r["comps"])
        pa, pb = r["pa"], r["pb"]
        rank_raw = r["comps"].get("ranking")

        print(f"\n  {r['label']}")
        print(f"  {SEP2}")
        print(f"  Market A={r['mkt_a']:.1%} B={r['mkt_b']:.1%}   "
              f"Model A={r['prob_a']:.1%} B={r['prob_b']:.1%}   "
              f"Gap A={r['gap_a']:+.1%}  ELO={r['elo_tag']}")
        if rank_raw and isinstance(rank_raw, tuple):
            print(f"  Ranking prior used: A={rank_raw[0]:.3f}  B={rank_raw[1]:.3f}"
                  f"  {'[market fallback]' if r['elo_tag']=='NONE' else '[real ELO]'}")

        print(f"\n  {'Factor':<22} {'Wt':>4}  {'A raw':>7}  {'B raw':>7}  "
              f"{'A delta':>8}  {'B delta':>8}")
        print(f"  {'-'*22} {'-'*4}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*8}")
        for k, v in sorted(decomp.items(), key=lambda x: abs(x[1]["delta_a"]), reverse=True):
            note = ""
            if k == "recent_form" and abs(v["delta_a"]) > 0.015:
                note = " *** form spike"
            elif k == "tournament_exp" and abs(v["delta_a"]) > 0.015:
                note = " *** career-wins"
            print(f"  {k:<22} {v['weight']:>4.2f}  {v['a_raw']:>7.3f}  {v['b_raw']:>7.3f}  "
                  f"{v['delta_a']:>+8.4f}  {v['delta_b']:>+8.4f}{note}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — AGGREGATE FACTOR ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def run_aggregate(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 4 — AGGREGATE: which factors inflate underdogs?")
    print(f"  Underdog = the B-side player (longer odds) in cases where udog_is_b=True")
    print(SEP)

    factor_all  = {k: [] for k in WEIGHTS}
    factor_noelo = {k: [] for k in WEIGHTS}

    for r in results:
        if r is None or not r["udog_is_b"]:
            continue
        decomp = _decompose(r["comps"])
        for k, v in decomp.items():
            factor_all[k].append(v["delta_b"])
            if r["elo_tag"] == "NONE":
                factor_noelo[k].append(v["delta_b"])

    def _print_agg(factor_dict, label):
        rows = []
        for k, deltas in factor_dict.items():
            if not deltas:
                continue
            avg = sum(deltas) / len(deltas)
            mx  = max(deltas, key=abs)
            rows.append((k, avg, mx, len(deltas)))
        rows.sort(key=lambda x: x[1], reverse=True)
        print(f"\n  {label}")
        print(f"  {'Factor':<22} {'Wt':>4}  {'Avg delta':>10}  "
              f"{'Max |delta|':>12}  Interpretation")
        print(f"  {'-'*22} {'-'*4}  {'-'*10}  {'-'*12}  {'-'*26}")
        for k, avg, mx, n in rows:
            w = WEIGHTS.get(k, 0)
            interp = ("*** INFLATES underdog" if avg > 0.015
                      else ("  * mild boost"   if avg > 0.005
                            else ("  * deflates" if avg < -0.005
                                  else "    near-neutral")))
            print(f"  {k:<22} {w:>4.2f}  {avg:>+10.4f}  {abs(mx):>12.4f}  {interp}")

    _print_agg(factor_all,   "ALL underdog cases (Groups A+B)")
    _print_agg(factor_noelo, "GROUP B only: no-ELO underdog cases")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — ELO FALLBACK SPECIFIC AUDIT
# ─────────────────────────────────────────────────────────────────────────────

def run_elo_fallback_audit(cases: list) -> None:
    """
    For each case: run calculate_probability() twice —
      with market_odds:    ELO fallback can fire
      without market_odds: ELO fallback cannot fire (old 0.50/0.50 behavior)
    Shows the delta and whether the gap vs market improved.
    """
    print(f"\n{SEP}")
    print("  SECTION 5 — ELO FALLBACK AUDIT  (with odds vs without odds)")
    print(f"  Fallback condition: both players have matches_played==0 in ELO store")
    print(f"  With odds    : uses vig-stripped market implied as ranking prior")
    print(f"  Without odds : old behavior: 1500/1500 -> 0.50/0.50 ELO prior")
    print(SEP)
    print()
    print(f"  {'Idx':<4}  {'NoOdds A':>9}  {'WithOdds A':>11}  {'Delta':>7}  "
          f"{'MktA':>6}  {'GapBefore':>10}  {'GapAfter':>9}  ELO  Fired?")
    print(f"  {'-'*4}  {'-'*9}  {'-'*11}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*9}  {'-'*4}  {'-'*6}")

    for label, pa, pb, oa, ob, surf, h2h_a, h2h_b, _ in cases:
        idx = label.split()[0]
        try:
            p_no_a, _, _  = calculate_probability(pa, pb, surf, h2h_a, h2h_b)
            p_wi_a, _, _  = calculate_probability(pa, pb, surf, h2h_a, h2h_b,
                                                   market_odds_a=oa, market_odds_b=ob)
        except Exception as e:
            print(f"  {idx:<4}  ERROR: {e}")
            continue

        mkt_a, _ = _mkt_prob(oa, ob)
        delta      = p_wi_a - p_no_a
        gap_before = p_no_a - mkt_a
        gap_after  = p_wi_a - mkt_a

        mp_a = _elo_history(pa.full_name or pa.short_name)
        mp_b = _elo_history(pb.full_name or pb.short_name)
        elo_tag = ("BOTH" if mp_a > 0 and mp_b > 0
                   else ("ONE" if mp_a > 0 or mp_b > 0
                         else "NONE"))

        # Fallback fires when NONE and odds were provided
        fired = "YES" if elo_tag == "NONE" else "no"

        # Improvement signal
        improved = (abs(gap_after) < abs(gap_before) - 0.005)
        mark = " ok" if improved else ("  !" if abs(gap_after) > 0.10 else "")

        print(f"  {idx:<4}  {p_no_a:>9.1%}  {p_wi_a:>11.1%}  {delta:>+7.1%}  "
              f"{mkt_a:>6.1%}  {gap_before:>+10.1%}  {gap_after:>+9.1%}  "
              f"{elo_tag:<4}  {fired:<6}{mark}")

    print()
    print("  Legend:")
    print("  Delta     = prob_A change caused by ELO fallback switching to market prior")
    print("  GapBefore = model - market  without fallback (0.50/0.50 ELO prior)")
    print("  GapAfter  = model - market  with    fallback (market implied ELO prior)")
    print("  Fired?    = YES means the fallback condition triggered (both mp==0)")
    print("  ok        = gap vs market improved after fallback")
    print("  !         = gap > 10% — still elevated")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def run_verdict(results: list) -> None:
    print(f"\n{SEP}")
    print("  SECTION 6 — VERDICT  (4 diagnostic questions)")
    print(SEP)
    print()

    valid  = [r for r in results if r]
    grp_a  = [r for r in valid if r["elo_tag"] == "BOTH"]
    grp_b  = [r for r in valid if r["elo_tag"] == "NONE"]
    grp_c  = [r for r in valid if r["elo_tag"] == "ONE"]

    def _avg(lst):  return sum(lst) / len(lst) if lst else 0
    def _max(lst):  return max(lst) if lst else 0

    all_edges     = [r["pick_edge"] for r in valid]
    all_gaps      = [abs(r["pick_prob"] - r["pick_mkt"]) for r in valid]
    grpb_edges    = [r["pick_edge"] for r in grp_b]
    grpb_gaps     = [abs(r["pick_prob"] - r["pick_mkt"]) for r in grp_b]
    grpa_gaps     = [abs(r["pick_prob"] - r["pick_mkt"]) for r in grp_a]

    print(f"  1. Is underdog concentration structural?")
    # Measure: for Group B (no-ELO, fallback fires), does the model still
    # push underdogs far above their market probability?
    udog_b_rows = [r for r in grp_b if r["udog_is_b"]]
    if udog_b_rows:
        avg_udog_prob = _avg([r["pick_prob"] for r in udog_b_rows])
        avg_udog_mkt  = _avg([r["pick_mkt"]  for r in udog_b_rows])
        avg_overest   = avg_udog_prob - avg_udog_mkt
        print(f"     Group B underdog picks: {len(udog_b_rows)}")
        print(f"     Avg model prob (underdog):   {avg_udog_prob:.1%}")
        print(f"     Avg market prob (underdog):  {avg_udog_mkt:.1%}")
        print(f"     Avg model overestimation:   {avg_overest:+.1%}")
        if avg_overest > 0.06:
            print(f"     [!!] Structural underdog inflation: +{avg_overest:.1%} above market.")
        elif avg_overest > 0.03:
            print(f"     [!]  Moderate underdog inflation: +{avg_overest:.1%} above market.")
        else:
            print(f"     [ok] Underdog overestimation within range: {avg_overest:+.1%}")
    print()

    print(f"  2. Is the extreme edge tail acceptable?")
    print(f"     ALL  — mean edge: {_avg(all_edges):.1%}   "
          f"max: {_max(all_edges):.1%}   "
          f">20%: {sum(1 for e in all_edges if e>0.20)}/{len(all_edges)}")
    print(f"     GRP A — mean edge: {_avg([r['pick_edge'] for r in grp_a]):.1%}   "
          f"max: {_max([r['pick_edge'] for r in grp_a]):.1%}  (control: real ELO)")
    print(f"     GRP B — mean edge: {_avg(grpb_edges):.1%}   "
          f"max: {_max(grpb_edges):.1%}  (no-ELO, fallback fires)")
    print(f"     GRP C — mean edge: {_avg([r['pick_edge'] for r in grp_c]):.1%}   "
          f"max: {_max([r['pick_edge'] for r in grp_c]):.1%}  (one-side ELO)")
    if _max(grpb_edges) > 0.30:
        print(f"     [!!] Group B max edge {_max(grpb_edges):.1%} — extreme tail persists after fallback")
    elif _max(grpb_edges) > 0.20:
        print(f"     [!]  Group B max edge {_max(grpb_edges):.1%} — tail elevated")
    else:
        print(f"     [ok] Group B max edge {_max(grpb_edges):.1%} — tail acceptable")
    print()

    print(f"  3. Is HIGH confidence now meaningfully separated from MEDIUM?")
    print(f"     Cannot evaluate from calculate_probability() alone.")
    print(f"     Run the full pipeline scanner to assess confidence tier distribution.")
    print()

    print(f"  4. Which factor is the main remaining source of miscalibration?")
    # Pull factor aggregates from Group B underdog cases
    factor_avgs = {}
    for r in [r for r in grp_b if r and r["udog_is_b"]]:
        decomp = _decompose(r["comps"])
        for k, v in decomp.items():
            factor_avgs.setdefault(k, []).append(v["delta_b"])
    if factor_avgs:
        ranked = sorted([(k, sum(v)/len(v)) for k, v in factor_avgs.items()],
                        key=lambda x: x[1], reverse=True)
        top = ranked[0]
        print(f"     Group B underdog deltas (positive = inflates underdog):")
        for k, avg in ranked[:5]:
            bar = "***" if avg > 0.015 else ("  *" if avg > 0.005 else "   ")
            print(f"       {bar} {k:<22} avg delta={avg:+.4f}")
        print(f"     Main remaining source: {top[0]} (avg delta={top[1]:+.4f})")
    print()

    # Final verdict
    mean_edge_b = _avg(grpb_edges)
    max_edge_b  = _max(grpb_edges)
    avg_ovest   = (avg_udog_prob - avg_udog_mkt) if udog_b_rows else 0

    print(f"  FINAL VERDICT:")
    if max_edge_b > 0.30:
        verdict = "extreme edge inflation remains"
    elif avg_ovest > 0.06:
        verdict = "model improved but still over-selects underdogs"
    elif avg_ovest > 0.03:
        verdict = "model calibration is materially improved"
    else:
        verdict = "model calibration is now materially improved"

    print(f"  \"{verdict}\"")
    print()
    print(f"  Key metrics (fresh test matrix, {len(valid)} cases):")
    print(f"    All-cases  mean pick edge:         {_avg(all_edges):.1%}  "
          f"(target 5-12%)")
    print(f"    Group A    mean pick edge (ELO OK): {_avg([r['pick_edge'] for r in grp_a]):.1%}")
    print(f"    Group B    mean pick edge (no ELO): {_avg(grpb_edges):.1%}")
    print(f"    Group B    max  pick edge:           {_max(grpb_edges):.1%}")
    print(f"    Group B    avg underdog overest:    {avg_ovest:+.1%}  "
          f"(target < +3%)")
    print(f"    Group A    avg |prob-mkt|:          {_avg(grpa_gaps):.1%}")
    print(f"    Group B    avg |prob-mkt|:          {_avg(grpb_gaps):.1%}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{SEP}")
    print("  FRESH MODEL AUDIT  —  post-fix evaluation (no predictions.json)")
    print(f"  Fixes evaluated: prob shrink | ranking prior | log-scale tournament_exp |")
    print(f"                   recent_form shrink | missing-ELO market fallback")
    print(f"  Groups: A=both ELO (control)  B=no ELO (treatment)  C=mixed ELO")
    print(SEP)

    cases   = build_test_cases()
    results = run_output_table(cases)
    run_edge_distribution(results)
    run_decomposition(results)
    run_aggregate(results)
    run_elo_fallback_audit(cases)
    run_verdict(results)

    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
