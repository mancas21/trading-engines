"""Live check: the watchdog distinguishes a STALL from a long run.

Both earlier implementations were runtime-based, and both were wrong for the
same structural reason: `scanner.py` runs run_forever(), an infinite loop, so
EVERY child eventually exceeds any runtime bound. Measured from the real log:
"exceeded the 1200s watchdog" three times, exactly 1200s apart, each one
killing a scanner that was writing rows normally.

These cases pin the corrected semantics:

  A. a child that keeps making progress is NEVER killed, however long it runs
  B. a child that stops making progress IS killed, promptly
  C. the default stall limit clears the measured worst-case cycle
  D. the killed sentinel is non-zero, so it counts as a crash

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_stall.py
"""
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from supervisor import Supervisor, STALL_S, WATCHDOG_KILLED  # noqa: E402

PY = str(HERE.parents[0] / "quant-alert-engine" / "venv" / "Scripts" / "python.exe")

# Measured with the resolver attached: median 422s, max 583s per cycle.
MEASURED_MAX_CYCLE_S = 583.0


def case_progress_prevents_a_kill() -> bool:
    """A child that keeps touching its progress file must run to completion,
    even past the stall limit."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    marker = tmp / "progress"
    marker.write_text("0")
    child = tmp / "child.py"
    # runs 6s, touching the marker every second; stall limit is 2s
    child.write_text(
        "import pathlib, time\n"
        f"m = pathlib.Path(r'{marker}')\n"
        "for i in range(6):\n"
        "    m.write_text(str(i))\n"
        "    time.sleep(1)\n"
        "print('finished normally')\n",
        encoding="utf-8",
    )
    sup = Supervisor(command=[PY, str(child)], lock_path=tmp / "a.lock",
                     log_path=tmp / "a.log", max_attempts=1, backoff_s=0.0,
                     stall_s=2.0, poll_s=0.5,
                     progress_marker=lambda: marker.stat().st_mtime)
    rc = sup.run()
    text = (tmp / "a.log").read_text(encoding="utf-8")
    ok = rc == 0 and "stall" not in text.lower()
    print(f"  A. making progress   -> rc={rc} (want 0), killed={('stall' in text.lower())}")
    print(f"                          {'PASS' if ok else 'FAIL'}  (ran 6s with a 2s limit)")
    return ok


def case_stall_is_killed() -> bool:
    """A child that stops progressing must be killed promptly."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    marker = tmp / "progress"
    marker.write_text("0")
    child = tmp / "child.py"
    child.write_text(
        "import time\n"
        "print('about to wedge', flush=True)\n"
        "time.sleep(600)\n",          # never touches the marker again
        encoding="utf-8",
    )
    sup = Supervisor(command=[PY, str(child)], lock_path=tmp / "b.lock",
                     log_path=tmp / "b.log", max_attempts=1, backoff_s=0.0,
                     stall_s=3.0, poll_s=0.5,
                     progress_marker=lambda: marker.stat().st_mtime)
    t0 = time.time()
    sup.run()
    dt = time.time() - t0
    text = (tmp / "b.log").read_text(encoding="utf-8")
    killed = "stall" in text.lower()
    bounded = dt < 30
    ok = killed and bounded
    print(f"  B. stalled child     -> killed={killed}, elapsed={dt:.1f}s "
          f"(want well under 600s)")
    print(f"                          {'PASS' if ok else 'FAIL'}")
    return ok


def case_stall_limit_clears_a_real_cycle() -> bool:
    """The default must exceed the slowest legitimate cycle, or it would kill
    a slow-but-working scanner -- which is exactly what 300s did."""
    ok = STALL_S >= MEASURED_MAX_CYCLE_S
    print(f"  C. default stall     -> {STALL_S:.0f}s vs measured worst cycle "
          f"{MEASURED_MAX_CYCLE_S:.0f}s")
    print(f"                          {'PASS' if ok else 'FAIL'}")
    return ok


def case_sentinel_counts_as_a_crash() -> bool:
    ok = WATCHDOG_KILLED != 0
    print(f"  D. kill sentinel     -> {WATCHDOG_KILLED} (non-zero so it restarts)")
    print(f"                          {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("live supervisor STALL watchdog")
    print("=" * 70)
    r = [case_stall_limit_clears_a_real_cycle(),
         case_sentinel_counts_as_a_crash(),
         case_progress_prevents_a_kill(),
         case_stall_is_killed()]
    print("=" * 70)
    print("ALL PASS" if all(r) else "SOME FAILED")
    raise SystemExit(0 if all(r) else 1)