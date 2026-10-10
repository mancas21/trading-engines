"""Live check: restart-on-crash vs no-restart-on-clean-exit.

Two separate proofs, because conflating them is how the sibling engine ended up
relaunching a deliberately-stopped process:

  A. a child that CRASHES is relaunched, up to the attempt cap
  B. a child that EXITS CLEANLY runs exactly once and the supervisor stops

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_restart.py
"""
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
ROOT = HERE.parents[0]
sys.path.insert(0, str(HERE))

from supervisor import Supervisor  # noqa: E402

PY = str(ROOT / "quant-alert-engine" / "venv" / "Scripts" / "python.exe")


def case_crashes() -> bool:
    """A child that always exits 1 must be attempted exactly max_attempts."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    sup = Supervisor(
        command=[PY, "-c", "import sys; print('crashing'); sys.exit(1)"],
        lock_path=tmp / "a.lock", log_path=tmp / "a.log",
        max_attempts=3, backoff_s=0.0,
    )
    rc = sup.run()
    ok = sup.attempts == 3 and rc != 0
    print(f"  A. crashing child   -> attempts={sup.attempts} (want 3), rc={rc} "
          f"(want non-zero)   {'PASS' if ok else 'FAIL'}")
    return ok


def case_clean_exit() -> bool:
    """A child that exits 0 must run exactly once; the supervisor stops."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    sup = Supervisor(
        command=[PY, "-c", "print('clean shutdown')"],
        lock_path=tmp / "b.lock", log_path=tmp / "b.log",
        max_attempts=9, backoff_s=0.0,
    )
    rc = sup.run()
    ok = sup.attempts == 1 and rc == 0
    print(f"  B. clean-exit child -> attempts={sup.attempts} (want 1), rc={rc} "
          f"(want 0)          {'PASS' if ok else 'FAIL'}")
    return ok


def case_real_scanner_crash() -> bool:
    """A child that CRASHES must be relaunched, and the relaunch that then
    exits cleanly must be respected.

    The sequence is deliberately crash-then-clean: attempt 1 exits 7, attempt 2
    exits 0. That proves both halves in one run. (The first version of this
    fixture ran clean-then-crash, which never reaches the crash — a clean exit
    stops the supervisor by design, so the second attempt never happened.)
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    state = tmp / "state.txt"
    fake = tmp / "fake_scanner.py"
    fake.write_text(
        "import pathlib, sys, time\n"
        f"s = pathlib.Path(r'{state}')\n"
        "n = int(s.read_text()) if s.exists() else 0\n"
        "s.write_text(str(n + 1))\n"
        "print('attempt-run', n, flush=True)\n"
        "time.sleep(0.2)\n"
        "sys.exit(7 if n == 0 else 0)\n",      # crash once, then exit cleanly
        encoding="utf-8",
    )
    sup = Supervisor(command=[PY, str(fake)], lock_path=tmp / "c.lock",
                     log_path=tmp / "c.log", max_attempts=4, backoff_s=0.2)
    rc = sup.run()
    text = (tmp / "c.log").read_text(encoding="utf-8")
    saw_crash = "exit=7" in text
    saw_restart = "attempt 2" in text
    saw_stop = "not relaunching" in text
    ok = saw_crash and saw_restart and saw_stop and sup.attempts == 2 and rc == 0
    print(f"  C. crash->restart->clean  -> attempts={sup.attempts} (want 2), "
          f"exit=7 seen: {saw_crash}, relaunched: {saw_restart}, "
          f"stopped: {saw_stop}   {'PASS' if ok else 'FAIL'}")
    return ok


def case_lock_released() -> bool:
    """The lock must not outlive the supervisor, or a restart wedges."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    lock = tmp / "d.lock"
    Supervisor(command=[PY, "-c", "pass"], lock_path=lock,
               log_path=tmp / "d.log", max_attempts=1, backoff_s=0.0).run()
    ok = not lock.exists()
    print(f"  D. lock released    -> lock file gone: {ok}          "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("live supervisor restart policy")
    print("=" * 70)
    results = [case_crashes(), case_clean_exit(), case_real_scanner_crash(),
               case_lock_released()]
    print("=" * 70)
    print("ALL PASS" if all(results) else "SOME FAILED")
    raise SystemExit(0 if all(results) else 1)