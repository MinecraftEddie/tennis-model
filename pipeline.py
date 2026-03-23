import json
import logging
import os
import re
import time
from datetime import date, datetime
from typing import Optional

import requests

from tennis_model.models import PlayerProfile, MatchPick, SERVE_BOUNDS
from tennis_model.ingestion.tennis_abstract import (
    _parse_ta_serve_stats,
    _parse_ta_wta_serve_stats,
    _parse_ta_wta_full_profile,
)
from tennis_model.profiles import STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
from tennis_model.elo import get_elo_engine, canonical_id
from tennis_model.model import calculate_probability, fair_odds, edge_pct
from tennis_model.probability_adjustments import shrink_toward_market
from tennis_model.validation import validate_match
from tennis_model.confidence import compute_confidence
from tennis_model.ev import compute_ev, EVResult
from tennis_model.formatter import (
    format_pick_card, format_factor_table, format_value_analysis,
    EDGE_ALERT_THRESHOLD, EDGE_DISPLAY_THRESHOLD, _pct, _quality_tier,
)
from tennis_model.telegram import send_telegram, maybe_alert
from tennis_model.odds_feed import get_live_odds, fetch_slate
# P1: extracted helpers
from tennis_model.ingestion.identity import resolve_identity
from tennis_model.ingestion.http_utils import fetch_with_retry
from tennis_model.ingestion.profile_cache import (
    profile_cache_key       as _profile_cache_key,
    load_cached_profile     as _load_cached_profile,
    save_cached_profile     as _save_cached_profile,
    profile_to_cacheable    as _profile_to_cacheable,
    apply_cached_to_profile as _apply_cached_to_profile,
)
# P2: clean profile entry point + quality classification
from tennis_model.ingestion.profile_fetcher import fetch_profile_with_quality
import tennis_model.formatter as _fmt
import tennis_model.telegram as _tg

log = logging.getLogger(__name__)

# --- Evaluator second-pass filter (optional — gracefully absent) ---
try:
    from tennis_model.evaluator import evaluate as _evaluator_evaluate
    EVALUATOR_AVAILABLE = True
except ImportError:
    _evaluator_evaluate = None
    EVALUATOR_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
# ATP API ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

ATP_PLAYER_API  = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayer?playerId={pid}"
ATP_STATS_API   = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayerStats?playerId={pid}&year=0&surface=0"
ATP_RESULTS_API = "https://www.atptour.com/-/ajax/playerdashboard/GetPlayerMatchResults?playerId={pid}&year={year}"
ATP_H2H_API     = "https://www.atptour.com/-/ajax/playerdashboard/GetH2HMatches?playerId={pid_a}&opponentId={pid_b}"


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
    except requests.RequestException as exc:
        log.warning(f"HTTP fetch failed [{url}]: {exc}")
        return ""


def _ta_fetch(url: str) -> tuple[str, str | None]:
    """
    Tennis Abstract fetch with explicit error classification + retry.
    Used by _top_up_from_tennis_abstract to distinguish failure modes.

    P1: uses fetch_with_retry() for transparent retries on 429/timeout/502-504
    (up to 3 attempts, 2s/4s backoff).  Error classification is unchanged.

    Returns:
        (html, degraded_reason) where degraded_reason is None on success, or one of:
        "degraded_ratelimit" (HTTP 429 — after all retries exhausted)
        "degraded_timeout"   (requests.Timeout — after all retries exhausted)
        "degraded_empty"     (non-200, connection error, or HTML too short)
    """
    try:
        r = fetch_with_retry(SESSION, url)   # P1: retry on transient failures
        if r.status_code == 429:
            log.warning(f"[TA] 429 rate-limited (after retries): {url}")
            return "", "degraded_ratelimit"
        r.raise_for_status()
        html = r.text
        if not html or len(html) < 500:
            log.warning(f"[TA] Response too short for: {url}")
            return "", "degraded_empty"
        return html, None
    except requests.Timeout:
        log.warning(f"[TA] Timeout (after retries): {url}")
        return "", "degraded_timeout"
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        log.warning(f"[TA] HTTP {code}: {url}")
        return "", "degraded_empty"
    except requests.RequestException as exc:
        log.warning(f"[TA] Fetch failed: {exc}")
        return "", "degraded_empty"


# ──────────────────────────────────────────────────────────────────────────────
# PLAYER ID RESOLUTION
# ──────────────────────────────────────────────────────────────────────────────

def resolve_player_id(short_name: str) -> tuple[str, str, str, str]:
    """
    Returns (full_name, slug, atp_id, identity_source).

    P1: thin wrapper — logic now lives in ingestion/identity.py.
    Kept for any callers outside pipeline.py; internal code uses
    resolve_identity() directly via fetch_player_profile().
    """
    r = resolve_identity(short_name)
    return r.full_name, r.slug, r.atp_id, r.source

# ──────────────────────────────────────────────────────────────────────────────
# LIVE ATP API (top-up layer)
# ──────────────────────────────────────────────────────────────────────────────

