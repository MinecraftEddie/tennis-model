"""
tests/test_profile_cache.py
============================
Unit tests for ingestion/profile_cache.py.

Covers:
  - profile_cache_key stability and format
  - save + load round-trip (cache hit)
  - stale cache returns None (TTL expired)
  - apply_cached_to_profile applies all expected fields
  - profile_to_cacheable snapshots the right fields
  - write errors are silent (never raise)
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tennis_model.ingestion.profile_cache as _cache_mod
from tennis_model.ingestion.profile_cache import (
    profile_cache_key,
    save_cached_profile,
    load_cached_profile,
    profile_to_cacheable,
    apply_cached_to_profile,
)
from tennis_model.models import PlayerProfile


# ── Key ───────────────────────────────────────────────────────────────────────

def test_cache_key_stable():
    k1 = profile_cache_key("ATP", "Novak Djokovic")
    k2 = profile_cache_key("ATP", "Novak Djokovic")
    assert k1 == k2


def test_cache_key_format():
    k = profile_cache_key("ATP", "Novak Djokovic")
    assert k == "atp_novak_djokovic"


def test_cache_key_wta():
    k = profile_cache_key("WTA", "Elena Rybakina")
    assert k == "wta_elena_rybakina"


def test_cache_key_special_chars():
    """Dots and hyphens become underscores; no double-underscores."""
    k = profile_cache_key("ATP", "A. Player-Name")
    assert " " not in k
    assert "__" not in k


# ── Save / Load ───────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_cache(tmp_path, monkeypatch):
    """Redirect CACHE_DIR to a temp directory for the duration of a test."""
    monkeypatch.setattr(_cache_mod, "CACHE_DIR", str(tmp_path))
    return tmp_path


def test_save_load_hit(tmp_cache):
    key  = "atp_test_player"
    data = {"ranking": 42, "hard_wins": 100, "hard_losses": 30}
    save_cached_profile(key, data)
    loaded = load_cached_profile(key)
    assert loaded is not None
    assert loaded["ranking"]   == 42
    assert loaded["hard_wins"] == 100


def test_load_miss_no_file(tmp_cache):
    """Non-existent key returns None without raising."""
    result = load_cached_profile("atp_nobody")
    assert result is None


def test_load_stale_cache(tmp_cache, monkeypatch):
    """Cache entry older than TTL is treated as a miss."""
    monkeypatch.setattr(_cache_mod, "CACHE_TTL_SECONDS", 0.0)
    key = "atp_stale_player"
    save_cached_profile(key, {"ranking": 99})
    # TTL=0 means every entry is immediately stale
    loaded = load_cached_profile(key)
    assert loaded is None


def test_load_excludes_internal_timestamp(tmp_cache):
    """_cached_at must not appear in the returned dict."""
    save_cached_profile("atp_foo", {"ranking": 10})
    loaded = load_cached_profile("atp_foo")
    assert loaded is not None
    assert "_cached_at" not in loaded


def test_save_creates_directory(tmp_path, monkeypatch):
    """save_cached_profile creates the cache directory if missing."""
    nested = os.path.join(str(tmp_path), "new_subdir")
    monkeypatch.setattr(_cache_mod, "CACHE_DIR", nested)
    assert not os.path.exists(nested)
    save_cached_profile("atp_key", {"ranking": 5})
    assert os.path.exists(nested)


def test_save_write_error_is_silent(tmp_cache, monkeypatch):
    """A write error (e.g. permission denied) must not propagate."""
    # Simulate makedirs failure
    def _bad_makedirs(*a, **kw):
        raise PermissionError("no write")
    monkeypatch.setattr(os, "makedirs", _bad_makedirs)
    # Should not raise
    save_cached_profile("atp_broken", {"ranking": 1})


# ── profile_to_cacheable ──────────────────────────────────────────────────────

def test_profile_to_cacheable_includes_key_fields():
    p = PlayerProfile(short_name="Test Player")
    p.ranking    = 50
    p.hard_wins  = 80
    p.serve_stats = {"career": {"n": 100}}

    data = profile_to_cacheable(p)
    assert data["ranking"]    == 50
    assert data["hard_wins"]  == 80
    assert "serve_stats" in data


def test_profile_to_cacheable_no_none_values():
    """None fields should not appear in the cacheable dict."""
    p    = PlayerProfile(short_name="Test")
    data = profile_to_cacheable(p)
    # Nullable fields with None default should be absent
    for v in data.values():
        assert v is not None, "cacheable dict must not contain None values"


# ── apply_cached_to_profile ───────────────────────────────────────────────────

def test_apply_cached_to_profile_sets_fields():
    p      = PlayerProfile(short_name="Test Player")
    cached = {
        "ranking":     50,
        "hard_wins":   30,
        "hard_losses": 20,
        "ytd_wins":    5,
        "ytd_losses":  3,
        "recent_form": ["W", "L", "W"],
        "data_source": "tennis_abstract",
    }
    apply_cached_to_profile(p, cached)
    assert p.ranking     == 50
    assert p.hard_wins   == 30
    assert p.ytd_wins    == 5
    assert p.recent_form == ["W", "L", "W"]
    assert p.data_source == "tennis_abstract"


def test_apply_cached_overwrites_existing():
    """Cached values overwrite whatever was on the profile before."""
    p = PlayerProfile(short_name="Test")
    p.ranking = 9999
    apply_cached_to_profile(p, {"ranking": 42})
    assert p.ranking == 42


def test_apply_cached_ignores_unknown_keys():
    """Unknown keys in cached dict must not raise AttributeError."""
    p = PlayerProfile(short_name="Test")
    # Should not raise even if the cache contains a stale/extra key
    apply_cached_to_profile(p, {"ranking": 10, "_internal_key_xyz": "ignored"})
    assert p.ranking == 10
