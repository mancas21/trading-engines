"""Live check: a duplicate supervisor must exit 0 and spawn NO child.

This is the scenario that produced the flashing "Already running" dialog in the
sibling engine: a supervisor crash-loop spawning a second instance on every
attempt, each hitting a single-instance lock and popping a modal.

Hold the lock with a live process, then start a supervisor pointed at the same
lock and a child that would write a marker file if it ever ran.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_supervisor.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
ROOT = HERE.parents[0]
sys.path.insert(0, str(HERE))

from supervisor import InstanceLock, Supervisor  # noqa: E402

PY = str(ROOT / "quant-alert-engine" / "venv" / "Scripts" / "python.exe")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    lock_path = tmp / "x.lock"
    log_path = tmp / "sup.log"
    marker = tmp / "CHILD_RAN"
    child = tmp / "child.py"
    child.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path(r'{marker}').write_text('x')\n"
        "print('CHILD STARTED', flush=True)\n",
        encoding="utf-8",
    )

    # A live process holds the lock for the duration of the test.
    holder = InstanceLock(lock_path)
    assert holder.acquire(), "could not take the lock for the holder"
    print(f"holder acquired the lock (pid {os.getpid()}), file contents: "
          f"{lock_path.read_text(encoding='utf-8').strip()}")

    print("\n--- starting a DUPLICATE supervisor against the same lock ---")
    sup = Supervisor(command=[PY, str(child)], lock_path=lock_path,
                     log_path=log_path, max_attempts=3, backoff_s=0.0)
    rc = sup.run()

    print(f"\nduplicate supervisor exit code : {rc}   (must be 0)")
    print(f"duplicate supervisor attempts  : {sup.attempts}   (must be 0)")
    print(f"child actually ran             : {marker.exists()}   (must be False)")
    print(f"lock still held by the holder  : {lock_path.exists()}")

    print("\n--- supervisor log ---")
    print(log_path.read_text(encoding="utf-8") if log_path.exists() else "(none)")

    ok = (rc == 0 and sup.attempts == 0 and not marker.exists()
          and lock_path.exists())
    print("\nRESULT:", "PASS - no second instance spawned" if ok
          else "FAIL - a duplicate supervisor spawned a child")

    holder.release()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())