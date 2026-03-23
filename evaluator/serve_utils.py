from __future__ import annotations

from typing import Any, Optional


def _nested_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _as_pct(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None

    # Tolère 65 ou 0.65
    if v > 1.0:
        v = v / 100.0

    if v < 0.0 or v > 1.0:
        return None
    return v


def _get_serve_metric(serve_stats: dict[str, Any], key: str) -> Optional[float]:
    """
    Lookup tolérant ATP/WTA :
    - d'abord à la racine
    - puis dans serve_stats['career']
    - puis dans serve_stats['recent']
    """
    root_val = _as_pct(serve_stats.get(key))
    if root_val is not None:
        return root_val

    career_val = _as_pct(_nested_get(serve_stats, "career", key))
    if career_val is not None:
        return career_val

    recent_val = _as_pct(_nested_get(serve_stats, "recent", key))
    if recent_val is not None:
        return recent_val

    return None
