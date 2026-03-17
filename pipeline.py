import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import requests

from tennis_model.profiles import STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
from tennis_model.model import calculate_probability, fair_odds, edge_pct
from tennis_model.validation import validate_match
from tennis_model.confidence import compute_confidence
from tennis_model.ev import compute_ev, EVResult
from tennis_model.formatter import (
    format_pick_card, format_factor_table, format_value_analysis,
    EDGE_ALERT_THRESHOLD, EDGE_DISPLAY_THRESHOLD, _pct,
)
from tennis_model.telegram import send_telegram, maybe_alert
import tennis_model.formatter as _fmt
import tennis_model.telegram as _tg

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ATP API ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

ATP_PLAYER_API  = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayer?playerId={pid}"
ATP_STATS_API   = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayerStats?playerId={pid}&year=0&surface=0"
ATP_RESULTS_API = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayerMatchResults?playerId={pid}&year={year}"
ATP_H2H_API     = "https://www.atptour.com/-/ajax/playerdashboard/GetH2HMatches?playerId={pid_a}&opponentId={pid_b}"

# ──────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PlayerProfile:
    short_name:       str
    full_name:        str   = ""
    atp_id:           str   = ""
    slug:             str   = ""
    ranking:          int   = 9999
    age:              int   = 0
    height_cm:        int   = 0
    plays:            str   = "Right-handed"
    turned_pro:       int   = 2000
    career_wins:      int   = 0
    career_losses:    int   = 0
    ytd_wins:         int   = 0
    ytd_losses:       int   = 0
    hard_wins:        int   = 0
    hard_losses:      int   = 0
    clay_wins:        int   = 0
    clay_losses:      int   = 0
    grass_wins:       int   = 0
    grass_losses:     int   = 0
    recent_form:      list  = field(default_factory=list)
    career_high_rank: int   = 9999
    data_source:      str   = "unknown"


@dataclass
class MatchPick:
    player_a:         PlayerProfile
    player_b:         PlayerProfile
    surface:          str   = "Hard"
    tournament:       str   = "ATP Tour"
    tournament_level: str   = "ATP 250"
    tour:             str   = "ATP"    # "ATP" or "WTA" — derived from tournament name
    prob_a:           float = 0.50
    prob_b:           float = 0.50
    fair_odds_a:      float = 2.00
    fair_odds_b:      float = 2.00
    market_odds_a:    Optional[float] = None
    market_odds_b:    Optional[float] = None
    edge_a:           Optional[float] = None
    edge_b:           Optional[float] = None
    pick_player:          str   = ""
    bookmaker:            str   = ""
    h2h_summary:          str   = "No prior meetings"
    factor_breakdown:     dict  = field(default_factory=dict)
    simulation:           dict  = field(default_factory=dict)
    confidence:           str   = "LOW"
    validation_passed:    bool  = True
    filter_reason:        str   = ""
    validation_warnings:  list  = field(default_factory=list)

# ──────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ──────────────────────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/html, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Referer":          "https://www.atptour.com/",
    "X-Requested-With": "XMLHttpRequest",
})


def _get_json(url: str, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=12)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt < retries - 1:
                time.sleep(2)
    return None


def _get_html(url: str) -> str:
    try:
        r = SESSION.get(url, timeout=12)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return ""

# ──────────────────────────────────────────────────────────────────────────────
# PLAYER ID RESOLUTION
# ──────────────────────────────────────────────────────────────────────────────

def resolve_player_id(short_name: str) -> tuple[str, str, str]:
    """Returns (full_name, slug, atp_id). Checks local map first."""
    name_lower = short_name.lower().strip()

    # 1. Local map (fastest)
    for key, val in PLAYER_ID_MAP.items():
        if key in name_lower:
            log.info(f"ID resolved from map: {val[0]} ({val[2]})")
            return val

    # 2. ATP search HTML (parse href pattern /players/slug/ID/overview)
    parts      = short_name.strip().split()
    query      = parts[-1] if len(parts) > 1 and len(parts[0]) <= 2 else short_name
    search_url = f"https://www.atptour.com/en/players?query={requests.utils.quote(query)}"
    html       = _get_html(search_url)
    m          = re.search(r'/players/([a-z0-9\-]+)/([A-Z0-9]{4})/overview', html)
    if m:
        slug = m.group(1)
        pid  = m.group(2)
        full = slug.replace("-", " ").title()
        log.info(f"ID resolved from ATP search: {full} | {pid}")
        return full, slug, pid

    log.warning(
        f"Cannot resolve ATP ID for '{short_name}'.\n"
        f"  → Add to PLAYER_ID_MAP: e.g.  \"{name_lower.split()[-1]}\": "
        f"(\"Full Name\", \"url-slug\", \"XXXX\")"
    )
    return short_name, "", ""

