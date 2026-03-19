import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import requests

from tennis_model.profiles import STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
from tennis_model.elo import get_elo_engine, canonical_id
from tennis_model.model import calculate_probability, fair_odds, edge_pct
from tennis_model.validation import validate_match
from tennis_model.confidence import compute_confidence
from tennis_model.ev import compute_ev, EVResult
from tennis_model.formatter import (
    format_pick_card, format_factor_table, format_value_analysis,
    EDGE_ALERT_THRESHOLD, EDGE_DISPLAY_THRESHOLD, _pct, _quality_tier,
)
from tennis_model.telegram import send_telegram, maybe_alert
from tennis_model.odds_feed import get_live_odds, fetch_slate
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
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PlayerProfile:
    short_name:       str
    full_name:        str            = ""
    atp_id:           str            = ""
    slug:             str            = ""
    ranking:          int            = 9999
    age:              Optional[int]  = None   # None = fetch failed / unknown
    height_cm:        Optional[int]  = None   # None = fetch failed / unknown
    plays:            str            = "Right-handed"
    turned_pro:       Optional[int]  = None   # metadata only — not consumed by model
    career_wins:      Optional[int]  = None   # None = not populated; 0 = confirmed zero
    career_losses:    Optional[int]  = None
    ytd_wins:         Optional[int]  = None   # None = not fetched; 0 = confirmed zero
    ytd_losses:       Optional[int]  = None
    hard_wins:        int            = 0
    hard_losses:      int            = 0
    clay_wins:        int            = 0
    clay_losses:      int            = 0
    grass_wins:       int            = 0
    grass_losses:     int            = 0
    recent_form:      list           = field(default_factory=list)
    career_high_rank: Optional[int]  = None   # metadata only — not consumed by model
    data_source:      str            = "unknown"
    serve_stats:      dict           = field(default_factory=dict)


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
    odds_source:          str   = "manual"   # "live" or "manual"
    evaluator_result:     dict  = field(default_factory=dict)
    quality_tier:         str   = ""         # "CLEAN" | "CAUTION" | "FRAGILE" — operational output only

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

    # 2. Skip ATP fetch entirely for WTA players — they have no ATP ID.
    # Return full_name from WTA_PROFILES so name_clean resolves to correct jsfrags URL.
    _last = name_lower.replace(".", " ").split()[-1]
    for key, val in WTA_PROFILES.items():
        if key in name_lower or key.split()[-1] == _last:
            full_name_wta = val.get("full_name", short_name)
            return full_name_wta, "", ""

    # 3. ATP search HTML (parse href pattern /players/slug/ID/overview)
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


