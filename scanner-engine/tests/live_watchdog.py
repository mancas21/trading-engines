"""Live check: a wedged child is killed and relaunched.

The scanner sat for 312s with zero new rows while every venue aged past
max_age_s, and the supervisor never restarted it — `subprocess.run` with no
timeout cannot tell a hang from health.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_watchdog.py
"""
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from supervisor import Supervisor, WATCHDOG_KILLED  # noqa: E402

PY = str(HERE.parents[0] / "quant-alert-engine" / "venv" / "Scripts" / "python.exe")


def case_hang_is_killed() -> bool:
    """A child that sleeps forever must be killed at the watchdog and retried."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    sup = Supervisor(
        command=[PY, "-c", "import time; print('child up', flush=True); time.sleep(600)"],
        lock_path=tmp / "a.lock", log_path=tmp / "a.log",
        max_attempts=2, backoff_s=0.0, watchdog_s=8.0,
    )
    t0 = time.time()
    sup.run()
    dt = time.time() - t0
    text = (tmp / "a.log").read_text(encoding="utf-8")
    killed = "watchdog" in text
    retried = "attempt 2" in text
    # 2 attempts x 8s watchdog + startup ~= 16-20s; NOT 600s
    bounded = dt < 60
    ok = killed and retried and bounded
    print(f"  hang killed  : {killed}")
    print(f"  relaunched   : {retried}  (attempts={sup.attempts})")
    print(f"  elapsed      : {dt:.1f}s  (want well under the 600s sleep)")
    print(f"                {'PASS' if ok else 'FAIL'}")
    return ok


def case_healthy_child_is_untouched() -> bool:
    """A child that finishes normally must NOT be reported as a watchdog kill."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    sup = Supervisor(
        command=[PY, "-c", "print('done')"],
        lock_path=tmp / "b.lock", log_path=tmp / "b.log",
        max_attempts=1, backoff_s=0.0, watchdog_s=30.0,
    )
    rc = sup.run()
    text = (tmp / "b.log").read_text(encoding="utf-8")
    ok = rc == 0 and sup.attempts == 1 and "watchdog" not in text
    print(f"  clean child  : rc={rc} attempts={sup.attempts} "
          f"watchdog_fired={'watchdog' in text}")
    print(f"                {'PASS' if ok else 'FAIL'}")
    return ok


def case_watchdog_sentinel_is_nonzero() -> bool:
    """The sentinel must be non-zero or should_restart() would treat a hang as
    a clean exit and refuse to relaunch."""
    ok = WATCHDOG_KILLED != 0
    print(f"  sentinel     : {WATCHDOG_KILLED} (must be non-zero so it counts "
          f"as a crash)")
    print(f"                {'PASS' if ok else 'FAIL'}")
    return ok


# Measured cycle-to-cycle interval with the resolver attached: median 422s,
# max 583s. A watchdog below that kills healthy scanners -- a 300s default did
# exactly that in production.
MEASURED_MAX_CYCLE_S = 583.0


def case_default_watchdog_exceeds_the_slowest_real_cycle() -> bool:
    """The deadline must clear the SLOWEST legitimate cycle with headroom."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    sup = Supervisor(command=[PY, "-c", "pass"], lock_path=tmp / "c.lock",
                     log_path=tmp / "c.log")
    ok = sup.watchdog_s >= MEASURED_MAX_CYCLE_S * 2
    print(f"  default      : {sup.watchdog_s:.0f}s  (slowest real cycle "
          f"{MEASURED_MAX_CYCLE_S:.0f}s; want >=2x)")
    print(f"                {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("live supervisor watchdog")
    print("=" * 66)
    r = [case_watchdog_sentinel_is_nonzero(),
         case_default_watchdog_exceeds_the_slowest_real_cycle(),
         case_healthy_child_is_untouched(),
         case_hang_is_killed()]
    print("=" * 66)
    print("ALL PASS" if all(r) else "SOME FAILED")
    raise SystemExit(0 if all(r) else 1)