# ──────────────────────────────────────────────────────────────────────────────
# LIVE ATP API (top-up layer)
# ──────────────────────────────────────────────────────────────────────────────

def _top_up_from_api(pid: str, profile: PlayerProfile) -> None:
    """Try ATP AJAX APIs to fill any gaps left by static data."""

    # Player overview (ranking, age, height)
    if profile.ranking == 9999 or profile.age == 0:
        data = _get_json(ATP_PLAYER_API.format(pid=pid))
        if data:
            p = data.get("player") or data.get("data") or data
            try:
                if profile.ranking == 9999:
                    profile.ranking = int(p.get("Rank") or p.get("ranking") or 9999)
                if profile.age == 0:
                    profile.age = int(p.get("Age") or 0)
                if profile.height_cm == 0:
                    profile.height_cm = int(p.get("HeightCm") or 0)
                profile.data_source = "atp_api"
            except (TypeError, ValueError):
                pass

    # Stats (surface splits)
    if profile.hard_wins == 0 and profile.clay_wins == 0:
        data = _get_json(ATP_STATS_API.format(pid=pid))
        if data:
            stats = data.get("stats") or data.get("data") or data
            for surface, wa, la in [("hard","hard_wins","hard_losses"),
                                     ("clay","clay_wins","clay_losses"),
                                     ("grass","grass_wins","grass_losses")]:
                blk = stats.get(surface) or stats.get(surface.title()) or {}
                w = int(blk.get("wins") or blk.get("Wins") or 0)
                l = int(blk.get("losses") or blk.get("Losses") or 0)
                if w + l > 0:
                    setattr(profile, wa, w)
                    setattr(profile, la, l)

    # WTA profiles always have static recent_form — this block is skipped for them.
    # ATP players with no static form will be fetched here.
    # Match results (recent form)
    if not profile.recent_form:
        data = _get_json(ATP_RESULTS_API.format(pid=pid, year=date.today().year))
        if data:
            matches = data.get("matches") or data.get("data") or []
            form = []
            for m in matches:
                outcome = str(m.get("outcome") or m.get("result") or "").upper()
                if outcome.startswith("W"):
                    form.append("W")
                elif outcome.startswith("L"):
                    form.append("L")
            profile.recent_form = form[:10]


def _top_up_from_tennis_abstract(profile: PlayerProfile) -> None:
    """Tennis Abstract: public HTML stats page, no JS needed."""
    name_clean = (profile.full_name or profile.short_name).replace(" ", "").replace(".", "")
    html = _get_html(f"https://www.tennisabstract.com/cgi-bin/player-classic.cgi?p={name_clean}")
    if not html or len(html) < 500:
        return

    if profile.ranking == 9999:
        m = re.search(r'(?:rank(?:ing)?)[^\d]{0,20}?#?(\d{1,4})\b', html, re.I)
        if m:
            profile.ranking = int(m.group(1))

    for surface, wa, la in [("Hard","hard_wins","hard_losses"),
                             ("Clay","clay_wins","clay_losses"),
                             ("Grass","grass_wins","grass_losses")]:
        if getattr(profile, wa) == 0:
            m = re.search(rf'{surface}[^0-9]{{0,30}}(\d+)\s*[-–]\s*(\d+)', html, re.I)
            if m:
                setattr(profile, wa, int(m.group(1)))
                setattr(profile, la, int(m.group(2)))

    if not profile.recent_form:
        form = re.findall(r'\b([WL])\b', html)
        profile.recent_form = form[:10]

    if profile.ranking < 9999 or profile.hard_wins > 0:
        profile.data_source = "tennis_abstract"

# ──────────────────────────────────────────────────────────────────────────────
# PROFILE BUILDER
# ──────────────────────────────────────────────────────────────────────────────

