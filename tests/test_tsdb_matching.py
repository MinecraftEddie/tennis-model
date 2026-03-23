"""
tests/test_tsdb_matching.py
===========================
Unit tests for the strict player-name matching logic in
tennis_model/integrations/thesportsdb_results.py

All tests are pure-unit (no network, no I/O).
fetch_match_result tests that require HTTP are patched via unittest.mock.
"""
import pytest
from unittest.mock import patch

from tennis_model.integrations.thesportsdb_results import (
    _norm,
    _canonical_key,
    _player_matches_field,
    _date_window,
    fetch_match_result,
)


# ──────────────────────────────────────────────────────────────────────────────
# _norm
# ──────────────────────────────────────────────────────────────────────────────

class TestNorm:
    def test_lowercase(self):
        assert _norm("CARLOS") == "carlos"

    def test_accents_stripped(self):
        assert _norm("Świątek")  == "swiatek"
        assert _norm("Muñoz")    == "munoz"
        assert _norm("García")   == "garcia"
        assert _norm("Šaric")    == "saric"

    def test_dots_removed(self):
        assert _norm("C. Alcaraz") == "c alcaraz"

    def test_hyphens_become_spaces(self):
        assert _norm("Haddad-Maia")  == "haddad maia"
        assert _norm("Garcia-Lopez") == "garcia lopez"

    def test_whitespace_collapsed(self):
        assert _norm("  Iga   Swiatek  ") == "iga swiatek"

    def test_empty_string(self):
        assert _norm("") == ""


# ──────────────────────────────────────────────────────────────────────────────
# _canonical_key
# ──────────────────────────────────────────────────────────────────────────────

class TestCanonicalKey:
    def test_full_name(self):
        assert _canonical_key("Carlos Alcaraz")      == ("alcaraz", "carlos")

    def test_initial_and_surname(self):
        assert _canonical_key("C. Alcaraz")          == ("alcaraz", "c")

    def test_surname_only(self):
        sur, first = _canonical_key("Alcaraz")
        assert sur   == "alcaraz"
        assert first == ""

    def test_compound_surname_three_tokens(self):
        # "B. Haddad Maia" → last token = "maia", first token = "b"
        assert _canonical_key("B. Haddad Maia")       == ("maia", "b")

    def test_compound_surname_full_name(self):
        assert _canonical_key("Beatriz Haddad Maia")  == ("maia", "beatriz")

    def test_accent_stripped_in_key(self):
        assert _canonical_key("Iga Świątek")          == ("swiatek", "iga")

    def test_hyphenated_normalised(self):
        # "Garcia-Lopez" → _norm → "garcia lopez" → last="lopez", first="garcia"
        assert _canonical_key("Garcia-Lopez")         == ("lopez", "garcia")

    def test_empty_string(self):
        assert _canonical_key("") == ("", "")


# ──────────────────────────────────────────────────────────────────────────────
# _player_matches_field — should MATCH
# ──────────────────────────────────────────────────────────────────────────────

class TestPlayerMatchesFieldPositive:
    def test_exact_full_name(self):
        assert _player_matches_field("Carlos Alcaraz", "Carlos Alcaraz")

    def test_exact_full_name_with_accents(self):
        # Both sides normalise to "swiatek iga" vs "swiatek iga"
        assert _player_matches_field("Iga Świątek", "Iga Swiatek")

    def test_initial_vs_full_first_name(self):
        # "C. Alcaraz" initial "c" matches "carlos"[0]
        assert _player_matches_field("C. Alcaraz", "Carlos Alcaraz")

    def test_full_first_name_vs_initial(self):
        # Reversed direction
        assert _player_matches_field("Carlos Alcaraz", "C. Alcaraz")

    def test_surname_only_query_vs_full_field(self):
        # No first name in query → surname alone is sufficient
        assert _player_matches_field("Alcaraz", "Carlos Alcaraz")

    def test_surname_only_field_vs_full_query(self):
        assert _player_matches_field("Carlos Alcaraz", "Alcaraz")

    def test_hyphen_removal_compound(self):
        # "Haddad Maia" vs "Beatriz Haddad-Maia" both reduce to surname "maia"
        assert _player_matches_field("Haddad Maia", "Beatriz Haddad-Maia")

    def test_accent_normalisation_query(self):
        assert _player_matches_field("Muñoz", "Munoz")

    def test_accent_normalisation_field(self):
        assert _player_matches_field("Munoz", "Muñoz")

    def test_same_initial_different_case(self):
        # Both sides initial "c" → match
        assert _player_matches_field("C. Alcaraz", "C. Alcaraz")


# ──────────────────────────────────────────────────────────────────────────────
# _player_matches_field — should NOT MATCH (false positives rejected)
# ──────────────────────────────────────────────────────────────────────────────