def _parse_ta_serve_stats(html: str) -> dict:
    """Parse per-match serve stats from Tennis Abstract matchmx JS array.

    matchhead column indices (confirmed from live page):
      2=surf, 23=pts, 24=firsts(1stIn), 25=fwon(1stWon), 26=swon(2ndWon),
      32=opts(opp service pts), 34=ofwon(opp 1st won), 35=oswon(opp 2nd won)

    Returns a dict keyed by surface ('career','hard','clay','grass') plus
    'source'='tennis_abstract', or empty dict on any parse failure.
    """
    import json as _json
    m = re.search(r'var matchmx\s*=\s*(\[.*?\]);', html, re.S)
    if not m:
        return {}
    try:
        rows = _json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}

    # Plausible ranges for aggregated serve stats (raw point totals, not per-match %)
    _BOUNDS = {
        "first_serve_in":    (0.30, 0.80),
        "first_serve_won":   (0.40, 0.85),
        "second_serve_won":  (0.30, 0.75),
        "serve_win_pct":     (0.35, 0.85),
    }

    def _in_bounds(key: str, val: float) -> bool:
        lo, hi = _BOUNDS.get(key, (0.0, 1.0))
        return lo <= val <= hi

    def _agg(surface=None):
        pts = firsts = fwon = swon = opts = ofwon = oswon = n = 0
        skipped = 0
        for r in rows:
            if len(r) <= 35:
                continue
            if surface and r[2].lower() != surface:
                continue
            try:
                _pts = int(r[23]) if r[23] else 0
                if _pts == 0:
                    continue
                _firsts = int(r[24] or 0)
                _fwon   = int(r[25] or 0)
                _swon   = int(r[26] or 0)
                _second = _pts - _firsts
                # Row-level range check before accumulating
                if _firsts == 0 or _second <= 0:
                    skipped += 1
                    continue
                _fi = _firsts / _pts
                _fw = _fwon   / _firsts
                _sw = _swon   / _second
                if not (_in_bounds("first_serve_in", _fi)
                        and _in_bounds("first_serve_won", _fw)
                        and _in_bounds("second_serve_won", _sw)):
                    log.debug(
                        f"ATP matchmx: dropping row — out-of-range "
                        f"fi={_fi:.3f} fw={_fw:.3f} sw={_sw:.3f}"
                    )
                    skipped += 1
                    continue
                pts    += _pts
                firsts += _firsts
                fwon   += _fwon
                swon   += _swon
                opts   += int(r[32] or 0)
                ofwon  += int(r[34] or 0)
                oswon  += int(r[35] or 0)
                n      += 1
            except (ValueError, IndexError, TypeError):
                continue
        if skipped:
            log.debug(f"ATP matchmx: skipped {skipped} out-of-range rows")
        if pts == 0 or firsts == 0:
            return None
        second_pts = pts - firsts
        fi = round(firsts / pts, 4)
        fw = round(fwon / firsts, 4)
        sw = round(swon / second_pts, 4) if second_pts > 0 else 0.50
        swp = round(fi * fw + (1 - fi) * sw, 4)
        # Final aggregate bounds check
        if not (_in_bounds("first_serve_in", fi)
                and _in_bounds("first_serve_won", fw)
                and _in_bounds("second_serve_won", sw)
                and _in_bounds("serve_win_pct", swp)):
            log.warning(
                f"ATP matchmx: aggregate out of range after {n} matches — "
                f"fi={fi} fw={fw} sw={sw} swp={swp} — discarding"
            )
            return None
        return {
            "first_serve_in":    fi,
            "first_serve_won":   fw,
            "second_serve_won":  sw,
            "serve_win_pct":     swp,
            "return_points_won": round(1 - (ofwon + oswon) / opts, 4) if opts > 0 else 0.38,
            "n": n,
            "sample_type": f"matchmx_{surface or 'career'}",
        }

    result: dict = {}
    career = _agg()
    if career:
        result["career"] = career
    for surf in ("hard", "clay", "grass"):
        s = _agg(surf)
        if s and s["n"] >= 5:
            result[surf] = s
    if not result:
        return {}
    result["source"] = "tennis_abstract"
    return result


