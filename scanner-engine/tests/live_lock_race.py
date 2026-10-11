"""Live check: the instance lock is atomic under CONCURRENT starts.

The previous lock was a TOCTOU race: read pid, test liveness, then write. Two
supervisors starting within the same second both saw "no lock" and both ran --
verified live, two supervisor trees and two scanner children writing to one
database.

This spawns N processes that all try to take the lock at the same instant. A
correct lock lets exactly ONE win.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_lock_race.py
"""
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

PY = str(HERE.parents[0] / "quant-alert-engine" / "venv" / "Scripts" / "python.exe")

CHILD = r'''
import sys, time
sys.path.insert(0, r"{here}")
from supervisor import InstanceLock
lk = InstanceLock(r"{lock}")
got = lk.acquire()
print("WON" if got else "LOST", flush=True)
time.sleep(2.0)
if got:
    lk.release()
'''


def race(n: int) -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp())
    lock = tmp / "race.lock"
    script = tmp / "racer.py"
    script.write_text(
        CHILD.format(here=str(HERE), lock=str(lock)), encoding="utf-8")

    # launch all at once; the GIL is irrelevant, these are separate processes
    procs = [subprocess.Popen([PY, str(script)], stdout=subprocess.PIPE,
                              text=True, stderr=subprocess.DEVNULL)
             for _ in range(n)]
    out = [p.communicate(timeout=60)[0].strip() for p in procs]
    won = sum(1 for o in out if o == "WON")
    return {"n": n, "won": won, "out": out}


if __name__ == "__main__":
    print("live instance-lock race")
    print("=" * 62)
    ok = True
    for n in (2, 4, 8):
        r = race(n)
        passed = r["won"] == 1
        ok = ok and passed
        print(f"  {n:2} concurrent starts -> exactly 1 winner: "
              f"{'PASS' if passed else 'FAIL'}  (won={r['won']})")
    print("=" * 62)
    print("ALL PASS" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)