def fetch_player_profile(short_name: str) -> PlayerProfile:
    """
    Three-layer cascade:
      1. Static curated (always reliable)
      2. ATP AJAX API  (live top-up)
      3. Tennis Abstract fallback
    """
    profile           = PlayerProfile(short_name=short_name)
    full, slug, pid   = resolve_player_id(short_name)
    profile.full_name = full or short_name
    profile.slug      = slug
    profile.atp_id    = pid

    # Layer 1: static curated (ATP)
    static_applied = False
    if pid and pid.upper() in STATIC_PROFILES:
        for k, v in STATIC_PROFILES[pid.upper()].items():
            setattr(profile, k, v)
        profile.data_source = "static_curated"
        static_applied = True
        log.info(f"Static data applied for {profile.full_name}")

    # Layer 1b: WTA static profiles (lookup by name)
    if not static_applied:
        name_lower = short_name.lower().strip()
        for key, val in WTA_PROFILES.items():
            if key in name_lower or name_lower.split(".")[-1].strip() in key:
                for k, v in val.items():
                    setattr(profile, k, v)
                profile.data_source = "wta_static"
                static_applied = True
                log.info(f"WTA static data applied for {profile.full_name}")
                break

    # Layer 2: live ATP API (fill any gaps)
    if pid:
        _top_up_from_api(pid, profile)

    # Layer 3: Tennis Abstract fallback
    if profile.ranking == 9999 or (profile.hard_wins == 0 and profile.clay_wins == 0):
        _top_up_from_tennis_abstract(profile)

    log.info(
        f"✓ {profile.full_name}: Rank #{profile.ranking} | "
        f"YTD {profile.ytd_wins}-{profile.ytd_losses} | "
        f"Hard {profile.hard_wins}-{profile.hard_losses} "
        f"({_pct(profile.hard_wins, profile.hard_losses)}%) | "
        f"Form {''.join(profile.recent_form[:10]) or 'N/A'} | "
        f"Source: {profile.data_source}"
    )
    return profile

# ──────────────────────────────────────────────────────────────────────────────
# H2H
# ──────────────────────────────────────────────────────────────────────────────

def fetch_h2h(pa: PlayerProfile, pb: PlayerProfile) -> tuple[int, int, str]:
    if pa.atp_id and pb.atp_id:
        data = _get_json(ATP_H2H_API.format(pid_a=pa.atp_id, pid_b=pb.atp_id))
        if data:
            try:
                aw = int(data.get("player1Wins") or data.get("PlayerOneWins") or 0)
                bw = int(data.get("player2Wins") or data.get("PlayerTwoWins") or 0)
                if aw + bw > 0:
                    lead = pa.short_name if aw > bw else pb.short_name
                    return aw, bw, f"{lead} leads H2H {max(aw,bw)}-{min(aw,bw)}"
            except Exception:
                pass
    # Fallback WTA : Tennis Abstract
    return fetch_h2h_wta(pa, pb)


def fetch_h2h_wta(pa: PlayerProfile, pb: PlayerProfile) -> tuple[int, int, str]:
    """H2H via Tennis Abstract pour les joueuses WTA (pas d'ID ATP)."""
    name_a = (pa.full_name or pa.short_name).replace(" ", "").replace(".", "")
    name_b = (pb.full_name or pb.short_name).replace(" ", "").replace(".", "")
    try:
        url = f"https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p={name_a}"
        r = SESSION.get(url, timeout=10)
        html = r.text.lower()
        nb_short = pb.short_name.split(".")[-1].strip().lower()
        wins, losses = 0, 0
        for line in html.split("\n"):
            if nb_short in line or name_b.lower()[:6] in line:
                if ">w<" in line or '">w' in line or 'class="w"' in line:
                    wins += 1
                elif ">l<" in line or '">l' in line or 'class="l"' in line:
                    losses += 1
        if wins + losses > 0:
            lead = pa.short_name if wins > losses else pb.short_name
            return wins, losses, f"{lead} mène H2H {max(wins,losses)}-{min(wins,losses)}"
    except Exception:
        pass
    return 0, 0, "Pas de confrontation directe répertoriée"

# ──────────────────────────────────────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────────────────────────────────────

