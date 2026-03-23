"""
SQLite-backed store tracking which picks have already been alerted.

Dedupe key: (match_id, picked_side, model_version)
  match_id     = YYYY-MM-DD_lastname_a_lastname_b
  picked_side  = pick.pick_player (short_name of the selected player)
  model_version= MODEL_VERSION env var (default "2.0") — bump to reset dedupe namespace
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from tennis_model.config.runtime_config import MODEL_VERSION

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "alerted_picks.db"


class DedupeStore:
    """SQLite-backed deduplication store."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or _DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerted_picks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id      TEXT    NOT NULL,
                    picked_side   TEXT    NOT NULL,
                    model_version TEXT    NOT NULL DEFAULT '2.0',
                    alerted_at    TEXT    NOT NULL,
                    UNIQUE(match_id, picked_side, model_version)
                )
                """
            )
            conn.commit()

    def already_sent(
        self,
        match_id: str,
        picked_side: str,
        model_version: str = MODEL_VERSION,
    ) -> bool:
        """Return True if this (match_id, picked_side, model_version) was already alerted."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM alerted_picks
                WHERE match_id=? AND picked_side=? AND model_version=?
                """,
                (match_id, picked_side, model_version),
            ).fetchone()
        if row is not None:
            log.info(f"[DEDUPE] already_sent: {match_id!r} / {picked_side!r} / v{model_version}")
        return row is not None

    def mark_sent(
        self,
        match_id: str,
        picked_side: str,
        model_version: str = MODEL_VERSION,
    ) -> None:
        """Record that an alert was sent for this pick."""
        alerted_at = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO alerted_picks
                        (match_id, picked_side, model_version, alerted_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (match_id, picked_side, model_version, alerted_at),
                )
                conn.commit()
        except Exception as exc:
            log.error(f"DedupeStore.mark_sent failed: {exc}")
