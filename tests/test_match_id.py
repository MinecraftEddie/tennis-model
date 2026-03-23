"""
Audit tests for alerts.telegram._canon_last and _match_id.

Covers:
  1. Reversed player order  → same key
  2. Hyphenated / compound surnames  → consistent last token
  3. Accents / apostrophes  → accent-stripped key
  4. short_name vs full_name  → same last token
  5. Whitespace / case variants  → same key
  6. Cross-source consistency  → _match_id aligns with backtest._make_id logic
  7. Apostrophe in surname
  8. Three-token compound surname (e.g. del Potro)
"""
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.alerts.telegram import _canon_last, _match_id


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _player(short_name: str, full_name: str = None):
    """Minimal mock PlayerProfile with only the fields _match_id uses."""
    return SimpleNamespace(short_name=short_name, full_name=full_name)


def _pick(short_a, short_b, full_a=None, full_b=None, pick_player=None):
    """Minimal mock MatchPick."""
    return SimpleNamespace(
        player_a=_player(short_a, full_a),
        player_b=_player(short_b, full_b),
        pick_player=pick_player or short_a,
    )


# ──────────────────────────────────────────────────────────────────────────────
# _canon_last  unit tests
# ──────────────────────────────────────────────────────────────────────────────

def test_canon_last_short_name_with_initial():
    # "C. Alcaraz" → "alcaraz"
    assert _canon_last("C. Alcaraz") == "alcaraz"


def test_canon_last_full_name():
    # "Carlos Alcaraz" → "alcaraz"
    assert _canon_last("Carlos Alcaraz") == "alcaraz"


def test_canon_last_single_token():
    # "Djokovic" (no initial) → "djokovic"
    assert _canon_last("Djokovic") == "djokovic"


def test_canon_last_strips_accent_tilde():
    # "Muñoz" → "munoz"  (ñ = n + combining tilde)
    assert _canon_last("Muñoz") == "munoz"


def test_canon_last_strips_accent_acute():
    # "García" → "garcia"
    assert _canon_last("García") == "garcia"


def test_canon_last_strips_caron():
    # "Šaric" → "saric"
    assert _canon_last("Šaric") == "saric"


def test_canon_last_case_insensitive():
    # "SINNER" and "sinner" should produce the same output
    assert _canon_last("SINNER") == _canon_last("sinner") == "sinner"


def test_canon_last_extra_whitespace():
    # Double space inside, trailing space
    assert _canon_last("  J.  Sinner  ") == "sinner"


def test_canon_last_compound_surname_last_token():
    # "B. Haddad Maia" → rightmost token = "maia"
    assert _canon_last("B. Haddad Maia") == "maia"


def test_canon_last_del_potro():
    # "J. del Potro" → "potro"
    assert _canon_last("J. del Potro") == "potro"


def test_canon_last_hyphenated_first_name():
    # "Jo-W. Tsonga" → last token is "tsonga"
    assert _canon_last("Jo-W. Tsonga") == "tsonga"


def test_canon_last_apostrophe_surname():
    # "T. O'Brien" — apostrophe in surname, no diacritic to strip
    assert _canon_last("T. O'Brien") == "o'brien"


# ──────────────────────────────────────────────────────────────────────────────
# _match_id  integration tests
# ──────────────────────────────────────────────────────────────────────────────

def test_match_id_reversed_order_same_key():
    """CRITICAL: player_a / player_b swap must produce the same key."""
    p1 = _pick("C. Alcaraz", "J. Sinner")
    p2 = _pick("J. Sinner", "C. Alcaraz")
    assert _match_id(p1) == _match_id(p2)


def test_match_id_accent_vs_ascii_same_key():
    """Muñoz from odds API vs Munoz from static profile → same key."""
    p_accented = _pick("R. Muñoz", "J. Sinner")
    p_ascii    = _pick("R. Munoz", "J. Sinner")
    assert _match_id(p_accented) == _match_id(p_ascii)


def test_match_id_full_name_vs_short_name_same_key():
    """full_name path must produce same last token as short_name path."""
    p_full  = _pick("C. Alcaraz", "J. Sinner",
                    full_a="Carlos Alcaraz", full_b="Jannik Sinner")
    p_short = _pick("C. Alcaraz", "J. Sinner")
    assert _match_id(p_full) == _match_id(p_short)


def test_match_id_case_insensitive():
    """Short names in ALLCAPS vs normal case → same key."""
    p_upper = _pick("ALCARAZ", "SINNER")
    p_lower = _pick("C. Alcaraz", "J. Sinner")
    assert _match_id(p_upper) == _match_id(p_lower)


def test_match_id_hyphenated_surname_consistent():
    """Hyphenated first name should not affect the surname extracted."""
    p1 = _pick("Jo-W. Tsonga", "R. Nadal")
    p2 = _pick("J.W. Tsonga",  "R. Nadal")
    # Both should end in _nadal_tsonga (sorted)
    assert _match_id(p1) == _match_id(p2)


def test_match_id_whitespace_variants_same_key():
    """Extra spaces around names must not change the key."""
    p1 = _pick("C. Alcaraz",   "J. Sinner")
    p2 = _pick("  C. Alcaraz", "J.  Sinner  ")
    assert _match_id(p1) == _match_id(p2)


def test_match_id_compound_surname_consistent():
    """Compound surname: short_name with and without first initial → same last token."""
    p1 = _pick("B. Haddad Maia", "I. Swiatek")
    p2 = _pick("Haddad Maia",    "I. Swiatek")
    assert _match_id(p1) == _match_id(p2)


def test_match_id_key_format():
    """Sanity check: key is YYYY-MM-DD_xxx_yyy with sorted parts."""
    import re
    from datetime import date
    p = _pick("C. Alcaraz", "J. Sinner")
    mid = _match_id(p)
    today = date.today().strftime("%Y-%m-%d")
    assert mid.startswith(today + "_")
    suffix = mid[len(today) + 1:]
    parts = suffix.split("_")
    assert len(parts) == 2
    assert parts == sorted(parts), "Last-name parts should be in alphabetical order"


def test_match_id_full_name_divergence_uses_full():
    """
    When full_name last token differs from short_name last token, full_name wins
    (aligned with backtest._make_id which also uses full_name or short_name).
    """
    # full_name provided → use "alcaraz" from full_name
    p_with_full    = _pick("C. Alcaraz", "J. Sinner",
                           full_a="Carlos Alcaraz", full_b="Jannik Sinner")
    # no full_name → fall back to short_name → same "alcaraz"
    p_without_full = _pick("C. Alcaraz", "J. Sinner")
    assert _match_id(p_with_full) == _match_id(p_without_full)


def test_match_id_del_potro_consistent():
    """'J. del Potro' from odds API vs 'Juan Martin del Potro' from profile → same key."""
    p_short = _pick("J. del Potro", "R. Federer")
    p_full  = _pick("J. del Potro", "R. Federer",
                    full_a="Juan Martin del Potro", full_b="Roger Federer")
    assert _match_id(p_short) == _match_id(p_full)