class TestPlayerMatchesFieldNegative:
    def test_token_containment_rejected(self):
        # OLD behaviour: "Lee" in ["tommy", "lee", "jones"] → True (WRONG)
        # NEW behaviour: q_sur="lee" ≠ f_sur="jones" → False
        assert not _player_matches_field("B. Lee", "Tommy Lee Jones")

    def test_surname_in_middle_of_hyphenated_name(self):
        # "Garcia" should NOT match "Garcia-Lopez" because surname → "lopez"
        assert not _player_matches_field("Garcia", "Garcia-Lopez")

    def test_different_long_first_names(self):
        # Venus ≠ Serena even though surname matches
        assert not _player_matches_field("Venus Williams", "Serena Williams")

    def test_different_initials(self):
        # "V. Williams" must NOT match "S. Williams"
        assert not _player_matches_field("V. Williams", "S. Williams")

    def test_short_surname_3_chars(self):
        # "Lee" is 3 chars < 4 — never match even if equal
        assert not _player_matches_field("B. Lee", "C. Lee")
        assert not _player_matches_field("Lee", "Lee")

    def test_short_surname_2_chars(self):
        assert not _player_matches_field("Li Wei", "Li Na")

    def test_short_surname_single_char(self):
        assert not _player_matches_field("A", "A")

    def test_empty_query(self):
        assert not _player_matches_field("", "Carlos Alcaraz")

    def test_empty_field(self):
        assert not _player_matches_field("Carlos Alcaraz", "")

    def test_completely_different_surnames(self):
        assert not _player_matches_field("Djokovic", "Federer")

    def test_initial_mismatch(self):
        # "C. Alcaraz" initial "c" must NOT match "R. Alcaraz" initial "r"
        assert not _player_matches_field("C. Alcaraz", "R. Alcaraz")

    def test_multi_char_first_name_vs_incompatible_initial(self):
        # "Venus Williams" first "venus" vs "S. Williams" initial "s"
        # "v"[0] = "v" ≠ "s"[0] = "s" → False
        assert not _player_matches_field("Venus Williams", "S. Williams")


# ──────────────────────────────────────────────────────────────────────────────
# _date_window
# ──────────────────────────────────────────────────────────────────────────────

class TestDateWindow:
    def test_returns_three_dates(self):
        window = _date_window("2026-03-21")
        assert len(window) == 3

    def test_correct_minus_one(self):
        assert _date_window("2026-03-21")[0] == "2026-03-20"

    def test_correct_center(self):
        assert _date_window("2026-03-21")[1] == "2026-03-21"

    def test_correct_plus_one(self):
        assert _date_window("2026-03-21")[2] == "2026-03-22"

    def test_month_boundary_backward(self):
        assert _date_window("2026-03-01")[0] == "2026-02-28"

    def test_month_boundary_forward(self):
        assert _date_window("2026-02-28")[2] == "2026-03-01"

    def test_year_boundary_backward(self):
        assert _date_window("2026-01-01")[0] == "2025-12-31"

    def test_year_boundary_forward(self):
        assert _date_window("2025-12-31")[2] == "2026-01-01"

    def test_invalid_date_returns_input_only(self):
        window = _date_window("not-a-date")
        assert window == ["not-a-date"]

    def test_invalid_date_partial(self):
        window = _date_window("2026-13-01")   # month 13 is invalid
        assert window == ["2026-13-01"]


# ──────────────────────────────────────────────────────────────────────────────
# fetch_match_result — reversed player order (mock-based)
# ──────────────────────────────────────────────────────────────────────────────

class TestFetchMatchResultReversedOrder:
    """
    Verify that fetch_match_result identifies the winner correctly when the
    event has player_b as strHomeTeam and player_a as strAwayTeam.
    """

    _FINISHED_EVENT_REVERSED = {
        "idEvent":      "99001",
        "strHomeTeam":  "Novak Djokovic",   # player_b in the call below
        "strAwayTeam":  "Carlos Alcaraz",   # player_a
        "strStatus":    "Match Finished",
        "strPostponed": "",
        "intHomeScore": "2",                # Djokovic won 2 sets
        "intAwayScore": "1",
    }

    @patch(
        "tennis_model.integrations.thesportsdb_results._fetch_events_for_league",
        return_value=[_FINISHED_EVENT_REVERSED],
    )
    def test_reversed_order_winner_b(self, _mock):
        # player_a = "Carlos Alcaraz", player_b = "Novak Djokovic"
        # Home = Djokovic (player_b), home_sets=2 > away_sets=1 → winner = B
        result = fetch_match_result("Carlos Alcaraz", "Novak Djokovic", "2026-03-21")
        assert result["status"] == "final"
        assert result["winner"] == "B"   # Djokovic is player_b and won

    @patch(
        "tennis_model.integrations.thesportsdb_results._fetch_events_for_league",
        return_value=[_FINISHED_EVENT_REVERSED],
    )
    def test_reversed_order_source_tagged(self, _mock):
        result = fetch_match_result("Carlos Alcaraz", "Novak Djokovic", "2026-03-21")
        assert result["source"] == "thesportsdb"
        assert result["event_id"] == "99001"

    @patch(
        "tennis_model.integrations.thesportsdb_results._fetch_events_for_league",
        return_value=[],
    )
    def test_no_events_returns_not_found(self, _mock):
        result = fetch_match_result("Alcaraz", "Djokovic", "2026-03-21")
        assert result["status"] == "not_found"
        assert result["winner"] is None