def _top_up_from_api(pid: str, profile: PlayerProfile) -> None:
    """Try ATP AJAX APIs to fill any gaps left by static data."""

    # Player overview (ranking, age, height)
    if profile.ranking == 9999 or profile.age is None:
        data = _get_json(ATP_PLAYER_API.format(pid=pid))
        if data:
            p = data.get("player") or data.get("data") or data
            try:
                if profile.ranking == 9999:
                    _r = p.get("Rank") or p.get("ranking")
                    if _r:
                        profile.ranking = int(_r)
                if profile.age is None:
                    _a = p.get("Age")
                    if _a:
                        profile.age = int(_a)
                if profile.height_cm is None:
                    _h = p.get("HeightCm")
                    if _h:
                        profile.height_cm = int(_h)
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
    """Tennis Abstract fallback. Fetches ranking for all players; serve stats for WTA via jsfrags."""
    name_clean = (profile.full_name or profile.short_name).replace(" ", "").replace(".", "")
    is_wta = profile.data_source == "wta_static"

    if is_wta:
        # WTA: fetch ranking only if unknown; serve stats always come from jsfrags (separate URL)
        if profile.ranking == 9999:
            html = _get_html(f"https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p={name_clean}")
            if html and len(html) >= 500:
                m = re.search(r'var currentrank\s*=\s*(\d+)', html)
                if not m:
                    m = re.search(r'(?:rank(?:ing)?)[^\d]{0,20}?#?(\d{1,4})\b', html, re.I)
                if m:
                    profile.ranking = int(m.group(1))
        if not profile.serve_stats:
            ss = _parse_ta_wta_serve_stats(name_clean)
            if ss:
                profile.serve_stats = ss
        return

    # ATP path: fetch from player-classic page
    url = f"https://www.tennisabstract.com/cgi-bin/player-classic.cgi?p={name_clean}"
    html, _fetch_err = _ta_fetch(url)  # P0: classify errors; P1: retry inside
    if _fetch_err:
        # P1: try local cache before falling through to P0 degraded state
        if profile.identity_source != "unresolved":
            _key    = _profile_cache_key(profile.tour or "atp", profile.full_name or profile.short_name)
            _cached = _load_cached_profile(_key)
            if _cached:
                _apply_cached_to_profile(profile, _cached)
                profile.profile_quality = "degraded"
                log.warning(
                    f"[CACHE HIT] {profile.short_name}: loaded cached TA profile "
                    f"({_fetch_err}) — data may be up to 24h old"
                )
                return

        # No cache (or unresolved identity) → P0 behavior: degraded, no hard fail
        log.warning(
            f"Tennis Abstract: ATP page failed for {profile.short_name} "
            f"[{_fetch_err}] — skipping TA top-up"
        )
        # P0: if identity was resolved, set a degraded state instead of leaving
        # data_source="unknown" which would trigger a hard fail in validation.
        if profile.identity_source != "unresolved":
            profile.data_source    = _fetch_err   # "degraded_ratelimit" / "degraded_timeout" / "degraded_empty"
            profile.profile_quality = "degraded"
        return

    if profile.ranking == 9999:
        m = re.search(r'(?:rank(?:ing)?)[^\d]{0,20}?#?(\d{1,4})\b', html, re.I)
        if m:
            profile.ranking = int(m.group(1))

    # Derive surface W/L from matchmx rows (cols: 2=surface, 4=W/L).
    # The TA player-classic page is fully JS-rendered; there is no HTML
    # surface-record section to regex-parse.  The previous loose regex
    # produced false matches on match-id strings (e.g. "2023-540-100"
    # → hard_wins=2023).  Counting directly from matchmx is reliable.
    _mmx_m = re.search(r'var matchmx\s*=\s*(\[.*?\]);', html, re.S)
    if _mmx_m:
        try:
            _mx_rows = json.loads(_mmx_m.group(1))
            _wl: dict = {}  # surf_key → [wins, losses]
            for _r in _mx_rows:
                if len(_r) < 5:
                    continue
                _s   = str(_r[2]).strip().lower()
                _res = str(_r[4]).strip().upper()
                if _s not in ("hard", "clay", "grass") or _res not in ("W", "L"):
                    continue
                _wl.setdefault(_s, [0, 0])
                if _res == "W":
                    _wl[_s][0] += 1
                else:
                    _wl[_s][1] += 1
            for _sk, _aw, _al in (("hard",  "hard_wins",  "hard_losses"),
                                   ("clay",  "clay_wins",  "clay_losses"),
                                   ("grass", "grass_wins", "grass_losses")):
                if getattr(profile, _aw) == 0 and _sk in _wl:
                    setattr(profile, _aw, _wl[_sk][0])
                    setattr(profile, _al, _wl[_sk][1])
        except (ValueError, TypeError):
            log.debug(f"{profile.short_name}: matchmx surface W/L parse failed")

    if not profile.recent_form:
        form = re.findall(r'\b([WL])\b', html)
        profile.recent_form = form[:10]

    # Serve stats: parse from matchmx JS array (ATP pages only — real point-level data)
    if not profile.serve_stats:
        ss = _parse_ta_serve_stats(html)
        if ss:
            profile.serve_stats = ss
            log.info(
                f"{profile.short_name}: serve stats from Tennis Abstract "
                f"({ss.get('career', {}).get('n', 0)} career matches with data)"
            )
        else:
            log.warning(f"{profile.short_name}: ATP serve stats unavailable from Tennis Abstract — using model defaults")

    if profile.ranking < 9999 or profile.hard_wins > 0:
        profile.data_source    = "tennis_abstract"
        profile.profile_quality = "full"   # P0: successful TA fetch = full quality
        # P1: persist to cache so a future 429/timeout can serve stale data
        _key = _profile_cache_key(profile.tour or "atp", profile.full_name or profile.short_name)
        _save_cached_profile(_key, _profile_to_cacheable(profile))