def _parse_ta_wta_serve_stats(name_clean: str) -> dict:
    """Fetch and parse WTA serve stats from Tennis Abstract jsfrags JS file.

    URL: https://www.tennisabstract.com/jsfrags/{CamelCaseName}.js
    Parses the 'recent-results' HTML table (columns 0-15):
      Date|Tournament|Surface|Rd|Rk|vRk|Match|Score|DR|A%|DF%|1stIn|1st%|2nd%|BPSvd|Time
                      idx=2                                        11   12   13   14

    Returns a dict keyed by surface ('career','hard','clay','grass') plus
    'source'='tennis_abstract_wta', or empty dict on failure.
    """
    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        log.warning("bs4 not installed — cannot parse WTA jsfrags serve stats")
        return {}

    url = f"https://www.tennisabstract.com/jsfrags/{name_clean}.js"
    js_text = _get_html(url)
    if not js_text or len(js_text) < 200:
        return {}

    # The file is: var player_frag = `<html>...`;
    m = re.search(r'var player_frag\s*=\s*`(.*?)`;', js_text, re.DOTALL)
    table_html = m.group(1) if m else js_text

    soup = _BS(table_html, 'html.parser')
    table = soup.find('table', id='recent-results')
    if not table:
        return {}

    def _pct_to_float(s: str):
        s = s.strip().rstrip('%')
        try:
            return float(s) / 100.0
        except (ValueError, TypeError):
            return None

    def _bp_fraction(s: str):
        bm = re.match(r'(\d+)/(\d+)', s.strip())
        return (int(bm.group(1)), int(bm.group(2))) if bm else None

    _BOUNDS_WTA = {
        "first_serve_in":    (0.30, 0.80),
        "first_serve_won":   (0.40, 0.85),
        "second_serve_won":  (0.30, 0.75),
        "serve_win_pct":     (0.35, 0.85),
    }

    def _in_bounds_wta(key: str, val: float) -> bool:
        lo, hi = _BOUNDS_WTA.get(key, (0.0, 1.0))
        return lo <= val <= hi

    surface_data: dict[str, dict] = {}
    valid_surfaces = {"hard", "clay", "grass"}
    _skipped_rows = 0

    for row in table.find_all('tr'):
        cells = [c.get_text(strip=True) for c in row.find_all('td')]
        if len(cells) < 15:
            continue
        surf_key = cells[2].strip().lower()
        if surf_key not in valid_surfaces:
            continue

        fi = _pct_to_float(cells[11])  # 1stIn
        fw = _pct_to_float(cells[12])  # 1st%
        sw = _pct_to_float(cells[13])  # 2nd%
        if fi is None or fw is None or sw is None:
            continue  # upcoming match or missing data

        # Row-level bounds check
        if (not _in_bounds_wta("first_serve_in", fi)
                or not _in_bounds_wta("first_serve_won", fw)
                or not _in_bounds_wta("second_serve_won", sw)):
            log.debug(
                f"WTA serve stats ({name_clean}) row skipped — out of bounds: "
                f"fi={fi:.3f} fw={fw:.3f} sw={sw:.3f}"
            )
            _skipped_rows += 1
            continue

        bp = _bp_fraction(cells[14]) if len(cells) > 14 else None

        for key in (surf_key, "career"):
            if key not in surface_data:
                surface_data[key] = {"fi": [], "fw": [], "sw": [], "bp_s": 0, "bp_t": 0, "n": 0}
            d = surface_data[key]
            d["fi"].append(fi)
            d["fw"].append(fw)
            d["sw"].append(sw)
            d["n"] += 1
            if bp:
                d["bp_s"] += bp[0]
                d["bp_t"] += bp[1]

    result: dict = {}
    for key, d in surface_data.items():
        n = d["n"]
        if n == 0:
            continue
        if key != "career" and n < 3:
            continue  # too few surface matches
        # KNOWN LIMITATION: jsfrags provides per-match % totals, not raw point counts.
        # We average the per-match percentages equally regardless of match length.
        # A 100-point match and a 20-point match each contribute 1/n weight.
        # The ATP path (matchmx) avoids this by aggregating raw point totals first.
        if n < 10:
            log.warning(
                f"WTA serve stats for {name_clean} ({key}): only {n} matches — "
                f"average-of-averages bias likely; treat stats as approximate"
            )
        else:
            log.info(
                f"WTA serve stats for {name_clean} ({key}): {n} matches averaged "
                f"(average-of-averages — unequal match lengths not corrected)"
            )
        avg_fi  = sum(d["fi"]) / n
        avg_fw  = sum(d["fw"]) / n
        avg_sw  = sum(d["sw"]) / n
        avg_swp = round(avg_fi * avg_fw + (1 - avg_fi) * avg_sw, 4)
        # Aggregate bounds check
        if (not _in_bounds_wta("first_serve_in", avg_fi)
                or not _in_bounds_wta("first_serve_won", avg_fw)
                or not _in_bounds_wta("second_serve_won", avg_sw)
                or not _in_bounds_wta("serve_win_pct", avg_swp)):
            log.warning(
                f"WTA serve stats ({name_clean}, {key}): aggregate out of bounds "
                f"fi={avg_fi:.3f} fw={avg_fw:.3f} sw={avg_sw:.3f} swp={avg_swp:.3f} — discarding"
            )
            continue
        entry = {
            "first_serve_in":   round(avg_fi, 4),
            "first_serve_won":  round(avg_fw, 4),
            "second_serve_won": round(avg_sw, 4),
            "serve_win_pct":    avg_swp,
            "n": n,
            "sample_type": f"jsfrags_{key}",
        }
        if d["bp_t"] > 0:
            entry["break_saved_pct"] = round(d["bp_s"] / d["bp_t"], 4)
        result[key] = entry

    if _skipped_rows:
        log.debug(f"WTA serve stats ({name_clean}): skipped {_skipped_rows} out-of-bounds rows total")
    if not result:
        return {}

    result["source"] = "tennis_abstract_wta"
    log.info(f"WTA serve stats from jsfrags ({name_clean}): "
             f"{result.get('career', {}).get('n', 0)} career matches, "
             f"surfaces={sorted(k for k in result if k not in ('source', 'career'))}")
    return result


