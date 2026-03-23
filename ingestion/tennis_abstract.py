"""
tennis_model/ingestion/tennis_abstract.py
==========================================
Tennis Abstract parsers — extracted from pipeline.py (mechanical extraction,
no logic changes).

Public functions
----------------
_parse_ta_serve_stats(html)         — ATP matchmx JS array → serve stats dict
_parse_ta_wta_serve_stats(name)     — WTA jsfrags #recent-results → serve stats dict
_parse_ta_wta_full_profile(name)    — WTA jsfrags all tables → full profile dict
"""
import logging
import re
from datetime import date
from typing import Optional

import requests

from tennis_model.models import SERVE_BOUNDS

log = logging.getLogger(__name__)

# Dedicated HTTP session for Tennis Abstract requests.
# Mirrors the SESSION setup in pipeline.py without the ATP-specific headers.
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
})


def _get_html(url: str) -> str:
    try:
        r = _SESSION.get(url, timeout=12)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        log.warning(f"Tennis Abstract fetch failed [{url}]: {exc}")
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# ATP — matchmx serve stats parser
# ──────────────────────────────────────────────────────────────────────────────

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
        log.warning("ATP serve stats: matchmx JS array not found in page — serve stats unavailable")
        return {}
    try:
        rows = _json.loads(m.group(1))
    except (ValueError, TypeError):
        log.warning("ATP serve stats: matchmx JSON parse failed — serve stats unavailable")
        return {}

    def _in_bounds(key: str, val: float) -> bool:
        lo, hi = SERVE_BOUNDS.get(key, (0.0, 1.0))
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


# ──────────────────────────────────────────────────────────────────────────────
# WTA — jsfrags serve stats only (legacy; superseded by _parse_ta_wta_full_profile)
# ──────────────────────────────────────────────────────────────────────────────

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
        log.warning(f"WTA jsfrags fetch empty/failed for {name_clean} — serve stats unavailable")
        return {}

    # The file is: var player_frag = `<html>...`;
    m = re.search(r'var player_frag\s*=\s*`(.*?)`;', js_text, re.DOTALL)
    table_html = m.group(1) if m else js_text

    soup = _BS(table_html, 'html.parser')
    table = soup.find('table', id='recent-results')
    if not table:
        log.warning(f"WTA jsfrags: no #recent-results table for {name_clean} — serve stats unavailable")
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

    def _in_bounds_wta(key: str, val: float) -> bool:
        lo, hi = SERVE_BOUNDS.get(key, (0.0, 1.0))
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


# ──────────────────────────────────────────────────────────────────────────────
# WTA — jsfrags full profile parser
# ──────────────────────────────────────────────────────────────────────────────

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
        log.warning(f"WTA jsfrags fetch empty/failed for {name_clean} — full profile unavailable")
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

    career_wins   = hard_wins  + clay_wins  + grass_wins
    career_losses = hard_losses + clay_losses + grass_losses

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

    def _ss_in_bounds(key: str, val: float) -> bool:
        lo, hi = SERVE_BOUNDS.get(key, (0.0, 1.0))
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