def run_match(
    match_str:      str,
    tournament:     str            = "ATP Tour",
    tournament_lvl: str            = "ATP 250",
    surface:        str            = "Hard",
    market_odds_a:  Optional[float]= None,
    market_odds_b:  Optional[float]= None,
    bookmaker:      str            = "",
    pick_number:    int            = 1,
) -> MatchPick:
    sep   = re.compile(r"\s+vs\.?\s+", re.I)
    parts = sep.split(match_str.strip())
    if len(parts) != 2:
        raise ValueError(f"Cannot parse '{match_str}' — use 'A. Player vs B. Player'")

    na, nb = parts[0].strip(), parts[1].strip()
    log.info(f"\n{'═'*60}\nMATCH: {na} vs {nb}  [{tournament} · {surface}]\n{'═'*60}")

    pa = fetch_player_profile(na)
    pb = fetch_player_profile(nb)

    match_name = f"{na} vs {nb}"

    # --- Validation ---
    validation = validate_match(
        pa, pb, surface,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
    )
    if not validation.passed:
        log.warning(f"VALIDATION FAILED {match_name}: {validation.errors}")

    h2h_a, h2h_b, h2h_s = fetch_h2h(pa, pb)
    prob_a, prob_b, comps = calculate_probability(
        pa, pb, surface, h2h_a, h2h_b, market_odds_a, market_odds_b
    )

    fo_a, fo_b = fair_odds(prob_a), fair_odds(prob_b)
    ea = edge_pct(market_odds_a, fo_a) if market_odds_a else None
    eb = edge_pct(market_odds_b, fo_b) if market_odds_b else None

    # Sanity guards — catch upstream corruption early
    assert 0.0 < prob_a < 1.0 and 0.0 < prob_b < 1.0, \
        f"Probability out of range: prob_a={prob_a}, prob_b={prob_b}"
    assert abs((prob_a + prob_b) - 1.0) < 0.01, \
        f"Probabilities do not sum to 1.0: {prob_a + prob_b}"
    assert fo_a >= 1.0 and fo_b >= 1.0, \
        f"Fair odds below 1.0: fo_a={fo_a}, fo_b={fo_b}"

    # --- Confidence ---
    max_edge_dec = max(ea or 0.0, eb or 0.0) / 100.0   # % → decimal
    confidence = compute_confidence(
        pa, pb, surface, validation,
        edge=max_edge_dec,
        model_prob=max(prob_a, prob_b),
    )

    # --- EV filter ---
    ev_a = (compute_ev(market_odds_a, fo_a, validation, confidence)
            if market_odds_a else EVResult(edge=0.0, is_value=False,
                                           filter_reason="NO MARKET ODDS"))
    ev_b = (compute_ev(market_odds_b, fo_b, validation, confidence)
            if market_odds_b else EVResult(edge=0.0, is_value=False,
                                           filter_reason="NO MARKET ODDS"))
    best_ev = ev_a if ev_a.edge > ev_b.edge else ev_b

    pick_player = ""
    if ea is not None and eb is not None:
        if ea >= eb and ea > 0:  pick_player = na
        elif eb > 0:             pick_player = nb

    tour = "WTA" if "wta" in tournament.lower() or "wta" in tournament_lvl.lower() else "ATP"

    pick = MatchPick(
        player_a=pa, player_b=pb, surface=surface,
        tournament=tournament, tournament_level=tournament_lvl,
        tour=tour,
        prob_a=prob_a, prob_b=prob_b,
        fair_odds_a=fo_a, fair_odds_b=fo_b,
        market_odds_a=market_odds_a, market_odds_b=market_odds_b,
        edge_a=ea, edge_b=eb,
        pick_player=pick_player, bookmaker=bookmaker,
        h2h_summary=h2h_s, factor_breakdown=comps,
        simulation=comps.get("monte_carlo", {}),
        confidence=confidence,
        validation_passed=validation.passed,
        filter_reason=best_ev.filter_reason or "",
        validation_warnings=validation.warnings,
    )

    card     = format_pick_card(pick, pick_number)
    table    = format_factor_table(pick)
    analysis = format_value_analysis(pick)
    print("\n" + card + table + analysis + "\n")

    if best_ev.is_value:
        maybe_alert(pick, card + "\n" + analysis)
    else:
        log.info(f"FILTERED {match_name}: {best_ev.filter_reason}")
    return pick


def run_from_config(cfg_path: str = "config.json") -> None:
    if not os.path.exists(cfg_path):
        log.warning(f"No {cfg_path} found.")
        return
    with open(cfg_path) as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {})
    if tg.get("bot_token"):      _tg.TELEGRAM_BOT_TOKEN   = tg["bot_token"]
    if tg.get("chat_id"):        _tg.TELEGRAM_CHAT_ID     = tg["chat_id"]
    if tg.get("edge_threshold"): _fmt.EDGE_ALERT_THRESHOLD = float(tg["edge_threshold"])
    for i, m in enumerate(cfg.get("matches", []), 1):
        try:
            # Support both flat odds (odds_a/odds_b) and nested bookmakers array
            bk = m.get("bookmakers", [])
            if bk:
                odds_a    = bk[0].get("odds_a")
                odds_b    = bk[0].get("odds_b")
                bookmaker = bk[0].get("name", "")
            else:
                odds_a    = m.get("odds_a")
                odds_b    = m.get("odds_b")
                bookmaker = m.get("bookmaker", "")
            run_match(m["match"], m.get("tournament","ATP Tour"), m.get("level","ATP 250"),
                      m.get("surface","Hard"), odds_a, odds_b, bookmaker, i)
            time.sleep(2)
        except Exception as exc:
            log.error(f"Error on '{m.get('match','?')}': {exc}")
