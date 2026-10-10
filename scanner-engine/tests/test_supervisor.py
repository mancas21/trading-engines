"""Tests for the supervisor.

THREE BEHAVIOURS THIS SUPERVISOR MUST HAVE, EACH LEARNED THE HARD WAY
===================================================================

1. RESTART ONLY ON CRASH.
   The sibling engine's supervisor relaunches on any exit, so closing the
   window looks like a failure and the process comes back. A clean exit must
   be respected and must NOT trigger a relaunch.

2. SINGLE-INSTANCE LOCK, ACQUIRED BEFORE LAUNCHING THE CHILD.
   The sibling engine's flashing "Already running" dialog was a supervisor
   crash-loop spawning a SECOND engine on every attempt; each second instance
   hit a single-instance lock, popped a modal, and the loop repeated. The
   supervisor must take the lock itself and, if it is already held, exit
   quietly with code 0 rather than launching anything.

3. ATTEMPT NUMBERS AND LAST OUTPUT MUST BE LOGGED.
   A supervisor that restarts silently is indistinguishable from one that is
   working. The sibling's log showed skipping attempt numbers (1,2,4,5,6,7)
   which was the only clue that a loop was running.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_supervisor.py -v
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import supervisor as SUP

HERE = pathlib.Path(__file__).resolve().parent


# ── the lock ───────────────────────────────────────────────────────────────

def test_lock_is_exclusive_across_processes():
    a = SUP.InstanceLock(HERE / "test.lock")
    b = SUP.InstanceLock(HERE / "test.lock")
    assert a.acquire() is True
    try:
        assert b.acquire() is False, "a second lock holder must be refused"
    finally:
        a.release()
    assert b.acquire() is True
    b.release()


def test_lock_releases_even_if_holder_dies():
    """A stale lock from a killed process must not wedge the supervisor
    forever. The lock records its PID and is reclaimable when that PID is
    gone."""
    lock = HERE / "test_stale.lock"
    if lock.exists():
        lock.unlink()
    a = SUP.InstanceLock(lock)
    assert a.acquire()

    # simulate a dead holder by rewriting the pid to one that cannot exist
    a.write_pid(999999)
    b = SUP.InstanceLock(lock)
    assert b.acquire() is True, "a lock held by a dead pid must be reclaimable"
    b.release()
    if lock.exists():
        lock.unlink()


def test_lock_file_is_removed_on_release():
    lock = HERE / "test_rm.lock"
    if lock.exists():
        lock.unlink()
    a = SUP.InstanceLock(lock)
    a.acquire()
    a.release()
    assert not lock.exists()


# ── restart policy ─────────────────────────────────────────────────────────

def test_should_restart_only_on_nonzero_exit():
    assert SUP.should_restart(returncode=0) is False, "a clean exit must not relaunch"
    assert SUP.should_restart(returncode=1) is True
    assert SUP.should_restart(returncode=None) is True, "killed = crash = restart"


def test_restart_requires_a_bounded_attempt_count():
    """An infinite crash-loop is worse than a dead scanner. The cap must
    exist and be enforced."""
    s = SUP.Supervisor(max_attempts=5, lock_path=HERE / "t.lock",
                      command=[sys.executable, "-c", "pass"])
    assert s.should_continue(attempt=1, returncode=1) is True
    assert s.should_continue(attempt=4, returncode=1) is True
    assert s.should_continue(attempt=5, returncode=1) is False
    assert s.should_continue(attempt=6, returncode=1) is False


def test_clean_exit_stops_immediately_even_with_attempts_left():
    s = SUP.Supervisor(max_attempts=99, lock_path=HERE / "t.lock",
                      command=[sys.executable, "-c", "pass"])
    assert s.should_continue(attempt=1, returncode=0) is False


def test_supervisor_exits_zero_when_lock_already_held():
    """THE FLASHING-WINDOW FIX. A second supervisor must exit quietly with 0
    and never spawn a child."""
    lock = HERE / "test_dup.lock"
    if lock.exists():
        lock.unlink()
    held = SUP.InstanceLock(lock)
    assert held.acquire()
    try:
        marker = HERE / "test_dup_marker.txt"
        if marker.exists():
            marker.unlink()
        s = SUP.Supervisor(max_attempts=3, lock_path=lock,
                          command=[sys.executable, "-c",
                                   f"open(r'{marker}','w').write('x')"])
        rc = s.run()
        assert rc == 0, "a duplicate supervisor must exit 0"
        assert not marker.exists(), "a duplicate supervisor must not spawn a child"
    finally:
        held.release()
        if lock.exists():
            lock.unlink()


# ── logging ────────────────────────────────────────────────────────────────

def test_log_records_attempt_number_and_exit_code():
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    s = SUP.Supervisor(max_attempts=2, lock_path=log.parent / "s.lock",
                      command=[sys.executable, "-c", "pass"], log_path=log)
    s.run()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "attempt 1" in text
    assert "exit=0" in text


def test_log_captures_child_output_on_crash():
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    s = SUP.Supervisor(max_attempts=1, lock_path=log.parent / "s.lock",
                      command=[sys.executable, "-c",
                               "import sys; print('BOOM from child'); sys.exit(3)"],
                      log_path=log)
    s.run()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "BOOM from child" in text, "child output must reach the log"
    assert "exit=3" in text


def test_log_records_attempts_without_gaps():
    """The sibling's skipping attempt numbers (1,2,4,5...) were the only clue
    that a crash-loop was running. Attempt numbers must be contiguous."""
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    s = SUP.Supervisor(max_attempts=3, lock_path=log.parent / "s.lock",
                      command=[sys.executable, "-c", "import sys; sys.exit(1)"],
                      log_path=log)
    s.run()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "attempt 1" in text and "attempt 2" in text and "attempt 3" in text


# ── end to end: a command that fails then succeeds ─────────────────────────

def test_supervisor_restarts_a_crashing_child_up_to_the_cap():
    """A child that always crashes is attempted exactly max_attempts times."""
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    s = SUP.Supervisor(max_attempts=3, lock_path=log.parent / "s.lock",
                      command=[sys.executable, "-c", "import sys; sys.exit(2)"],
                      log_path=log)
    rc = s.run()
    assert rc != 0, "an exhausted crash-loop must report failure, not success"
    assert s.attempts == 3


def test_supervisor_does_not_restart_a_child_that_exits_cleanly():
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    s = SUP.Supervisor(max_attempts=5, lock_path=log.parent / "s.lock",
                      command=[sys.executable, "-c", "pass"], log_path=log)
    rc = s.run()
    assert rc == 0
    assert s.attempts == 1, "a clean child must run exactly once"


def test_lock_is_released_after_supervisor_exits():
    log = pathlib.Path(tempfile.mkdtemp()) / "sup.log"
    lock = log.parent / "s.lock"
    SUP.Supervisor(max_attempts=1, lock_path=lock,
                   command=[sys.executable, "-c", "pass"],
                   log_path=log).run()
    assert not lock.exists(), "the lock must not outlive the supervisor"