def _parse_ta_wta_full_profile(name_clean: str) -> Optional[dict]:
    """
    Fetch WTA jsfrags ONCE and extract complete player data from the correct tables:

      #year-end-rankings → current ranking (first 'Current...' row, col 1)
      #career-splits     → all-time hard/clay/grass W/L (accurate career records)
      #tour-years        → current-year YTD W/L (all matches, not just recent-results subset)
      #recent-results    → recent form (last 10 W/L) + per-match serve stats

    Using these dedicated tables avoids the two key bugs in the previous implementation:
    (1) surface splits counted from only the last ~20 rows instead of career totals;
    (2) ranking required a separate wplayer.cgi HTTP request, often rate-limited.

    Returns a dict of PlayerProfile fields + 'serve_stats', or None on failure.
    """
    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        log.warning("bs4 not installed — cannot parse WTA jsfrags full profile")
        return None

    url = f"https://www.tennisabstract.com/jsfrags/{name_clean}.js"
    js_text = _get_html(url)
    if not js_text or len(js_text) < 200:
        log.debug(f"jsfrags fetch failed for {name_clean}")
        return None

    m = re.search(r'var player_frag\s*=\s*`(.*?)`;', js_text, re.DOTALL)
    table_html = m.group(1) if m else js_text
    soup = _BS(table_html, 'html.parser')

    def _cells(row):
        return [c.get_text(strip=True) for c in row.find_all(['td', 'th'])]

    # ── 1. Ranking from #year-end-rankings ────────────────────────────────────
    # First data row is 'Current (YYYY-MM-DD)' with WTA rank in column 1.
    ranking = 9999
    yer = soup.find('table', id='year-end-rankings')
    if yer:
        for row in yer.find_all('tr'):
            c = _cells(row)
            if c and c[0].startswith('Current') and len(c) >= 2:
                try:
                    ranking = int(c[1])
                except (ValueError, TypeError):
                    pass
                break

    # ── 2. Career surface splits from #career-splits ──────────────────────────
    # Rows: Split | M | W | L | Win% | ...
    # Split values: 'Hard', 'Clay', 'Grass' (also Indoor/Outdoor but we skip those)
    hard_wins = hard_losses = 0
    clay_wins = clay_losses = 0
    grass_wins = grass_losses = 0
    cs = soup.find('table', id='career-splits')
    if cs:
        for row in cs.find_all('tr'):
            c = _cells(row)
            if len(c) < 4:
                continue
            surf = c[0].lower().strip()
            try:
                w, l = int(c[2]), int(c[3])
            except (ValueError, TypeError):
                continue
            if surf == 'hard':
                hard_wins, hard_losses = w, l
            elif surf == 'clay':
                clay_wins, clay_losses = w, l
            elif surf == 'grass':
                grass_wins, grass_losses = w, l

    career_wins  = hard_wins  + clay_wins  + grass_wins
    career_losses= hard_losses + clay_losses + grass_losses

    # ── 3. YTD from #tour-years ───────────────────────────────────────────────
    # Rows: Year | M | W | L | Win% | ...
    current_year = str(date.today().year)
    ytd_wins = ytd_losses = 0
    ty = soup.find('table', id='tour-years')
    if ty:
        for row in ty.find_all('tr'):
            c = _cells(row)
            if c and c[0] == current_year and len(c) >= 4:
                try:
                    ytd_wins   = int(c[2])
                    ytd_losses = int(c[3])
                except (ValueError, TypeError):
                    pass
                break

    # ── 4. Recent form + serve stats from #recent-results ────────────────────
    # W/L detection: cells[6] text is "Winnerd. Loser" (win, no space before d.)
    # or "Opponent d. Winner" (loss, space before d.). Split on 'd. ' (space after).
    rr = soup.find('table', id='recent-results')
    if not rr:
        log.debug(f"No #recent-results table in jsfrags for {name_clean}")
        return None

    # Derive last-name fragment for W/L detection
    name_parts = re.findall(r'[A-Z][a-z]+', name_clean)
    search_names: list[str] = []
    if name_parts:
        search_names.append(name_parts[-1])
        if len(name_parts) >= 2:
            search_names.append(name_parts[-2])
    if name_clean[:6] not in search_names:
        search_names.append(name_clean[:6])

    valid_surfaces = {"hard", "clay", "grass"}

    def _pf(s: str):
        try:
            return float(s.strip().rstrip('%')) / 100.0
        except (ValueError, TypeError):
            return None

    def _bp(s: str):
        bm = re.match(r'(\d+)/(\d+)', s.strip())
        return (int(bm.group(1)), int(bm.group(2))) if bm else None

    _SS_BOUNDS = {
        "first_serve_in":    (0.30, 0.80),
        "first_serve_won":   (0.40, 0.85),
        "second_serve_won":  (0.30, 0.75),
        "serve_win_pct":     (0.35, 0.85),
    }

    def _ss_in_bounds(key: str, val: float) -> bool:
        lo, hi = _SS_BOUNDS.get(key, (0.0, 1.0))
        return lo <= val <= hi

    form_results: list[str] = []   # most-recent-first (table order)
    surf_serve:   dict      = {}
    _ss_skipped  = 0

    for row in rr.find_all('tr'):
        cells = [c.get_text(strip=True) for c in row.find_all('td')]
        if len(cells) < 7:
            continue

        surf_key   = cells[2].strip().lower()
        match_text = cells[6].strip()

        if surf_key not in valid_surfaces:
            continue
        if not match_text or 'd. ' not in match_text:
            continue   # upcoming match or bye

        parts_d = re.split(r'd\. ', match_text, 1)
        if len(parts_d) != 2:
            continue
        winner_part, loser_part = parts_d[0], parts_d[1]

        result: Optional[str] = None
        for sn in search_names:
            snl = sn.lower()
            if snl in winner_part.lower():
                result = 'W'
                break
            elif snl in loser_part.lower():
                result = 'L'
                break
        if result is None:
            continue

        form_results.append(result)

        # Serve stats (per-match averages — average-of-averages limitation noted)
        if len(cells) >= 14:
            fi = _pf(cells[11])
            fw = _pf(cells[12])
            sw = _pf(cells[13])
            if fi is not None and fw is not None and sw is not None:
                # Row-level bounds check
                if (not _ss_in_bounds("first_serve_in", fi)
                        or not _ss_in_bounds("first_serve_won", fw)
                        or not _ss_in_bounds("second_serve_won", sw)):
                    log.debug(
                        f"jsfrags ({name_clean}) serve row skipped — out of bounds: "
                        f"fi={fi:.3f} fw={fw:.3f} sw={sw:.3f}"
                    )
                    _ss_skipped += 1
                else:
                    bp_val = _bp(cells[14]) if len(cells) > 14 else None
                    for key in (surf_key, "career"):
                        if key not in surf_serve:
                            surf_serve[key] = {"fi": [], "fw": [], "sw": [],
                                               "bp_s": 0, "bp_t": 0, "n": 0}
                        d = surf_serve[key]
                        d["fi"].append(fi); d["fw"].append(fw); d["sw"].append(sw)
                        d["n"] += 1
                        if bp_val:
                            d["bp_s"] += bp_val[0]; d["bp_t"] += bp_val[1]

    if not form_results and career_wins + career_losses == 0 and ytd_wins + ytd_losses == 0:
        log.debug(f"No usable data in jsfrags for {name_clean}")
        return None

    # recent_form: oldest-first convention (model uses [-10:], [-1] = most recent)
    recent_form = list(reversed(form_results[:10]))

    if _ss_skipped:
        log.debug(f"jsfrags ({name_clean}): skipped {_ss_skipped} out-of-bounds serve rows")

    # Build serve stats dict
    serve_stats: dict = {}
    for key, d in surf_serve.items():
        n = d["n"]
        if n == 0 or (key != "career" and n < 3):
            continue
        avg_fi  = sum(d["fi"]) / n
        avg_fw  = sum(d["fw"]) / n
        avg_sw  = sum(d["sw"]) / n
        avg_swp = round(avg_fi * avg_fw + (1 - avg_fi) * avg_sw, 4)
        # Aggregate bounds check
        if (not _ss_in_bounds("first_serve_in", avg_fi)
                or not _ss_in_bounds("first_serve_won", avg_fw)
                or not _ss_in_bounds("second_serve_won", avg_sw)
                or not _ss_in_bounds("serve_win_pct", avg_swp)):
            log.warning(
                f"jsfrags ({name_clean}, {key}): aggregate serve stats out of bounds "
                f"fi={avg_fi:.3f} fw={avg_fw:.3f} sw={avg_sw:.3f} swp={avg_swp:.3f} — discarding"
            )
            continue
        entry = {
            "first_serve_in":   round(avg_fi, 4),
            "first_serve_won":  round(avg_fw, 4),
            "second_serve_won": round(avg_sw, 4),
            "serve_win_pct":    avg_swp,
            "n": n,
            "sample_type": f"jsfrags_{key}",
        }
        if d["bp_t"] > 0:
            entry["break_saved_pct"] = round(d["bp_s"] / d["bp_t"], 4)
        serve_stats[key] = entry
    if serve_stats:
        serve_stats["source"] = "tennis_abstract_wta"

    log.info(
        f"jsfrags full profile ({name_clean}): "
        f"Rank #{ranking} | YTD {ytd_wins}-{ytd_losses} | "
        f"Hard {hard_wins}-{hard_losses} | Clay {clay_wins}-{clay_losses} | "
        f"Grass {grass_wins}-{grass_losses} | "
        f"career {career_wins}-{career_losses} | "
        f"Form={''.join(recent_form[-5:])} | "
        f"Serve n={serve_stats.get('career', {}).get('n', 0)}"
    )
    result: dict = {
        "ytd_wins":     ytd_wins,
        "ytd_losses":   ytd_losses,
        "hard_wins":    hard_wins,
        "hard_losses":  hard_losses,
        "clay_wins":    clay_wins,
        "clay_losses":  clay_losses,
        "grass_wins":   grass_wins,
        "grass_losses": grass_losses,
        "career_wins":  career_wins,
        "career_losses":career_losses,
        "recent_form":  recent_form,
        "serve_stats":  serve_stats,
        "data_source":  "tennis_abstract_dynamic",
    }
    if ranking != 9999:
        result["ranking"] = ranking
    return result


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
    html = _get_html(url)
    if not html or len(html) < 500:
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

    if profile.ranking < 9999 or profile.hard_wins > 0:
        profile.data_source = "tennis_abstract"

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
    full, slug, pid   = resolve_player_id(short_name)
    profile.full_name = full or short_name
    profile.slug      = slug
    profile.atp_id    = pid

    is_wta = tour.lower() == "wta"

    # Layer 1: static curated (ATP)
    static_applied = False
    if not is_wta and pid and pid.upper() in STATIC_PROFILES:
        for k, v in STATIC_PROFILES[pid.upper()].items():
            setattr(profile, k, v)
        profile.data_source = "static_curated"
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
            log.info(f"Dynamic WTA profile built from jsfrags for {profile.full_name}")
        elif static_applied:
            # jsfrags failed; fall back to stale WTA_PROFILES data + serve stats top-up
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

        log.info(
            f"✓ {profile.full_name}: Rank #{profile.ranking} | "
            f"YTD {profile.ytd_wins}-{profile.ytd_losses} | "
            f"Hard {profile.hard_wins}-{profile.hard_losses} "
            f"({_pct(profile.hard_wins, profile.hard_losses)}%) | "
            f"Source: {profile.data_source}"
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
) -> MatchPick:
    sep   = re.compile(r"\s+vs\.?\s+", re.I)
    parts = sep.split(match_str.strip())
    if len(parts) != 2:
        raise ValueError(f"Cannot parse '{match_str}' — use 'A. Player vs B. Player'")

    na, nb = parts[0].strip(), parts[1].strip()
    log.info(f"\n{'═'*60}\nMATCH: {na} vs {nb}  [{tournament} · {surface}]\n{'═'*60}")

    # Derive tour for odds API: use explicit arg, else infer from tournament string
    _tour = (tour or ("wta" if "wta" in tournament.lower() or "wta" in tournament_lvl.lower()
                      else "atp")).lower()

    # --- Live odds (Step B) ---
    odds_source = "manual"
    live = get_live_odds(na, nb, tour=_tour)
    if live:
        market_odds_a = live["odds_a"]
        market_odds_b = live["odds_b"]
        bookmaker     = live["bookmaker"]
        odds_timestamp = live["timestamp"]
        odds_source   = "live"
        log.info(f"Live odds from {bookmaker}: {market_odds_a}/{market_odds_b}")
    elif market_odds_a or market_odds_b:
        log.warning("Using manual odds — may be stale")

    pa = fetch_player_profile(na, tour=_tour)
    pb = fetch_player_profile(nb, tour=_tour)

    match_name = f"{na} vs {nb}"

    # --- Validation ---
    validation = validate_match(
        pa, pb, surface,
        market_odds_a=market_odds_a,
        market_odds_b=market_odds_b,
        odds_source=odds_source,
        odds_timestamp=odds_timestamp,
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

    # --- EV filter ---
    days_inactive_a = _days_inactive(pa)
    days_inactive_b = _days_inactive(pb)
    days_inactive = max(days_inactive_a, days_inactive_b)

    # --- Confidence ---
    max_edge_dec = max(ea or 0.0, eb or 0.0) / 100.0   # % → decimal
    confidence = compute_confidence(
        pa, pb, surface, validation,
        edge=max_edge_dec,
        model_prob=max(prob_a, prob_b),
        days_inactive=days_inactive,
    )
    if days_inactive > 0:
        log.info(f"Days inactive (max of both players): {days_inactive}")

    # --- Data gate ---
    # ATP: block if both players are fully unknown/estimated.
    # WTA: stricter — both players must have tennis_abstract_dynamic profiles.
    #      Any stale, static, or estimated source produces fake edges; block early.
    _gate_reason = None
    if pa.data_source == "wta_estimated" and pb.data_source == "wta_estimated":
        _gate_reason = "INSUFFICIENT DATA: both players unrecognised"
    elif _tour == "wta":
        _bad = [f"{p.short_name}={p.data_source}" for p in [pa, pb]
                if p.data_source != "tennis_abstract_dynamic"]
        if _bad:
            _gate_reason = f"WTA DATA GATE: {', '.join(_bad)}"

    if _gate_reason:
        log.warning(f"PICK BLOCKED — {_gate_reason}")
        _block = EVResult(edge=0.0, is_value=False, filter_reason=_gate_reason)
        ev_a = ev_b = best_ev = _block
    else:
        ev_a = (compute_ev(market_odds_a, fo_a, validation, confidence, days_inactive, tour=_tour)
                if market_odds_a else EVResult(edge=0.0, is_value=False,
                                               filter_reason="NO MARKET ODDS"))
        ev_b = (compute_ev(market_odds_b, fo_b, validation, confidence, days_inactive, tour=_tour)
                if market_odds_b else EVResult(edge=0.0, is_value=False,
                                               filter_reason="NO MARKET ODDS"))
        best_ev = ev_a if ev_a.edge > ev_b.edge else ev_b

    pick_player = ""
    if ea is not None and eb is not None:
        if ea >= eb and ea > 0:  pick_player = na
        elif eb > 0:             pick_player = nb

    pick_tour = "WTA" if _tour == "wta" else "ATP"

    pick = MatchPick(
        player_a=pa, player_b=pb, surface=surface,
        tournament=tournament, tournament_level=tournament_lvl,
        tour=pick_tour,
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
        odds_source=odds_source,
    )

    # --- Evaluator second pass ---
    eval_result: dict = {}
    if EVALUATOR_AVAILABLE:
        _match_ctx = {
            "is_live":          False,
            "days_inactive_a":  days_inactive_a,
            "days_inactive_b":  days_inactive_b,
        }
        try:
            eval_result = _evaluator_evaluate(pick, _match_ctx)
            pick.evaluator_result = eval_result
            log.info(
                f"Evaluator: {eval_result['alert_level'].upper()} — "
                f"{eval_result['recommended_action'].upper()} — "
                f"{eval_result.get('short_message', '')}"
            )
            for flag in eval_result.get("risk_flags", []):
                log.warning(f"RISK FLAG: {flag}")
            if eval_result.get("recommended_action") == "watchlist":
                log.info(
                    f"WATCHLIST: {na} vs {nb} — "
                    f"{eval_result.get('reasons', [])}"
                )
        except Exception as exc:
            log.warning(f"Evaluator error — skipping second-pass filter: {exc}")
            eval_result = {}

    # Operational quality tier — output-only, no model logic
    pick.quality_tier = _quality_tier(pick)

    card     = format_pick_card(pick, pick_number)
    table    = format_factor_table(pick)
    analysis = format_value_analysis(pick)
    if not _silent:
        print("\n" + card + table + analysis + "\n")

    if best_ev.is_value:
        evaluator_approved = (
            not EVALUATOR_AVAILABLE
            or eval_result.get("recommended_action") in ("send", "send_with_caution")
        )
        if evaluator_approved:
            maybe_alert(pick, card + "\n" + analysis)
        else:
            # Update filter_reason so run_batch.py display is accurate
            pick.filter_reason = (
                f"EVALUATOR_{eval_result.get('recommended_action', 'blocked').upper()}"
            )
            log.warning(
                f"EV passed but evaluator blocked: "
                f"{eval_result.get('recommended_action')} — "
                f"{eval_result.get('short_message', '')}"
            )
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
    """
    # --- Load Telegram config (same as run_from_config) ---
    if os.path.exists(cfg_path):
        with open(cfg_path) as _f:
            cfg = json.load(_f)
        tg = cfg.get("telegram", {})
        if tg.get("bot_token"):      _tg.TELEGRAM_BOT_TOKEN    = tg["bot_token"]
        if tg.get("chat_id"):        _tg.TELEGRAM_CHAT_ID      = tg["chat_id"]
        if tg.get("edge_threshold"): _fmt.EDGE_ALERT_THRESHOLD = float(tg["edge_threshold"])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'═'*64}")
    print(f"  FULL SLATE SCAN — {now_str}")
    print(f"{'═'*64}\n")

    slate = fetch_slate()
    total_atp = len(slate.get("atp", []))
    total_wta = len(slate.get("wta", []))

    print(f"  Slate fetched: {total_atp} ATP events, {total_wta} WTA events\n")

    alerts:  list[dict] = []   # EV passed (sent or qualified-only)
    blocked: list[dict] = []   # blocked by a guard rail
    skipped: list[dict] = []   # exception / no odds

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
                pick = run_match(
                    match_str, tournament, level, surface,
                    market_odds_a=event["odds_a"],
                    market_odds_b=event["odds_b"],
                    bookmaker=event["bookmaker"],
                    pick_number=pick_number,
                    tour=tour,
                    odds_timestamp=event.get("commence_time", ""),
                    _silent=True,
                )
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

            fr = pick.filter_reason or ""

            if not fr and pick.pick_player:
                # EV passed → Telegram was sent (or attempted)
                if pick.pick_player == pick.player_b.short_name:
                    pick_odds = pick.market_odds_b
                    pick_edge = pick.edge_b
                else:
                    pick_odds = pick.market_odds_a
                    pick_edge = pick.edge_a
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
                })
            elif fr.startswith("EVALUATOR_"):
                # EV passed but evaluator said no
                if pick.pick_player == pick.player_b.short_name:
                    pick_odds = pick.market_odds_b
                    pick_edge = pick.edge_b
                else:
                    pick_odds = pick.market_odds_a
                    pick_edge = pick.edge_a
                alerts.append({
                    "match":          match_str,
                    "tournament":     tournament,
                    "tour":           tour.upper(),
                    "surface":        surface,
                    "pick":           pick.pick_player,
                    "odds":           pick_odds,
                    "edge":           pick_edge,
                    "confidence":     pick.confidence,
                    "rec_action":     fr,
                    "qualified_only": True,
                    "mapping":        mapping_status,
                    "quality_tier":   pick.quality_tier,
                })
            elif fr:
                blocked.append({
                    "match":   match_str,
                    "tour":    tour.upper(),
                    "reason":  fr,
                    "mapping": mapping_status,
                })
            else:
                # Both edges ≤ 0 — below threshold on both sides
                blocked.append({
                    "match":   match_str,
                    "tour":    tour.upper(),
                    "reason":  "No edge on either side",
                    "mapping": mapping_status,
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
