"""
tests/test_audit_json.py
=========================
Unit tests for DailyAudit.save_audit_json() — P2 addition.

Covers:
  - JSON file created in specified directory
  - File content matches DailyAudit fields
  - matches_scanned field present
  - Write failure is non-blocking (no exception)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.orchestration.audit import DailyAudit


def test_audit_json_file_created():
    audit = DailyAudit()
    audit.matches_scanned = 5

    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        assert os.path.exists(path), "Audit JSON file must be created"


def test_audit_json_minimal_content():
    audit = DailyAudit()
    audit.matches_scanned = 8
    audit.profiles_full   = 10
    audit.profiles_degraded = 2

    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

    assert data["date"]             == audit.date
    assert data["matches_scanned"]  == 8
    assert data["profiles_full"]    == 10
    assert data["profiles_degraded"] == 2


def test_audit_json_all_required_keys():
    audit = DailyAudit()
    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

    required = {
        "date", "matches_scanned",
        "profiles_full", "profiles_degraded", "profiles_failed",
        "no_pick_reasons",
        "picks_generated", "alerts_eligible", "alerts_sent",
        "alerts_suppressed", "alerts_failed",
        "watchlist_count",
        "telegram_configured", "telegram_dry_run",
    }
    missing = required - set(data.keys())
    assert not missing, f"Missing keys in audit JSON: {missing}"


def test_audit_json_write_failure_is_non_blocking():
    """A bad path must not raise — pipeline must continue."""
    audit = DailyAudit()
    # Non-existent root path — will fail on makedirs or open
    audit.save_audit_json(audits_dir="/nonexistent_root_xyz/audits")
    # Reaching here = non-blocking ✓


def test_audit_json_no_pick_reasons_serialised():
    audit = DailyAudit()
    audit.no_pick_reasons = {"NO EDGE": 3, "WTA DATA GATE": 2}

    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

    assert data["no_pick_reasons"]["NO EDGE"] == 3
    assert data["no_pick_reasons"]["WTA DATA GATE"] == 2