# ──────────────────────────────────────────────────────────────────────────────
# INACTIVITY HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _days_inactive(profile: PlayerProfile) -> int:
    """Days since the player's last recorded ELO result.
    Returns -1 if no history exists (new player, no results recorded yet) —
    meaning UNKNOWN, not zero. Callers must treat -1 as 'no data', not 'played today'.
    Only positive when the ELO entry has matches_played > 0, meaning a real
    result was recorded via --record, not just an initialisation from today's run."""
    elo = get_elo_engine()
    pid = canonical_id(profile.full_name or profile.short_name)
    entry = elo.ratings.get(pid)
    if entry and entry.matches_played > 0:
        try:
            last = date.fromisoformat(entry.last_updated)
            return (date.today() - last).days
        except ValueError:
            pass
    log.debug(f"{profile.short_name}: no ELO history — days_inactive unknown, returning -1")
    return -1


# ──────────────────────────────────────────────────────────────────────────────
# PROFILE BUILDER
# ──────────────────────────────────────────────────────────────────────────────

def _build_wta_estimated_profile(profile: PlayerProfile, name_clean: str) -> None:
    """
    Populate a WTA profile for a player not in WTA_PROFILES.
    Cascade: jsfrags serve stats → wplayer.cgi ranking → estimated defaults.
    NEVER touches ATP endpoints.
    Sets data_source="wta_estimated" so callers can apply confidence penalty.
    """
    # Try jsfrags for serve stats + surface records from recent results
    ss = _parse_ta_wta_serve_stats(name_clean)
    if ss:
        profile.serve_stats = ss
        n = ss.get("career", {}).get("n", 0)
        log.info(f"{profile.short_name}: WTA estimated profile — jsfrags {n} matches")

    # Try wplayer.cgi for ranking
    if profile.ranking == 9999:
        html = _get_html(f"https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p={name_clean}")
        if html and len(html) >= 500:
            m = re.search(r'var currentrank\s*=\s*(\d+)', html)
            if m:
                profile.ranking = int(m.group(1))
                log.info(f"{profile.short_name}: WTA ranking from wplayer.cgi = #{profile.ranking}")

    # Final fallback defaults — main draw WTA players are typically ranked 50-150
    if profile.ranking == 9999:
        profile.ranking = 150
        log.info(f"{profile.short_name}: no ranking found — defaulting to #150")

    if profile.hard_wins == 0 and profile.hard_losses == 0:
        # Neutral 50% hard record — no surface bias
        profile.hard_wins   = 50
        profile.hard_losses = 50

    profile.data_source = "wta_estimated"


