"""
ingestion/profile_cache.py
===========================
JSON-based local cache for player profiles with a 24-hour TTL.

Purpose
-------
Absorb transient Tennis Abstract / ATP API failures.  When a fresh fetch
fails (429, timeout, empty) but a recent cache entry exists, the pipeline
can continue in "degraded" mode rather than hard-failing or dropping the
match entirely.

Cache location
--------------
<repo_root>/data/profile_cache/<key>.json

Each file is a JSON object with a top-level "_cached_at" timestamp (Unix
epoch float) plus the profile field snapshot.  The directory is created on
first write; read failures never propagate to the caller.

API
---
profile_cache_key(tour, full_name) -> str
    Stable filesystem-safe key.

load_cached_profile(key)            -> Optional[dict]
    Returns cached dict if it exists and is < CACHE_TTL_SECONDS old.
    Returns None on miss, expiry, or any read error.

save_cached_profile(key, data)      -> None
    Writes atomically (temp file + rename) to avoid partial writes.
    Silent on any write error — never blocks the pipeline.

profile_to_cacheable(profile)       -> dict
    Snapshot the fields worth caching from a PlayerProfile.

apply_cached_to_profile(profile, cached) -> None
    Restore cached fields onto an existing PlayerProfile instance.
"""
import json
import logging
import os
import re
import tempfile
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── Cache directory (repo_root/data/profile_cache/) ──────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))   # tennis_model/ingestion/
_PKG_DIR  = os.path.dirname(_HERE)                        # tennis_model/
_REPO_ROOT = os.path.dirname(_PKG_DIR)                    # <repo_root>/
CACHE_DIR  = os.path.join(_REPO_ROOT, "data", "profile_cache")

# TTL: entries older than this are considered stale
CACHE_TTL_SECONDS: float = 86_400.0   # 24 hours

# Fields we cache from PlayerProfile (time-sensitive stats — not identity)
_CACHED_FIELDS = (
    "ranking",
    "hard_wins",   "hard_losses",
    "clay_wins",   "clay_losses",
    "grass_wins",  "grass_losses",
    "ytd_wins",    "ytd_losses",
    "recent_form",
    "serve_stats",
    "data_source",
    "age",
    "height_cm",
)


# ── Key ───────────────────────────────────────────────────────────────────────

def profile_cache_key(tour: str, full_name: str) -> str:
    """
    Return a stable, filesystem-safe cache key for a player profile.

    Examples
    --------
    profile_cache_key("ATP", "Novak Djokovic") -> "atp_novak_djokovic"
    profile_cache_key("WTA", "E. Rybakina")    -> "wta_e_rybakina"
    """
    clean = re.sub(r"[^a-z0-9_]", "_", full_name.lower().strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return f"{tour.lower()}_{clean}"


# ── Load ──────────────────────────────────────────────────────────────────────

def load_cached_profile(key: str) -> Optional[dict]:
    """
    Load a cached profile for *key*.

    Returns
    -------
    dict
        Cached profile data (minus _cached_at) if the entry exists and is
        within CACHE_TTL_SECONDS.
    None
        On cache miss, expiry, or any read/parse error.
    """
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.debug(f"[CACHE] Read error for {key}: {exc}")
        return None

    cached_at = data.get("_cached_at", 0.0)
    age = time.time() - cached_at
    if age > CACHE_TTL_SECONDS:
        log.debug(f"[CACHE] Stale ({age / 3600:.1f}h old): {key}")
        return None

    # Return profile data without the internal timestamp
    return {k: v for k, v in data.items() if k != "_cached_at"}


# ── Save ──────────────────────────────────────────────────────────────────────

def save_cached_profile(key: str, data: dict) -> None:
    """
    Persist *data* to the cache for *key*.

    Writes atomically via a temp file + os.replace() to avoid partial writes.
    Silently ignores any write error — cache failures must never block the
    pipeline.
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload = {"_cached_at": time.time(), **data}
        path = os.path.join(CACHE_DIR, f"{key}.json")
        # Write to temp file in same directory, then rename (atomic on same fs)
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, path)
            log.debug(f"[CACHE] Saved: {key}")
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning(f"[CACHE] Write error for {key}: {exc}")


# ── Serialise / deserialise ───────────────────────────────────────────────────

def profile_to_cacheable(profile) -> dict:
    """
    Return a JSON-serialisable snapshot of the cacheable fields from *profile*.

    Only time-sensitive stats are captured — identity fields (full_name,
    slug, atp_id, identity_source) are excluded because they come from the
    fast local-map lookup and are always accurate.
    """
    result: dict = {}
    for field in _CACHED_FIELDS:
        val = getattr(profile, field, None)
        if val is not None:
            result[field] = val
    return result


def apply_cached_to_profile(profile, cached: dict) -> None:
    """
    Restore *cached* fields onto *profile* in place.

    Only touches fields that were explicitly cached (see _CACHED_FIELDS).
    Fields already set on *profile* (e.g. from static curated data) are
    overwritten with the cached values to match what TA returned last time.
    """
    for field in _CACHED_FIELDS:
        if field in cached:
            setattr(profile, field, cached[field])
    log.debug(f"[CACHE] Applied cached fields for {profile.short_name}")
