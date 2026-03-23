"""
Focused audit tests for the maybe_alert patch/restore in scan_matches_job.

Run with:
    PYTHONPATH=<parent-of-tennis_model> python tests/test_jobs_patch_restore.py

Verified behaviours
-------------------
1. Original maybe_alert is restored after normal execution
2. Original maybe_alert is restored after scan_today() raises any Exception
3. Repeated calls never stack wrappers (each run sees the original)
4. The patched wrapper is actually active during scan_today()
5. Exception inside scan_today() is swallowed (not re-raised to caller)
6. maybe_alert is restored correctly across multiple consecutive failure runs
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tennis_model.pipeline as _pipeline
from tennis_model.orchestration.jobs import scan_matches_job


# ──────────────────────────────────────────────────────────────────────────────
# Minimal mock objects
# ──────────────────────────────────────────────────────────────────────────────

class _Sentinel:
    """Unique callable whose identity we can assert on."""
    def __call__(self, *a, **kw):
        pass


def _raise_runtime(*a, **kw):
    raise RuntimeError("simulated scan failure")


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────

class _Patch:
    """Context manager: temporarily set _pipeline.<attr> = value."""
    def __init__(self, attr, value):
        self.attr = attr
        self.value = value
        self._orig = None

    def __enter__(self):
        self._orig = getattr(_pipeline, self.attr)
        setattr(_pipeline, self.attr, self.value)
        return self

    def __exit__(self, *_):
        setattr(_pipeline, self.attr, self._orig)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_maybe_alert_restored_after_normal_run():
    """Normal path: scan_today succeeds → maybe_alert is the original afterwards."""
    sentinel = _Sentinel()
    with _Patch("maybe_alert", sentinel), _Patch("scan_today", lambda: None):
        scan_matches_job(dry_run=True)
        assert _pipeline.maybe_alert is sentinel, (
            "maybe_alert was NOT restored after a normal scan_today() call"
        )
    print("PASS  test_maybe_alert_restored_after_normal_run")


def test_maybe_alert_restored_after_scan_raises():
    """Exception path: scan_today raises → maybe_alert is still restored."""
    sentinel = _Sentinel()
    with _Patch("maybe_alert", sentinel), _Patch("scan_today", _raise_runtime):
        scan_matches_job(dry_run=True)
        assert _pipeline.maybe_alert is sentinel, (
            "maybe_alert was NOT restored after scan_today() raised an exception"
        )
    print("PASS  test_maybe_alert_restored_after_scan_raises")


def test_scan_today_exception_does_not_propagate():
    """scan_today raising RuntimeError must be swallowed — not re-raised."""
    sentinel = _Sentinel()
    with _Patch("maybe_alert", sentinel), _Patch("scan_today", _raise_runtime):
        try:
            scan_matches_job(dry_run=True)
        except Exception as exc:
            raise AssertionError(
                f"scan_matches_job propagated an exception it should have swallowed: {exc}"
            )
    print("PASS  test_scan_today_exception_does_not_propagate")


def test_repeated_runs_do_not_stack_wrappers():
    """
    Running scan_matches_job N times must leave _pipeline.maybe_alert
    as the original — not a chain of N nested wrappers.
    """
    sentinel = _Sentinel()
    with _Patch("maybe_alert", sentinel), _Patch("scan_today", lambda: None):
        for _ in range(5):
            scan_matches_job(dry_run=True)
        assert _pipeline.maybe_alert is sentinel, (
            "maybe_alert was left as a wrapper after repeated scan_matches_job calls"
        )
    print("PASS  test_repeated_runs_do_not_stack_wrappers")


def test_patched_wrapper_is_active_during_scan():
    """
    During scan_today(), _pipeline.maybe_alert must be the deduped wrapper,
    not the original sentinel.
    """
    sentinel = _Sentinel()
    seen_during_scan = []

    def fake_scan():
        seen_during_scan.append(_pipeline.maybe_alert)

    with _Patch("maybe_alert", sentinel), _Patch("scan_today", fake_scan):
        scan_matches_job(dry_run=True)
        assert len(seen_during_scan) == 1
        assert seen_during_scan[0] is not sentinel, (
            "scan_today() ran with the original maybe_alert instead of the deduped wrapper"
        )
        assert _pipeline.maybe_alert is sentinel, (
            "maybe_alert was not restored after the scan"
        )
    print("PASS  test_patched_wrapper_is_active_during_scan")


def test_maybe_alert_restored_across_consecutive_failures():
    """
    If scan_today raises on every call, maybe_alert is restored each time.
    Confirms the finally clause fires correctly across repeated exception boundaries.
    """
    sentinel = _Sentinel()
    call_count = [0]

    def always_raises():
        call_count[0] += 1
        raise ValueError(f"error on call {call_count[0]}")

    with _Patch("maybe_alert", sentinel), _Patch("scan_today", always_raises):
        for i in range(3):
            scan_matches_job(dry_run=True)
            assert _pipeline.maybe_alert is sentinel, (
                f"maybe_alert not restored after exception on run {i + 1}"
            )
    assert call_count[0] == 3
    print("PASS  test_maybe_alert_restored_across_consecutive_failures")


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_maybe_alert_restored_after_normal_run,
        test_maybe_alert_restored_after_scan_raises,
        test_scan_today_exception_does_not_propagate,
        test_repeated_runs_do_not_stack_wrappers,
        test_patched_wrapper_is_active_during_scan,
        test_maybe_alert_restored_across_consecutive_failures,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            failed.append(t.__name__)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)