def fetch_player_profile(short_name: str, tour: str = "") -> PlayerProfile:
    """
    Profile cascade — respects tour to prevent WTA players hitting ATP endpoints.

    ATP cascade:  static_curated → ATP AJAX API → Tennis Abstract matchmx
    WTA cascade:  jsfrags dynamic (tennis_abstract_dynamic)
                  → wta_static (stale fallback) → estimated defaults
                  NEVER falls through to ATP endpoints when tour='wta'
    """
    profile           = PlayerProfile(short_name=short_name)
    # P1: delegate to ingestion/identity.py
    _id = resolve_identity(short_name)
    profile.full_name       = _id.full_name or short_name
    profile.slug            = _id.slug
    profile.atp_id          = _id.atp_id
    profile.identity_source = _id.source   # P0: record how identity was resolved
    pid = _id.atp_id  # P2 fix: was undefined after P1 refactor (caused NameError on static curated path)

    is_wta = tour.lower() == "wta"

    # Layer 1: static curated (ATP)
    static_applied = False
    if not is_wta and pid and pid.upper() in STATIC_PROFILES:
        for k, v in STATIC_PROFILES[pid.upper()].items():
            setattr(profile, k, v)
        profile.data_source    = "static_curated"
        profile.profile_quality = "full"   # P0: curated static = full quality
        static_applied = True
        log.info(f"Static data applied for {profile.full_name}")

    name_clean = (profile.full_name or profile.short_name).replace(" ", "").replace(".", "")

    # ── WTA path ──────────────────────────────────────────────────────────────
    if is_wta:
        # Layer 1b: WTA static profiles — apply physical/static attributes as base.
        # Dynamic jsfrags will override time-sensitive fields (ranking, ytd, form, serve).
        name_lower = short_name.lower().strip()
        _last = name_lower.replace(".", " ").split()[-1]
        for key, val in WTA_PROFILES.items():
            if key in name_lower or key.split()[-1] == _last:
                for k, v in val.items():
                    setattr(profile, k, v)
                profile.data_source = "wta_static"
                static_applied = True
                break

        # Recompute name_clean after WTA_PROFILES may have set a better full_name
        name_clean = (profile.full_name or profile.short_name).replace(" ", "").replace(".", "")

        # Layer 0: Dynamic profile from Tennis Abstract jsfrags.
        # Overrides all time-sensitive WTA_PROFILES fields (ranking, ytd, surface splits,
        # recent_form, serve_stats, data_source) with live data.
        dynamic = _parse_ta_wta_full_profile(name_clean)
        if dynamic:
            for k, v in dynamic.items():
                if k == "serve_stats":
                    profile.serve_stats = v
                else:
                    setattr(profile, k, v)  # sets data_source="tennis_abstract_dynamic"
            profile.profile_quality = "full"  # P0: live jsfrags = full quality
            log.info(f"Dynamic WTA profile built from jsfrags for {profile.full_name}")
        elif static_applied:
            # jsfrags failed; fall back to stale WTA_PROFILES data + serve stats top-up
            profile.profile_quality = "degraded"  # P0: stale static = degraded
            log.warning(
                f"WTA static profile for {profile.full_name} — "
                f"data may be stale (manually maintained); jsfrags failed"
            )
            if not profile.serve_stats:
                ss = _parse_ta_wta_serve_stats(name_clean)
                if ss:
                    profile.serve_stats = ss
            if profile.ranking == 9999:
                wpl = _get_html(
                    f"https://www.tennisabstract.com/cgi-bin/wplayer.cgi?p={name_clean}"
                )
                if wpl and len(wpl) >= 500:
                    rm = re.search(r'var currentrank\s*=\s*(\d+)', wpl)
                    if rm:
                        profile.ranking = int(rm.group(1))
        else:
            # Unknown WTA player: build estimated profile, NEVER touch ATP endpoints
            _build_wta_estimated_profile(profile, name_clean)
            # P0: wta_estimated has identity resolved (via wta_profiles fallback or
            # estimated) but stats are defaults — treat as degraded, not unknown
            if profile.profile_quality == "unknown":
                profile.profile_quality = "degraded"

        if not profile.serve_stats:
            log.warning(f"{profile.full_name}: serve_stats unavailable — using model defaults")
        log.info(
            f"✓ {profile.full_name}: Rank #{profile.ranking} | "
            f"YTD {profile.ytd_wins}-{profile.ytd_losses} | "
            f"Hard {profile.hard_wins}-{profile.hard_losses} "
            f"({_pct(profile.hard_wins, profile.hard_losses)}%) | "
            f"Source: {profile.data_source} | Quality: {profile.profile_quality}"
        )
        return profile

    # ── ATP path ──────────────────────────────────────────────────────────────
    # Layer 2: live ATP API (fill any gaps)
    if pid:
        _top_up_from_api(pid, profile)

    # Layer 3: Tennis Abstract fallback (ATP only)
    needs_ta = (
        profile.ranking == 9999
        or (profile.hard_wins == 0 and profile.clay_wins == 0)
        or not profile.serve_stats
    )
    if needs_ta:
        _top_up_from_tennis_abstract(profile)

    # P0: final quality guard for ATP path.
    # After all top-up layers, if quality is still "unknown" it means every fetch
    # layer was skipped or failed.  Set quality based on identity resolution so
    # validation.py uses the correct criterion.
    if profile.profile_quality == "unknown":
        if profile.identity_source == "unresolved":
            pass  # stays "unknown" → hard fail in validation
        elif profile.data_source in ("static_curated", "tennis_abstract", "atp_api"):
            profile.profile_quality = "full"
        else:
            # identity resolved but stats unavailable — degrade, don't hard fail
            profile.profile_quality = "degraded"
            if profile.data_source == "unknown":
                profile.data_source = "degraded_empty"

    if not profile.serve_stats:
        log.warning(f"{profile.full_name}: serve_stats unavailable — using model defaults")
    log.info(
        f"✓ {profile.full_name}: Rank #{profile.ranking} | "
        f"YTD {profile.ytd_wins}-{profile.ytd_losses} | "
        f"Hard {profile.hard_wins}-{profile.hard_losses} "
        f"({_pct(profile.hard_wins, profile.hard_losses)}%) | "
        f"Form {''.join(profile.recent_form[:10]) or 'N/A'} | "
        f"Source: {profile.data_source} | Identity: {profile.identity_source} | "
        f"Quality: {profile.profile_quality}"
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
# SCAN HELPERS  (used by scan_today — no model logic)
# ──────────────────────────────────────────────────────────────────────────────

# Static metadata for known tournament sport keys from The Odds API.
# Key fragment must appear in the sport_key string (e.g. "miami" in "tennis_atp_miami_open").
_SPORT_KEY_META: dict[str, dict] = {
    "miami":          {"name": "Miami Open",            "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "indian_wells":   {"name": "Indian Wells Masters",  "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "monte_carlo":    {"name": "Monte-Carlo Masters",   "surface": "Clay",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "madrid":         {"name": "Madrid Open",           "surface": "Clay",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "rome":           {"name": "Rome Masters",          "surface": "Clay",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "canada":         {"name": "Canadian Open",         "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "montreal":       {"name": "Montreal Open",         "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "toronto":        {"name": "Toronto Open",          "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "cincinnati":     {"name": "Cincinnati Open",       "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "shanghai":       {"name": "Shanghai Masters",      "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "paris_masters":  {"name": "Paris Masters",         "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "beijing":        {"name": "Beijing Open",          "surface": "Hard",  "atp": "ATP 1000",   "wta": "WTA 1000"},
    "australian":     {"name": "Australian Open",       "surface": "Hard",  "atp": "Grand Slam", "wta": "Grand Slam"},
    "roland":         {"name": "Roland Garros",         "surface": "Clay",  "atp": "Grand Slam", "wta": "Grand Slam"},
    "french":         {"name": "Roland Garros",         "surface": "Clay",  "atp": "Grand Slam", "wta": "Grand Slam"},
    "wimbledon":      {"name": "Wimbledon",             "surface": "Grass", "atp": "Grand Slam", "wta": "Grand Slam"},
    "us_open":        {"name": "US Open",               "surface": "Hard",  "atp": "Grand Slam", "wta": "Grand Slam"},
    "dubai":          {"name": "Dubai Open",            "surface": "Hard",  "atp": "ATP 500",    "wta": "WTA 1000"},
    "doha":           {"name": "Qatar Open",            "surface": "Hard",  "atp": "ATP 500",    "wta": "WTA 1000"},
    "barcelona":      {"name": "Barcelona Open",        "surface": "Clay",  "atp": "ATP 500",    "wta": "WTA 250"},
    "hamburg":        {"name": "Hamburg Open",          "surface": "Clay",  "atp": "ATP 500",    "wta": "WTA 250"},
    "queens":         {"name": "Queen's Club",          "surface": "Grass", "atp": "ATP 500",    "wta": "WTA 250"},
    "halle":          {"name": "Halle Open",            "surface": "Grass", "atp": "ATP 500",    "wta": "WTA 250"},
    "washington":     {"name": "Washington Open",       "surface": "Hard",  "atp": "ATP 500",    "wta": "WTA 500"},
    "vienna":         {"name": "Vienna Open",           "surface": "Hard",  "atp": "ATP 500",    "wta": "WTA 500"},
    "tokyo":          {"name": "Toray Pan Pacific",     "surface": "Hard",  "atp": "ATP 250",    "wta": "WTA 500"},
    "eastbourne":     {"name": "Eastbourne",            "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 500"},
    "birmingham":     {"name": "Birmingham",            "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 250"},
    "hertogenbosch":  {"name": "'s-Hertogenbosch",      "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 250"},
    "mallorca":       {"name": "Mallorca Open",         "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 250"},
    "bad_homburg":    {"name": "Bad Homburg Open",      "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 250"},
    "nottingham":     {"name": "Nottingham Open",       "surface": "Grass", "atp": "ATP 250",    "wta": "WTA 250"},
    "wuhan":          {"name": "Wuhan Open",            "surface": "Hard",  "atp": "ATP 250",    "wta": "WTA 1000"},
}


def _sport_key_meta(sport_key: str, tour: str) -> tuple[str, str, str]:
    """Returns (tournament_name, surface, level) inferred from Odds API sport key."""
    key_lower = sport_key.lower()
    for fragment, meta in _SPORT_KEY_META.items():
        if fragment in key_lower:
            return meta["name"], meta["surface"], meta[tour]
    # Fallback: prettify whatever remains after removing the tour prefix
    clean = (key_lower
             .replace(f"tennis_{tour}_", "")
             .replace("tennis_", "")
             .replace("_", " ")
             .title())
    return clean, "Hard", f"{'WTA' if tour == 'wta' else 'ATP'} 250"


def _is_in_player_map(name: str) -> bool:
    """True if the player's name fragment matches any key in PLAYER_ID_MAP."""
    name_lower = name.lower()
    for key in PLAYER_ID_MAP:
        if key in name_lower:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────────────────────────────────────

def run_match_with_result(
    match_str:      str,
    tournament:     str            = "ATP Tour",
    tournament_lvl: str            = "ATP 250",
    surface:        str            = "Hard",
    market_odds_a:  Optional[float]= None,
    market_odds_b:  Optional[float]= None,
    bookmaker:      str            = "",
    pick_number:    int            = 1,
    tour:           str            = "",
    odds_timestamp: str            = "",
    _silent:        bool           = False,
    _prefetched:    bool           = False,
    _audit=None,           # Optional[DailyAudit]
):
    """
    P6: Backward-compat wrapper — logic lives in orchestration/match_runner.

    The canonical implementation is orchestration.match_runner.run_match_with_result().
    This wrapper preserves the public API for callers that import from pipeline.

    Helpers fetch_h2h() and _days_inactive() remain here and are imported lazily
    by the orchestration layer (documented P6 constraint — future extraction possible).
    """
    from tennis_model.orchestration.match_runner import (
        run_match_with_result as _rmwr,
    )
    return _rmwr(
        match_str=match_str,
        tournament=tournament,
        tournament_lvl=tournament_lvl,
        surface=surface,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
        bookmaker=bookmaker,
        pick_number=pick_number,
        tour=tour,
        odds_timestamp=odds_timestamp,
        _silent=_silent,
        _prefetched=_prefetched,
        _audit=_audit,
    )


def run_match(
    match_str:      str,
    tournament:     str            = "ATP Tour",
    tournament_lvl: str            = "ATP 250",
    surface:        str            = "Hard",
    market_odds_a:  Optional[float]= None,
    market_odds_b:  Optional[float]= None,
    bookmaker:      str            = "",
    pick_number:    int            = 1,
    tour:           str            = "",
    odds_timestamp: str            = "",
    _silent:        bool           = False,
    _prefetched:    bool           = False,
    _audit=None,
) -> MatchPick:
    """
    Backward-compat wrapper — returns just the MatchPick.

    Callers that need the full MatchRunResult (EvaluatorDecision, AlertDecision,
    MatchFinalStatus) should call run_match_with_result() directly.
    """
    return run_match_with_result(
        match_str, tournament, tournament_lvl, surface,
        market_odds_a, market_odds_b, bookmaker, pick_number,
        tour, odds_timestamp, _silent, _prefetched, _audit,
    ).pick


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
            run_match(
                m["match"], m.get("tournament", "ATP Tour"), m.get("level", "ATP 250"),
                m.get("surface", "Hard"), odds_a, odds_b, bookmaker, i,
                tour=m.get("tour", ""),
                odds_timestamp=m.get("odds_timestamp", ""),
            )
            time.sleep(2)
        except Exception as exc:
            log.error(f"Error on '{m.get('match','?')}': {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# FULL SLATE SCANNER
# ──────────────────────────────────────────────────────────────────────────────

def scan_today(cfg_path: str = "config.json") -> None:
    """
    Full-slate scanner: fetch every active tennis event from The Odds API
    and attempt to evaluate each one.  Produces a compact operational summary.

    Classification:
      ALERT SENT         — model edge passed all filters; Telegram sent
      QUALIFIED ONLY     — edge passed EV filters but evaluator blocked
      BLOCKED            — data gate / suspicious edge / low confidence / etc.
      SKIPPED            — network error, parse failure, or no odds returned

    P1: emits a DailyAudit log at the end of each run.
    """
    # P1: initialise daily audit (logged at end of run)
    from tennis_model.orchestration.audit import DailyAudit
    audit = DailyAudit()

    # --- Load Telegram config (same as run_from_config) ---
    if os.path.exists(cfg_path):
        with open(cfg_path) as _f:
            cfg = json.load(_f)
        tg = cfg.get("telegram", {})
        if tg.get("bot_token"):      _tg.TELEGRAM_BOT_TOKEN    = tg["bot_token"]
        if tg.get("chat_id"):        _tg.TELEGRAM_CHAT_ID      = tg["chat_id"]
        if tg.get("edge_threshold"): _fmt.EDGE_ALERT_THRESHOLD = float(tg["edge_threshold"])

    # P0: log Telegram status once at startup — avoids silent dry_run surprises
    audit.telegram_configured = _tg.check_telegram_config()  # P1: record in audit

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'═'*64}")
    print(f"  FULL SLATE SCAN — {now_str}")
    print(f"{'═'*64}\n")

    slate = fetch_slate()
    total_atp = len(slate.get("atp", []))
    total_wta = len(slate.get("wta", []))

    print(f"  Slate fetched: {total_atp} ATP events, {total_wta} WTA events\n")

    alerts:         list[dict] = []   # EV passed (sent or qualified-only)
    blocked:        list[dict] = []   # blocked by a guard rail
    skipped:        list[dict] = []   # exception / no odds
    resolved_picks: list       = []   # MatchPick objects (for DailyAudit)

    # P5: MatchFinalStatus sets used for classification (replaces string inspection)
    from tennis_model.orchestration.match_runner import (
        ALERT_SENT_STATUSES as _ALERT_SENT_STATUSES,
        EVALUATOR_BLOCKED_STATUSES as _EVALUATOR_BLOCKED_STATUSES,
    )

    atp_both = atp_one = atp_none = 0
    pick_number = 0

    for tour in ("atp", "wta"):
        for event in slate.get(tour, []):
            pa_name  = event["player_a"]
            pb_name  = event["player_b"]
            match_str = f"{pa_name} vs {pb_name}"

            tournament, surface, level = _sport_key_meta(event["sport_key"], tour)

            # ATP: record player-map coverage before attempting
            mapping_status: str | None = None
            if tour == "atp":
                a_ok = _is_in_player_map(pa_name)
                b_ok = _is_in_player_map(pb_name)
                if a_ok and b_ok:
                    mapping_status = "both_mapped";  atp_both += 1
                elif a_ok or b_ok:
                    mapping_status = "one_mapped";   atp_one  += 1
                else:
                    mapping_status = "none_mapped";  atp_none += 1

            pick_number += 1
            try:
                # P5: use run_match_with_result() to get MatchRunResult
                _run_result = run_match_with_result(
                    match_str, tournament, level, surface,
                    market_odds_a=event["odds_a"],
                    market_odds_b=event["odds_b"],
                    bookmaker=event["bookmaker"],
                    pick_number=pick_number,
                    tour=tour,
                    odds_timestamp=event.get("commence_time", ""),
                    _silent=True,
                    _prefetched=True,
                    _audit=audit,
                )
                pick = _run_result.pick
            except Exception as exc:
                skipped.append({
                    "match":   match_str,
                    "tour":    tour.upper(),
                    "reason":  f"Error: {exc}",
                    "mapping": mapping_status,
                })
                log.warning(f"scan_today — error on {match_str}: {exc}")
                time.sleep(1)
                continue

            resolved_picks.append(pick)   # collect for DailyAudit
            fs = _run_result.final_status  # P5: use MatchFinalStatus for routing

            if fs in _ALERT_SENT_STATUSES:
                # EV passed → Telegram was sent (or attempted, or suppressed)
                picked    = pick.require_picked_side()
                pick_odds = picked["market_odds"]
                pick_edge = picked["edge"]
                er = getattr(pick, "evaluator_result", {}) or {}
                alerts.append({
                    "match":          match_str,
                    "tournament":     tournament,
                    "tour":           tour.upper(),
                    "surface":        surface,
                    "pick":           pick.pick_player,
                    "odds":           pick_odds,
                    "edge":           pick_edge,
                    "confidence":     pick.confidence,
                    "rec_action":     er.get("recommended_action", "send"),
                    "qualified_only": False,
                    "mapping":        mapping_status,
                    "quality_tier":   pick.quality_tier,
                    "final_status":   fs.value,
                })
            elif fs in _EVALUATOR_BLOCKED_STATUSES:
                # EV passed but evaluator held back (WATCHLIST or BLOCKED_MODEL)
                picked    = pick.require_picked_side()
                pick_odds = picked["market_odds"]
                pick_edge = picked["edge"]
                alerts.append({
                    "match":          match_str,
                    "tournament":     tournament,
                    "tour":           tour.upper(),
                    "surface":        surface,
                    "pick":           pick.pick_player,
                    "odds":           pick_odds,
                    "edge":           pick_edge,
                    "confidence":     pick.confidence,
                    "rec_action":     _run_result.filter_reason or fs.value,
                    "qualified_only": True,
                    "mapping":        mapping_status,
                    "quality_tier":   pick.quality_tier,
                    "final_status":   fs.value,
                })
            else:
                # NO_PICK, BLOCKED_VALIDATION, or any unexpected status
                fr = _run_result.filter_reason or ""
                blocked.append({
                    "match":        match_str,
                    "tour":         tour.upper(),
                    "reason":       fr or f"No edge ({fs.value})",
                    "mapping":      mapping_status,
                    "final_status": fs.value,
                })

            time.sleep(1)  # polite API rate limiting

    # ── Print compact summary ──────────────────────────────────────────────
    sent      = [a for a in alerts if not a["qualified_only"]]
    qualified = [a for a in alerts if a["qualified_only"]]

    # Split sent alerts by quality tier — FRAGILE were suppressed in maybe_alert()
    sent_ok      = [a for a in sent if a.get("quality_tier") != "FRAGILE"]
    sent_fragile = [a for a in sent if a.get("quality_tier") == "FRAGILE"]

    print(f"\n{'═'*64}")
    print("A.  ALERTS")
    print(f"{'═'*64}")
    if not sent_ok and not sent_fragile:
        print("  (none)")
    for a in sent_ok:
        e_str    = f"{a['edge']:+.1f}%" if a["edge"] is not None else "n/a"
        tier_tag = f"  [{a.get('quality_tier', 'CAUTION')}]"
        print(f"  ✅  {a['match']}{tier_tag}")
        print(f"      {a['tournament']} ({a['tour']}) · {a['surface']}")
        print(f"      BACK {a['pick']} @{a['odds']:.2f}  Edge {e_str}  Conf {a['confidence']}")
        print(f"      Action: {a['rec_action']}")

    if sent_fragile:
        if sent_ok:
            print()
        for a in sent_fragile:
            e_str = f"{a['edge']:+.1f}%" if a["edge"] is not None else "n/a"
            print(f"  🔴  {a['match']}  [FRAGILE — not sent to Telegram]")
            print(f"      {a['tournament']} ({a['tour']}) · {a['surface']}")
            print(f"      BACK {a['pick']} @{a['odds']:.2f}  Edge {e_str}  Conf {a['confidence']}")
            print(f"      Suppressed: serve sample n<5 or suspicious edge")

    if qualified:
        print(f"\n{'─'*64}")
        print("    QUALIFIED — EV passed, evaluator held back:")
        for a in qualified:
            e_str    = f"{a['edge']:+.1f}%" if a["edge"] is not None else "n/a"
            tier_tag = f"  [{a.get('quality_tier', 'CAUTION')}]" if a.get("quality_tier") else ""
            print(f"  ⚠   {a['match']} [{a['tour']}]{tier_tag}")
            print(f"      BACK {a['pick']} @{a['odds']:.2f}  Edge {e_str}  Conf {a['confidence']}")
            print(f"      Reason: {a['rec_action']}")

    print(f"\n{'═'*64}")
    print("B.  BLOCKED")
    print(f"{'═'*64}")
    if not blocked:
        print("  (none)")
    for b in blocked:
        mp = f"  [{b['mapping']}]" if b.get("mapping") else ""
        print(f"  ✗  {b['match']} [{b['tour']}]{mp}")
        print(f"      {b['reason']}")

    print(f"\n{'═'*64}")
    print("C.  SKIPPED")
    print(f"{'═'*64}")
    if not skipped:
        print("  (none)")
    for s in skipped:
        mp = f"  [{s['mapping']}]" if s.get("mapping") else ""
        print(f"  —  {s['match']} [{s['tour']}]{mp}")
        print(f"      {s['reason']}")

    print(f"\n{'═'*64}")
    print("D.  COVERAGE SUMMARY")
    print(f"{'═'*64}")
    total_processed = pick_number
    print(f"  ATP available    : {total_atp}")
    print(f"  WTA available    : {total_wta}")
    print(f"  Total processed  : {total_processed}")
    print(f"  Alerts sent      : {len(sent_ok)}")
    if sent_fragile:
        print(f"  FRAGILE suppressed: {len(sent_fragile)}  (serve n<5 or suspicious edge — not sent)")
    if qualified:
        print(f"  Qualified only   : {len(qualified)}  (EV passed, evaluator blocked)")
    print(f"  Blocked          : {len(blocked)}")
    print(f"  Skipped          : {len(skipped)}")
    if total_atp > 0:
        print(f"\n  ATP player-map coverage:")
        print(f"    Both mapped    : {atp_both}")
        print(f"    One mapped     : {atp_one}")
        print(f"    Neither mapped : {atp_none}")
    print()

    # ── E. DAILY AUDIT (P1 + P2) ──────────────────────────────────────────
    audit.matches_scanned = len(resolved_picks) + len(skipped)  # P2
    audit.populate_from_scan_results(resolved_picks, alerts, blocked, skipped)
    audit.log_summary()
    audit.save_audit_json()  # P2: persist JSON to data/audits/YYYY-MM-DD.json

    # ── F. TOMORROW SLATE DEBUG TABLE ──────────────────────────────────────
    from datetime import timedelta as _td
    _tomorrow = (date.today() + _td(days=1)).strftime("%Y-%m-%d")
    _tmrw_events = [
        e for tour in ("atp", "wta")
        for e in slate.get(tour, [])
        if e.get("commence_time", "").startswith(_tomorrow)
    ]
    if _tmrw_events:
        _lookup: dict = {}
        for _a in alerts:  _lookup[_a["match"]] = _a
        for _b in blocked: _lookup.setdefault(_b["match"], _b)
        for _s in skipped: _lookup.setdefault(_s["match"], _s)
        print(f"{'═'*64}")
        print(f"F.  TOMORROW SLATE DEBUG  ({_tomorrow})")
        print(f"{'═'*64}")
        for _e in _tmrw_events:
            _ms  = f"{_e['player_a']} vs {_e['player_b']}"
            _r   = _lookup.get(_ms, {})
            _pick_gen  = bool(_r.get("pick"))
            _qual_only = bool(_r.get("qualified_only"))
            _fr        = _r.get("reason", "")
            _tier      = _r.get("quality_tier", "?")
            _alrt      = _pick_gen and not _qual_only and _tier != "FRAGILE" and not _fr
            _blk       = (_fr or
                          ("EVALUATOR_BLOCKED" if _qual_only else
                           ("FRAGILE"          if _tier == "FRAGILE" else
                            ("NO_EDGE"         if _pick_gen else "MODEL_FILTER"))))
            _last_a    = _e["player_a"].strip().split()[-1].lower()
            _last_b    = _e["player_b"].strip().split()[-1].lower()
            _mid       = f"{_tomorrow}_{_last_a}_{_last_b}"
            print(f"  {_ms}")
            print(f"    match_id   : {_mid}")
            print(f"    commence   : {_e.get('commence_time', '?')}")
            print(f"    odds_found : yes  @{_e['odds_a']:.2f}/{_e['odds_b']:.2f}  [{_e.get('sport_key','?')}]")
            print(f"    pick_gen   : {'yes → ' + str(_r.get('pick', '')) if _pick_gen else 'no'}")
            print(f"    alertable  : {'yes' if _alrt else 'no'}")
            print(f"    blocked_by : {_blk if not _alrt else 'none (passed pipeline filters)'}")
            print(f"    tg_sent    : {'yes (check [DEDUPE]/[RISK]/[TELEGRAM] logs)' if _alrt else 'no'}")
            print()
    else:
        log.info(f"[SCAN] no events scheduled for tomorrow ({_tomorrow})")
