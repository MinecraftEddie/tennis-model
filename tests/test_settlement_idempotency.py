"""
Focused idempotency tests for tracking/settle_predictions.py.

Covers:
  1. Normal settlement is idempotent (second call skipped)
  2. void_match is idempotent (second call skipped)
  3. mark_unsettled is idempotent (second call skipped)
  4. REGRESSION: failed name-resolution (no_match) must NOT block a
     subsequent correct settle() call for the same match_id
  5. REGRESSION: ambiguous name match must NOT block a subsequent correct call
  6. Intentionally-marked UNSETTLED (mark_unsettled) still blocks re-settlement
  7. pending() reflects settled state correctly

Run:
    PYTHONPATH=<parent-of-tennis_model> python tests/test_settlement_idempotency.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tennis_model.tracking.settle_predictions as _sp


# ──────────────────────────────────────────────────────────────────────────────
# Test harness: redirect file paths to a temp dir for isolation
# ──────────────────────────────────────────────────────────────────────────────

class _TempSettleEnv:
    """
    Context manager that redirects _FORWARD_FILE and _SETTLED_FILE to
    isolated temp files so tests never touch real data.
    """
    def __init__(self, forward_records: list):
        self._records = forward_records
        self._tmpdir = None

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._fwd  = os.path.join(self._tmpdir, "forward.jsonl")
        self._sett = os.path.join(self._tmpdir, "settled.jsonl")

        # Write forward records
        with open(self._fwd, "w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(rec) + "\n")

        # Patch module-level paths
        self._orig_fwd  = _sp._FORWARD_FILE
        self._orig_sett = _sp._SETTLED_FILE
        _sp._FORWARD_FILE  = self._fwd
        _sp._SETTLED_FILE  = self._sett
        return self

    def __exit__(self, *_):
        _sp._FORWARD_FILE  = self._orig_fwd
        _sp._SETTLED_FILE  = self._orig_sett
        # Temp files cleaned up automatically by OS; explicit cleanup optional


def _forward_record(match_id: str, player_a="J. Sinner", player_b="C. Alcaraz",
                    is_pick=True, picked_side="A") -> dict:
    return {
        "match_id":   match_id,
        "date":       "2026-03-21",
        "player_a":   player_a,
        "player_b":   player_b,
        "is_pick":    is_pick,
        "picked_side": picked_side,
        "odds_a":     2.10,
        "odds_b":     1.75,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_settle_idempotent_second_call_skipped():
    """Settling the same match_id twice must skip the second call."""
    mid = "2026-03-21_sinner_alcaraz"
    with _TempSettleEnv([_forward_record(mid)]):
        r1 = _sp.settle(mid, winner="A")
        r2 = _sp.settle(mid, winner="A")

    assert r1["result"] == "WIN"
    assert r2 == {}, f"Expected empty dict on duplicate, got {r2}"
    print("PASS  test_settle_idempotent_second_call_skipped")


def test_void_match_idempotent():
    """void_match called twice must skip the second call."""
    mid = "2026-03-21_sinner_alcaraz"
    with _TempSettleEnv([_forward_record(mid)]):
        r1 = _sp.void_match(mid)
        r2 = _sp.void_match(mid)

    assert r1["result"] == "VOID"
    assert r2 == {}
    print("PASS  test_void_match_idempotent")


def test_mark_unsettled_idempotent():
    """mark_unsettled called twice must skip the second call."""
    mid = "2026-03-21_sinner_alcaraz"
    with _TempSettleEnv([_forward_record(mid)]):
        r1 = _sp.mark_unsettled(mid)
        r2 = _sp.mark_unsettled(mid)

    assert r1["result"] == "UNSETTLED"
    assert r2 == {}
    print("PASS  test_mark_unsettled_idempotent")


def test_failed_name_resolution_no_match_does_not_block_correct_settle():
    """
    REGRESSION: settle() with an unrecognised winner name writes an UNSETTLED
    record to settled_predictions.jsonl.  Before the fix, this blocked any
    subsequent settle() call.  After the fix, the correct call must succeed.
    """
    mid = "2026-03-21_sinner_alcaraz"
    with _TempSettleEnv([_forward_record(mid)]):
        # First call: winner name that doesn't match either player
        r_fail = _sp.settle(mid, winner="Completely Unknown Player Name")
        assert r_fail.get("result") == "UNSETTLED"
        assert r_fail.get("settlement_confidence") == "no_match"

        # Second call: correct winner — must NOT be blocked by the failed attempt
        r_ok = _sp.settle(mid, winner="A")

    assert r_ok.get("result") == "WIN", (
        f"Correct settle() was blocked by a prior failed no_match record. Got: {r_ok}"
    )
    print("PASS  test_failed_name_resolution_no_match_does_not_block_correct_settle")


def test_failed_name_resolution_ambiguous_does_not_block_correct_settle():
    """
    REGRESSION: an ambiguous winner name (matches both players) writes an UNSETTLED
    record with settlement_confidence="ambiguous".  This must NOT block a subsequent
    explicit settle() with an unambiguous "A" or "B" winner.
    """
    mid = "2026-03-21_anna_ana"
    # Two players whose normalised names could produce an ambiguous match
    rec = _forward_record(mid, player_a="Anna Smith", player_b="Ana Smith",
                          picked_side="A")
    with _TempSettleEnv([rec]):
        # Ambiguous first attempt
        r_fail = _sp.settle(mid, winner="Smith")
        assert r_fail.get("settlement_confidence") in ("ambiguous", "no_match"), (
            f"Expected ambiguous/no_match, got: {r_fail.get('settlement_confidence')}"
        )

        # Explicit second attempt must succeed
        r_ok = _sp.settle(mid, winner="A")

    assert r_ok.get("result") == "WIN", (
        f"Correct settle() was blocked by prior ambiguous record. Got: {r_ok}"
    )
    print("PASS  test_failed_name_resolution_ambiguous_does_not_block_correct_settle")


def test_mark_unsettled_still_blocks_re_settle():
    """
    mark_unsettled() is an explicit user action (settlement_confidence=None).
    It must still block future settle() calls — only auto-failed records are skipped.
    """
    mid = "2026-03-21_sinner_alcaraz"
    with _TempSettleEnv([_forward_record(mid)]):
        _sp.mark_unsettled(mid, notes="result unknown")
        r2 = _sp.settle(mid, winner="A")

    assert r2 == {}, (
        f"settle() should be blocked after mark_unsettled(), got: {r2}"
    )
    print("PASS  test_mark_unsettled_still_blocks_re_settle")


def test_pending_reflects_settled_state():
    """
    pending() must exclude a match_id that has been successfully settled,
    but still include one that only has a failed-settlement record.
    """
    mid_settled  = "2026-03-21_sinner_alcaraz"
    mid_failed   = "2026-03-21_medvedev_zverev"
    fwd = [_forward_record(mid_settled), _forward_record(mid_failed)]

    with _TempSettleEnv(fwd):
        _sp.settle(mid_settled, winner="A")                        # settled correctly
        _sp.settle(mid_failed,  winner="Completely Unknown XYZ")   # fails: no_match

        pending = _sp.pending()
        pending_ids = {r["match_id"] for r in pending}

    assert mid_settled not in pending_ids, (
        "Correctly settled match should not appear in pending()"
    )
    assert mid_failed in pending_ids, (
        "Match with only a failed-settlement record should still appear in pending()"
    )
    print("PASS  test_pending_reflects_settled_state")


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_settle_idempotent_second_call_skipped,
        test_void_match_idempotent,
        test_mark_unsettled_idempotent,
        test_failed_name_resolution_no_match_does_not_block_correct_settle,
        test_failed_name_resolution_ambiguous_does_not_block_correct_settle,
        test_mark_unsettled_still_blocks_re_settle,
        test_pending_reflects_settled_state,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            import traceback
            print(f"ERROR {t.__name__}: {e}")
            traceback.print_exc()
            failed.append(t.__name__)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)
