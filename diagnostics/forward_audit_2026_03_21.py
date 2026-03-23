"""
diagnostics/forward_audit_2026_03_21.py
========================================
FRESH FORWARD AUDIT — 2026-03-21
Post-fix full model calibration assessment. 35-case synthetic test matrix.
No predictions.json. No HTTP calls.

WTA note: Static profiles are tagged data_source="tennis_abstract_dynamic"
to simulate a successful live fetch. The production WTA data gate blocks
any non-dynamic source; this audit bypasses that gate intentionally to
assess model calibration directly.

Fixes covered:
  1. HIGH confidence gate (edge>=12%, gap>=8pp)
  2. DATA_AVAILABILITY_CAP = 0.55
  3. SHRINK_ALPHA = 0.70  (market shrink before fair-odds)
  4. Ranking-anchored ELO fallback -> market-implied when both mp==0
  5. log1p tournament_exp compression
  6. recent_form shrink (0.70*raw + 0.30*0.50)
  7. Logit stretch gamma=1.35 (pipeline.py, post-shrink, pre-fair-odds)
  8. MARKET_WEIGHT = 0.15 blend in calculate_probability

Run from parent directory of tennis_model/:
    python tennis_model/diagnostics/forward_audit_2026_03_21.py
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

from tennis_model.models   import PlayerProfile
from tennis_model.profiles import WTA_PROFILES, STATIC_PROFILES
from tennis_model.model    import calculate_probability, fair_odds, edge_pct
from tennis_model.probability_adjustments import shrink_toward_market
from tennis_model.validation import validate_match
from tennis_model.confidence import compute_confidence
from tennis_model.ev        import compute_ev, EVResult

SEP  = "=" * 78
SEP2 = "-" * 78


# ─────────────────────────────────────────────────────────────────────────────
# SERVE STATS STUBS
# source keys trigger real-stats path; career n>=8 avoids small-sample penalty
# ─────────────────────────────────────────────────────────────────────────────

_WTA_SS = {
    "source": "tennis_abstract_wta",
    "career": {"n": 25, "first_serve_in": 0.62, "first_serve_won": 0.66,
               "second_serve_won": 0.51},
    "clay":   {"n": 12, "first_serve_in": 0.61, "first_serve_won": 0.63,
               "second_serve_won": 0.49},
    "grass":  {"n":  8, "first_serve_in": 0.63, "first_serve_won": 0.67,
               "second_serve_won": 0.51},
}

_ATP_SS = {
    "source": "tennis_abstract",
    "career": {"n": 40, "first_serve_in": 0.64, "first_serve_won": 0.73,
               "second_serve_won": 0.52},
}


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
    p.data_source = "tennis_abstract_dynamic"
    p.serve_stats = _WTA_SS
    return p


def _atp(pid: str) -> PlayerProfile:
    d = STATIC_PROFILES[pid.upper()]
    p = PlayerProfile(short_name=d.get("full_name", pid))
    for k, v in d.items():
        if hasattr(p, k):
            setattr(p, k, v)
    p.full_name   = d.get("full_name", pid)
    p.data_source = "static_curated"
    p.serve_stats = _ATP_SS
    return p


def _synth(name, full, rank, age,
           hW, hL, cW, cL, gW, gL,
           carW, carL, ytdW, ytdL, form) -> PlayerProfile:
    p = PlayerProfile(
        short_name=name, full_name=full, ranking=rank, age=age,
        hard_wins=hW, hard_losses=hL, clay_wins=cW, clay_losses=cL,
        grass_wins=gW, grass_losses=gL,
        career_wins=carW, career_losses=carL,
        ytd_wins=ytdW, ytd_losses=ytdL,
        recent_form=form, data_source="static_curated",
    )
    p.serve_stats = _ATP_SS
    return p


def _qualifier(name="Q", tour="wta") -> PlayerProfile:
    p = PlayerProfile(
        short_name=name, full_name=name, ranking=9999,
        data_source="tennis_abstract_dynamic" if tour == "wta" else "static_curated",
    )
    p.serve_stats = _WTA_SS if tour == "wta" else _ATP_SS
    return p


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE REPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def _logit_stretch(p: float, g: float = 1.35) -> float:
    p = max(0.01, min(0.99, p))
    return 1.0 / (1.0 + math.exp(-math.log(p / (1.0 - p)) * g))


def _mkt(oa: float, ob: float):
    ra, rb = 1 / oa, 1 / ob
    t = ra + rb
    return ra / t, rb / t


# ─────────────────────────────────────────────────────────────────────────────
# TEST MATRIX (35 cases)
# (label, pa, pb, oa, ob, surface, h2h_a, h2h_b, tour, is_qualifier)
# ─────────────────────────────────────────────────────────────────────────────

def build_matrix():
    rows = []

    # ── ATP static profiles ───────────────────────────────────────────────────
    walton     = _atp("W09E")   # rank  85, 26yo, Hard specialist
    rodesch    = _atp("R0E0")   # rank 137, 24yo, Clay+10W streak
    maestrelli = _atp("M0TA")   # rank 162, 23yo
    watanuki   = _atp("W0AK")   # rank 191, 26yo

    # ── ATP synthetic top players ─────────────────────────────────────────────
    W = ["W"] * 10
    sinner = _synth("J. Sinner", "Jannik Sinner", 1, 23,
                    180, 50, 95, 28, 28, 9, 303, 87, 18, 2, W)
    alcaraz = _synth("C. Alcaraz", "Carlos Alcaraz", 3, 22,
                     142, 46, 88, 22, 40, 9, 270, 77, 14, 4,
                     ["W","L","W","W","W","L","W","W","W","W"])
    djokovic = _synth("N. Djokovic", "Novak Djokovic", 2, 38,
                      545, 90, 315, 48, 165, 18, 1025, 156, 8, 3,
                      ["W","W","L","W","W","L","W","W","L","W"])
    fritz = _synth("T. Fritz", "Taylor Fritz", 5, 27,
                   172, 83, 44, 42, 22, 18, 238, 143, 12, 6,
                   ["W","W","L","W","W","W","L","W","L","W"])
    dimitrov = _synth("G. Dimitrov", "Grigor Dimitrov", 20, 33,
                      245, 145, 110, 76, 60, 36, 415, 257, 8, 7,
                      ["W","L","W","W","L","W","L","W","W","L"])
    berrettini = _synth("M. Berrettini", "Matteo Berrettini", 30, 29,
                        140, 76, 92, 50, 56, 21, 288, 147, 10, 5,
                        ["W","W","W","L","W","L","W","W","L","W"])

    # ATP cases
    rows.append(("ATP-01", walton,     maestrelli,  1.55, 2.55, "Hard", 1, 2, "atp", False))
    rows.append(("ATP-02", rodesch,    watanuki,    1.38, 3.00, "Hard", 0, 0, "atp", False))
    rows.append(("ATP-03", rodesch,    walton,      1.52, 2.55, "Clay", 0, 0, "atp", False))
    rows.append(("ATP-04", sinner,     berrettini,  1.25, 4.20, "Hard", 3, 1, "atp", False))
    rows.append(("ATP-05", alcaraz,    dimitrov,    1.45, 2.80, "Hard", 2, 1, "atp", False))
    rows.append(("ATP-06", djokovic,   watanuki,    1.18, 5.50, "Hard", 0, 0, "atp", False))
    rows.append(("ATP-07", maestrelli, watanuki,    1.75, 2.10, "Clay", 0, 0, "atp", False))
    rows.append(("ATP-08", fritz,      walton,      1.42, 2.95, "Hard", 0, 0, "atp", False))

    # ── WTA profiles ──────────────────────────────────────────────────────────
    sakkari      = _wta("maria sakkari")
    siniakova    = _wta("katerina siniakova")
    baptiste     = _wta("hailey baptiste")
    cristian     = _wta("jaqueline cristian")
    boulter      = _wta("katie boulter")
    linette      = _wta("magda linette")
    venus        = _wta("venus williams")
    kenin        = _wta("sofia kenin")
    siegemund    = _wta("laura siegemund")
    haddad_maia  = _wta("beatriz haddad maia")
    frech        = _wta("magdalena frech")
    sonmez       = _wta("zeynep sonmez")
    putintseva   = _wta("yulia putintseva")
    blinkova     = _wta("anna blinkova")
    stearns      = _wta("peyton stearns")
    krueger      = _wta("ashlyn krueger")
    osorio       = _wta("camila osorio")
    brady        = _wta("jennifer brady")
    ruzic        = _wta("antonia ruzic")
    selekhm      = _wta("oksana selekhmeteva")
    gracheva     = _wta("varvara gracheva")
    jones        = _wta("francesca jones")
    bouzas       = _wta("jessica bouzas maneiro")
    galfi        = _wta("dalma galfi")
    stephens     = _wta("sloane stephens")
    tmaria       = _wta("tatjana maria")
    cirstea      = _wta("sorana cirstea")
    zhang        = _wta("shuai zhang")
    tjen         = _wta("janice tjen")
    waltert      = _wta("simona waltert")
    sierra       = _wta("solana sierra")
    arango       = _wta("emiliana arango")

    # WTA cases
    rows.append(("WTA-01", sakkari,     galfi,       1.40, 3.00, "Hard",  2, 3, "wta", False))
    rows.append(("WTA-02", siniakova,   blinkova,    1.45, 2.80, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-03", baptiste,    tmaria,      1.22, 4.50, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-04", cristian,    stephens,    1.28, 3.80, "Hard",  1, 0, "wta", False))
    rows.append(("WTA-05", cirstea,     arango,      1.38, 3.10, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-06", boulter,     galfi,       1.62, 2.35, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-07", linette,     venus,       1.08,10.00, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-08", kenin,       siniakova,   1.90, 1.92, "Hard",  1, 1, "wta", False))
    rows.append(("WTA-09", siegemund,   haddad_maia, 1.72, 2.12, "Clay",  0, 0, "wta", False))
    rows.append(("WTA-10", frech,       sonmez,      1.52, 2.55, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-11", putintseva,  blinkova,    1.62, 2.35, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-12", stearns,     krueger,     1.85, 1.98, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-13", osorio,      brady,       1.55, 2.50, "Clay",  0, 0, "wta", False))
    rows.append(("WTA-14", brady,       waltert,     1.90, 1.92, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-15", ruzic,       selekhm,     1.72, 2.18, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-16", gracheva,    jones,       1.55, 2.50, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-17", bouzas,      kenin,       1.82, 2.05, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-18", sakkari,     brady,       1.38, 3.00, "Grass", 0, 0, "wta", False))
    rows.append(("WTA-19", sierra,      zhang,       1.72, 2.15, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-20", tjen,        stephens,    1.50, 2.65, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-21", haddad_maia, jones,       1.32, 3.55, "Hard",  0, 0, "wta", False))
    rows.append(("WTA-22", linette,     frech,       1.75, 2.10, "Hard",  0, 0, "wta", False))

    # Qualifier cases
    qa = _qualifier("Qualifier", "atp")
    qw = _qualifier("Qualifier", "wta")
    rows.append(("Q-01", walton,   qa,          1.25, 4.50, "Hard", 0, 0, "atp", True))
    rows.append(("Q-02", sakkari,  qw,          1.15, 6.00, "Hard", 0, 0, "wta", True))
    rows.append(("Q-03", qa,       maestrelli,  3.80, 1.32, "Hard", 0, 0, "atp", True))
    rows.append(("Q-04", stephens, qw,          1.38, 3.10, "Hard", 0, 0, "wta", True))
    rows.append(("Q-05", galfi,    qw,          1.45, 2.90, "Hard", 0, 0, "wta", True))

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# RUN ALL CASES
# ─────────────────────────────────────────────────────────────────────────────

def run_all(matrix):
    results = []
    for row in matrix:
        label, pa, pb, oa, ob, surf, h2a, h2b, tour, is_q = row
        try:
            prob_a, prob_b, _ = calculate_probability(
                pa, pb, surf, h2a, h2b,
                market_odds_a=oa, market_odds_b=ob,
            )

            # Shrink toward market (SHRINK_ALPHA=0.70)
            sa = shrink_toward_market(prob_a, oa)
            sb = shrink_toward_market(prob_b, ob)

            # Logit stretch γ=1.35, then renormalise
            la, lb = _logit_stretch(sa), _logit_stretch(sb)
            la, lb = la / (la + lb), lb / (la + lb)

            fo_a_ = fair_odds(la)
            fo_b_ = fair_odds(lb)
            ea    = edge_pct(oa, fo_a_) / 100.0
            eb    = edge_pct(ob, fo_b_) / 100.0

            val  = validate_match(pa, pb, surf, oa, ob, odds_source="audit")
            conf = compute_confidence(
                pa, pb, surf, val,
                edge=max(ea, eb),
                model_prob=max(prob_a, prob_b),
                days_inactive=-1,
            )

            gate = None
            if tour == "wta":
                bad = [p.short_name for p in [pa, pb]
                       if p.data_source != "tennis_abstract_dynamic"]
                if bad:
                    gate = f"WTA DATA GATE: {', '.join(bad)}"

            if gate:
                ev_a = ev_b = EVResult(0.0, False, gate)
            else:
                ev_a = compute_ev(oa, fo_a_, val, conf, -1, tour)
                ev_b = compute_ev(ob, fo_b_, val, conf, -1, tour)

            mkt_a, mkt_b = _mkt(oa, ob)
            has_pick = (ev_a.is_value or ev_b.is_value) and not gate

            results.append(dict(
                label=label, pa=pa, pb=pb, oa=oa, ob=ob,
                surf=surf, tour=tour, is_q=is_q,
                prob_a=prob_a, prob_b=prob_b,
                adj_a=la, adj_b=lb,
                fo_a=fo_a_, fo_b=fo_b_,
                ea=ea, eb=eb,
                mkt_a=mkt_a, mkt_b=mkt_b,
                conf=conf, val_ok=val.passed,
                gate=gate, ev_a=ev_a, ev_b=ev_b,
                pick=has_pick, fav_is_a=(oa <= ob),
            ))
        except Exception as e:
            print(f"  [ERROR] {label}: {e}")
            results.append(None)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# STATS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _pctile(s, pct):
    n = len(s)
    if n == 0:
        return 0.0
    k = (n - 1) * pct / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def _stats_block(edges, label, indent="  "):
    if not edges:
        print(f"{indent}{label}: no data"); return
    s = sorted(edges)
    n = len(s)
    mean = sum(s) / n
    print(f"{indent}{label} (n={n})")
    print(f"{indent}  mean={mean*100:+.1f}%  median={_pctile(s,50)*100:+.1f}%  "
          f"p75={_pctile(s,75)*100:.1f}%  p90={_pctile(s,90)*100:.1f}%  "
          f"p95={_pctile(s,95)*100:.1f}%  p99={_pctile(s,99)*100:.1f}%")
    print(f"{indent}  min={s[0]*100:+.1f}%  max={s[-1]*100:+.1f}%")
    thr = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    parts = "  ".join(f">{int(t*100)}%:{sum(1 for x in s if x > t):>2}/{n}" for t in thr)
    print(f"{indent}  {parts}")


# ─────────────────────────────────────────────────────────────────────────────
# EDGE SAMPLES (both sides of each match = 2 * n_cases samples)
# ─────────────────────────────────────────────────────────────────────────────

def make_samples(results):
    samples = []
    for r in results:
        if r is None:
            continue
        for side, edge, adj, mkt, odds in [
            ("A", r["ea"], r["adj_a"], r["mkt_a"], r["oa"]),
            ("B", r["eb"], r["adj_b"], r["mkt_b"], r["ob"]),
        ]:
            is_fav = (r["fav_is_a"] and side == "A") or (not r["fav_is_a"] and side == "B")
            is_val = r["ev_a"].is_value if side == "A" else r["ev_b"].is_value
            samples.append(dict(
                label=f"{r['label']}-{side}",
                edge=edge,
                adj=adj,
                mkt=mkt,
                gap=adj - mkt,
                tour=r["tour"],
                conf=r["conf"],
                is_fav=is_fav,
                is_q=r["is_q"],
                is_value=is_val,
            ))
    return samples


# ─────────────────────────────────────────────────────────────────────────────
# REPORT SECTIONS
# ─────────────────────────────────────────────────────────────────────────────

def section1_summary(results, matrix):
    valid  = [r for r in results if r]
    errors = [r for r in results if r is None]
    atp_r  = [r for r in valid if r["tour"] == "atp"]
    wta_r  = [r for r in valid if r["tour"] == "wta"]
    q_r    = [r for r in valid if r["is_q"]]
    picks  = [r for r in valid if r["pick"]]
    gated  = [r for r in valid if r["gate"]]
    val_fail = [r for r in valid if not r["val_ok"]]

    conf_counts = {}
    for r in valid:
        conf_counts[r["conf"]] = conf_counts.get(r["conf"], 0) + 1

    print(f"\n{SEP}")
    print("  SECTION 1 — FRESH SAMPLE SUMMARY")
    print(f"  35-case matrix: 8 ATP, 22 WTA, 5 qualifier")
    print(f"  WTA: data_source promoted to tennis_abstract_dynamic (simulates live fetch)")
    print(SEP)
    print(f"  Matches processed:     {len(matrix)}")
    print(f"  Computed OK:           {len(valid)}    Errors: {len(errors)}")
    print()
    print(f"  ATP:                   {len(atp_r)}")
    print(f"  WTA:                   {len(wta_r)}")
    print(f"  Qualifier cases:       {len(q_r)}")
    print()
    print(f"  Picks generated:       {len(picks)}  ({len(picks)/len(valid)*100:.0f}% of valid cases)")
    print(f"  Blocked (all reasons): {len(valid)-len(picks)}")
    print(f"    WTA data gate:       0  (bypassed — dynamic source set)")
    print(f"    Validation failed:   {len(val_fail)}")
    print(f"    EV filter (no pick): {len(valid)-len(picks)-len(val_fail)}")
    print()
    for t in ["VERY HIGH", "HIGH", "MEDIUM", "LOW"]:
        n = conf_counts.get(t, 0)
        bar = "#" * n
        print(f"  {t:<10}  {n:>2}  {bar}")
    print()
    # Full case table
    print(f"  {'Case':<9} {'Surf':<6} {'Tour':<4} {'OddsA':>6} {'OddsB':>6}"
          f" {'ModelA':>7} {'MktA':>6} {'GapA':>6}"
          f" {'Ea':>7} {'Eb':>7} {'Conf':<10} Status")
    print(f"  {SEP2}")
    for r in valid:
        gap_a = r["adj_a"] - r["mkt_a"]
        status = "PICK" if r["pick"] else ("GATE" if r["gate"] else
                 ("VALFAIL" if not r["val_ok"] else "nobet"))
        # show ev reason for no-bet
        if not r["pick"] and not r["gate"] and r["val_ok"]:
            reason = r["ev_a"].filter_reason or r["ev_b"].filter_reason or "?"
            reason = reason[:18]
        else:
            reason = ""
        print(f"  {r['label']:<9} {r['surf']:<6} {r['tour'].upper():<4}"
              f" {r['oa']:>6.2f} {r['ob']:>6.2f}"
              f" {r['adj_a']:>7.1%} {r['mkt_a']:>6.1%} {gap_a:>+6.1%}"
              f" {r['ea']:>+7.1%} {r['eb']:>+7.1%} {r['conf']:<10} {status}"
              + (f"  [{reason}]" if reason else ""))


def section2_edge_distribution(samples):
    print(f"\n{SEP}")
    print("  SECTION 2 — EDGE DISTRIBUTION")
    print(f"  {len(samples)} edge samples (both sides of all matches)")
    print(f"  Edge = (market_odds / fair_odds) - 1  [after shrink + logit stretch]")
    print(SEP)

    all_e = [s["edge"] for s in samples]
    atp_e = [s["edge"] for s in samples if s["tour"] == "atp"]
    wta_e = [s["edge"] for s in samples if s["tour"] == "wta"]
    fav_e = [s["edge"] for s in samples if s["is_fav"]]
    udog_e = [s["edge"] for s in samples if not s["is_fav"]]
    q_e   = [s["edge"] for s in samples if s["is_q"]]
    nonq_e = [s["edge"] for s in samples if not s["is_q"]]

    _stats_block(all_e,   "ALL edges")
    print()
    _stats_block(atp_e,   "ATP only")
    _stats_block(wta_e,   "WTA only")
    print()
    _stats_block(fav_e,   "FAVORITES (lower market odds)")
    _stats_block(udog_e,  "UNDERDOGS (higher market odds)")
    print()
    _stats_block(q_e,     "QUALIFIER cases")
    _stats_block(nonq_e,  "Non-qualifier cases")

    print()
    print(f"  Confidence tier splits (best-edge per match):")
    for tier in ["VERY HIGH", "HIGH", "MEDIUM", "LOW"]:
        tier_e = [s["edge"] for s in samples if s["conf"] == tier]
        if tier_e:
            s2 = sorted(tier_e)
            print(f"  {tier:<10}  n={len(tier_e):>2}  mean={sum(tier_e)/len(tier_e)*100:+.1f}%  "
                  f"max={max(tier_e)*100:.1f}%  >10%:{sum(1 for x in tier_e if x>0.10)}")


def section3_calibration(samples, results):
    print(f"\n{SEP}")
    print("  SECTION 3 — CALIBRATION")
    print(f"  gap = adj_model_prob - vig_stripped_market_prob")
    print(f"  Positive gap = model overestimates that side vs market")
    print(SEP)

    fav  = [s for s in samples if s["is_fav"]]
    udog = [s for s in samples if not s["is_fav"]]

    def _avg(lst): return sum(lst) / len(lst) if lst else 0.0

    fav_gaps  = [s["gap"] for s in fav]
    udog_gaps = [s["gap"] for s in udog]
    atp_fav_gaps  = [s["gap"] for s in fav  if s["tour"] == "atp"]
    atp_udog_gaps = [s["gap"] for s in udog if s["tour"] == "atp"]
    wta_fav_gaps  = [s["gap"] for s in fav  if s["tour"] == "wta"]
    wta_udog_gaps = [s["gap"] for s in udog if s["tour"] == "wta"]

    print(f"\n  Aggregate calibration (n=35 matches = 70 samples):")
    print(f"  {'Segment':<28}  {'Avg gap':>9}  Interpretation")
    print(f"  {SEP2}")
    rows = [
        ("All favorites",         _avg(fav_gaps)),
        ("All underdogs",         _avg(udog_gaps)),
        ("ATP favorites",         _avg(atp_fav_gaps)),
        ("ATP underdogs",         _avg(atp_udog_gaps)),
        ("WTA favorites",         _avg(wta_fav_gaps)),
        ("WTA underdogs",         _avg(wta_udog_gaps)),
    ]
    for seg, avg in rows:
        if avg > 0.06:   note = "[!!] overestimates"
        elif avg > 0.03: note = "[! ] slight over"
        elif avg < -0.06: note = "[!!] underestimates"
        elif avg < -0.03: note = "[! ] slight under"
        else:             note = "[ok] well calibrated"
        print(f"  {seg:<28}  {avg*100:>+8.1f}%  {note}")

    # Top 10 positive gaps (model overestimates)
    all_s = sorted(samples, key=lambda s: s["gap"], reverse=True)
    print(f"\n  Top 10 LARGEST POSITIVE GAPS (model > market):")
    print(f"  {'Sample':<14}  {'ModelProb':>9}  {'MktProb':>8}  {'Gap':>7}  {'Edge':>7}  {'Conf'}")
    for s in all_s[:10]:
        flag = " !!" if s["gap"] > 0.12 else (" !" if s["gap"] > 0.06 else "")
        print(f"  {s['label']:<14}  {s['adj']:>9.1%}  {s['mkt']:>8.1%}  "
              f"{s['gap']:>+7.1%}  {s['edge']:>+7.1%}  {s['conf']}{flag}")

    # Top 10 negative gaps (model underestimates)
    print(f"\n  Top 10 LARGEST NEGATIVE GAPS (model < market):")
    print(f"  {'Sample':<14}  {'ModelProb':>9}  {'MktProb':>8}  {'Gap':>7}  {'Edge':>7}  {'Conf'}")
    for s in sorted(samples, key=lambda s: s["gap"])[:10]:
        flag = " !!" if s["gap"] < -0.12 else (" !" if s["gap"] < -0.06 else "")
        print(f"  {s['label']:<14}  {s['adj']:>9.1%}  {s['mkt']:>8.1%}  "
              f"{s['gap']:>+7.1%}  {s['edge']:>+7.1%}  {s['conf']}{flag}")

    # Top 10 largest edges
    print(f"\n  Top 10 LARGEST EDGES:")
    print(f"  {'Sample':<14}  {'Edge':>7}  {'ModelProb':>9}  {'MktProb':>8}  {'Odds':>6}  {'Conf'}  Pick?")
    for s in sorted(samples, key=lambda s: s["edge"], reverse=True)[:10]:
        flag = " !!" if s["edge"] > 0.30 else (" !" if s["edge"] > 0.20 else "")
        val_mark = "YES" if s["is_value"] else "no"
        print(f"  {s['label']:<14}  {s['edge']:>+7.1%}  {s['adj']:>9.1%}  {s['mkt']:>8.1%}  "
              f"{1/s['mkt']:>6.2f}  {s['conf']:<10}  {val_mark}{flag}")


def section4_diagnostics(samples, results):
    print(f"\n{SEP}")
    print("  SECTION 4 — DIAGNOSTICS")
    print(SEP)

    valid  = [r for r in results if r]
    atp_r  = [r for r in valid if r["tour"] == "atp"]
    wta_r  = [r for r in valid if r["tour"] == "wta"]

    def _avg(lst): return sum(lst) / len(lst) if lst else 0.0

    # Q1: Underdog concentration structural?
    udog_samps = [s for s in samples if not s["is_fav"]]
    udog_gaps  = [s["gap"] for s in udog_samps]
    avg_udog_gap = _avg(udog_gaps)
    n_udog_pos = sum(1 for g in udog_gaps if g > 0.04)
    print(f"\n  Q1: Is underdog concentration still structural?")
    print(f"    Underdog samples: {len(udog_samps)}")
    print(f"    Avg gap (model - market):  {avg_udog_gap*100:+.1f}%")
    print(f"    Samples with gap > +4pp:   {n_udog_pos}/{len(udog_samps)}")
    if avg_udog_gap > 0.06:
        print(f"    [!!] STRUCTURAL: model still consistently overestimates underdogs")
    elif avg_udog_gap > 0.03:
        print(f"    [! ] MODERATE: mild residual underdog overestimation")
    else:
        print(f"    [ok] Not structural: avg underdog gap within +/-3pp of market")

    # Q2: Edge tail acceptable?
    all_e = sorted([s["edge"] for s in samples])
    p95   = _pctile(all_e, 95)
    p99   = _pctile(all_e, 99)
    max_e = max(all_e)
    over30 = sum(1 for e in all_e if e > 0.30)
    over20 = sum(1 for e in all_e if e > 0.20)
    print(f"\n  Q2: Is the edge tail now acceptable?")
    print(f"    p95={p95*100:.1f}%  p99={p99*100:.1f}%  max={max_e*100:.1f}%")
    print(f"    Samples >20%: {over20}  >30%: {over30}")
    if max_e > 0.50:
        print(f"    [!!] EXTREME: max edge {max_e*100:.1f}% — likely miscalibration")
    elif max_e > 0.30:
        print(f"    [! ] ELEVATED: max edge {max_e*100:.1f}% — tail still high")
    elif p95 > 0.20:
        print(f"    [! ] p95 = {p95*100:.1f}% — 5% of samples exceed 20%")
    else:
        print(f"    [ok] Tail acceptable: p95 = {p95*100:.1f}%  max = {max_e*100:.1f}%")

    # Q3: Logit stretch effect on strong favorites
    fav_e = sorted([s["edge"] for s in samples if s["is_fav"]])
    udog_e = sorted([s["edge"] for s in samples if not s["is_fav"]])
    print(f"\n  Q3: Did logit stretch reduce probability compression?")
    print(f"    Favorites  — mean edge: {_avg(fav_e)*100:+.1f}%  max: {max(fav_e)*100:.1f}%")
    print(f"    Underdogs  — mean edge: {_avg(udog_e)*100:+.1f}%  max: {max(udog_e)*100:.1f}%")
    # Look at strong favorites (odds < 1.40)
    sf = [s for s in samples if s["is_fav"] and s.get("mkt", 0) > 0.65]
    if sf:
        sf_gaps = [s["gap"] for s in sf]
        print(f"    Strong favs (implied >65%): n={len(sf)}  avg gap={_avg(sf_gaps)*100:+.1f}%  "
              f"[positive = model agrees market is right]")
    # Logit stretch should give positive edge to favorites more often
    n_fav_pos = sum(1 for e in fav_e if e > 0)
    print(f"    Favorites with positive edge: {n_fav_pos}/{len(fav_e)}")
    if n_fav_pos < len(fav_e) * 0.40:
        print(f"    [! ] Compression still evident: <40% of favorites have positive edge")
    else:
        print(f"    [ok] Stretch working: {n_fav_pos}/{len(fav_e)} favorites have positive edge")

    # Q4: Realistic betting distribution?
    picks = [r for r in valid if r["pick"]]
    pick_rate = len(picks) / len(valid)
    conf_hi = sum(1 for r in valid if r["conf"] in ("HIGH", "VERY HIGH"))
    print(f"\n  Q4: Is this close to a realistic betting distribution?")
    print(f"    Pick rate:           {pick_rate*100:.0f}%  ({len(picks)}/{len(valid)})")
    print(f"    HIGH+ confidence:    {conf_hi}/{len(valid)}")
    print(f"    MEDIUM confidence:   {sum(1 for r in valid if r['conf'] == 'MEDIUM')}/{len(valid)}")
    print(f"    LOW confidence:      {sum(1 for r in valid if r['conf'] == 'LOW')}/{len(valid)}")
    # Realistic: ~10-25% of matches should generate picks
    if pick_rate > 0.50:
        print(f"    [!!] OVER-SELECTING: {pick_rate*100:.0f}% pick rate is unrealistically high")
    elif pick_rate > 0.30:
        print(f"    [! ] HIGH PICK RATE: {pick_rate*100:.0f}% — slightly aggressive")
    elif pick_rate < 0.05:
        print(f"    [! ] UNDER-SELECTING: {pick_rate*100:.0f}% — filters too restrictive")
    else:
        print(f"    [ok] Realistic pick rate: {pick_rate*100:.0f}%")

    # ATP vs WTA pick rates
    atp_picks = sum(1 for r in atp_r if r["pick"])
    wta_picks = sum(1 for r in wta_r if r["pick"])
    print(f"    ATP picks: {atp_picks}/{len(atp_r)}  WTA picks: {wta_picks}/{len(wta_r)}")


def section5_verdict(samples, results):
    print(f"\n{SEP}")
    print("  SECTION 5 — FINAL VERDICT")
    print(SEP)

    valid  = [r for r in results if r]
    picks  = [r for r in valid if r["pick"]]
    all_e  = [s["edge"] for s in samples]
    udog_e = [s["edge"] for s in samples if not s["is_fav"]]
    fav_e  = [s["edge"] for s in samples if s["is_fav"]]
    udog_g = [s["gap"]  for s in samples if not s["is_fav"]]

    def _avg(lst): return sum(lst) / len(lst) if lst else 0.0
    def _max(lst): return max(lst) if lst else 0.0

    avg_all      = _avg(all_e)
    avg_udog     = _avg(udog_e)
    avg_fav      = _avg(fav_e)
    avg_udog_gap = _avg(udog_g)
    max_edge     = _max(all_e)
    p95          = _pctile(sorted(all_e), 95)
    pick_rate    = len(picks) / len(valid)

    print(f"\n  Key metrics (35-match fresh matrix, {len(samples)} edge samples):")
    print(f"  {'Metric':<40}  {'Value':>10}  Target")
    print(f"  {SEP2}")
    metrics = [
        ("All-samples mean edge",               avg_all,      "5-12%"),
        ("Underdog mean edge",                  avg_udog,     "<= fav mean"),
        ("Favorite mean edge",                  avg_fav,      "5-12%"),
        ("Avg underdog model - market gap",     avg_udog_gap, "< +3pp"),
        ("Max edge (any sample)",               max_edge,     "< 30%"),
        ("p95 edge",                            p95,          "< 20%"),
        ("Pick rate",                           pick_rate,    "10-30%"),

    ]
    for name, val, target in metrics:
        print(f"  {name:<40}  {val*100:>+9.1f}%  {target}")

    print(f"\n  Confidence distribution: ", end="")
    for t in ["VERY HIGH", "HIGH", "MEDIUM", "LOW"]:
        n = sum(1 for r in valid if r["conf"] == t)
        print(f"{t}={n}  ", end="")
    print()

    # Determine verdicts
    verdict_parts = []

    if max_edge > 0.50:
        verdict_parts.append("extreme edge inflation remains (>50%)")
    elif max_edge > 0.30:
        verdict_parts.append("edge tail still elevated (>30%)")

    if avg_udog_gap > 0.06:
        verdict_parts.append("model still over-selects underdogs")
    elif avg_udog_gap > 0.03:
        verdict_parts.append("mild residual underdog bias")

    if pick_rate > 0.40:
        verdict_parts.append("pick rate unrealistically high")

    wta_r  = [r for r in valid if r["tour"] == "wta" and not r["is_q"]]
    wta_picks = [r for r in wta_r if r["pick"]]
    if len(wta_r) > 0 and len(wta_picks) / len(wta_r) > 0.40:
        verdict_parts.append("WTA pick inflation remains")

    high_conf = [r for r in valid if r["conf"] in ("HIGH", "VERY HIGH")]
    high_picks = [r for r in high_conf if r["pick"]]
    if len(high_conf) > 0 and len(high_picks) < len(high_conf) * 0.50:
        verdict_parts.append("HIGH confidence rarely translates to picks (gate working)")

    # Edge tail check with logit stretch
    if p95 < 0.20 and max_edge < 0.35 and avg_udog_gap < 0.04:
        verdict_parts.append("edge distribution is now close to acceptable")

    improved = (avg_udog_gap < 0.04 and max_edge < 0.40 and p95 < 0.22)
    still_problematic = any("inflation" in v or "over-selects" in v for v in verdict_parts)

    print(f"\n  RED FLAGS:")
    if not verdict_parts:
        print(f"    None detected — model calibration within acceptable bounds")
    for v in verdict_parts:
        print(f"    [!] {v}")

    print(f"\n  FINAL VERDICTS:")
    if not still_problematic and improved:
        print(f'  >> "current model is materially improved"')
    elif any("over-selects underdogs" in v for v in verdict_parts):
        print(f'  >> "current model still over-selects underdogs"')

    if any("WTA" in v for v in verdict_parts):
        print(f'  >> "qualifier/WTA inflation remains"')

    if "edge distribution is now close to acceptable" in verdict_parts:
        print(f'  >> "edge distribution is now close to acceptable"')

    if max_edge > 0.30:
        print(f'  >> "extreme edge inflation remains — top edge {max_edge*100:.1f}%"')

    if not verdict_parts:
        print(f'  >> "edge distribution is now close to acceptable"')
        print(f'  >> "current model is materially improved"')


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{SEP}")
    print("  FRESH FORWARD AUDIT — 2026-03-21")
    print("  Post-fix model calibration assessment (35 cases, no predictions.json)")
    print(f"  Fixes: HIGH gate | data cap | shrink | log-exp | form shrink | logit stretch")
    print(SEP)

    matrix  = build_matrix()
    results = run_all(matrix)
    samples = make_samples(results)

    section1_summary(results, matrix)
    section2_edge_distribution(samples)
    section3_calibration(samples, results)
    section4_diagnostics(samples, results)
    section5_verdict(samples, results)

    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
