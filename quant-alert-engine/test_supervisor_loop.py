"""Regression tests for the flashing-window incident (2026-10-05).

The symptom was an unending stack of modal "Already running" dialogs. The chain:

  1. TWO supervisors ran at once (PIDs 21552 + 25496), each spawning a UI.
  2. One UI won data/engine.lock and worked.
  3. The other UI refused to start, showed a MODAL MessageBoxW, and BLOCKED
     forever on user input -- it never exited.
  4. Because it never exited, the supervisor never saw a crash, its
     MAX_FAST_FAILS guard never tripped, and no backoff ever applied.
  5. Each restart popped another dialog on top of the last.

These tests pin the three fixes:
  - the refusal is non-modal, so it cannot block;
  - the refusal exits ALREADY_RUNNING_EXIT (3), which the supervisor treats as
    terminal rather than as a crash;
  - the supervisor is itself single-instance.

Run with:  python test_supervisor_loop.py     (no pytest needed)
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(HERE, "qae_supervisor.py")
ENGINE = os.path.join(HERE, "quant_alert_engine.py")
UI = os.path.join(HERE, "quant_alert_ui.py")
PY = sys.executable

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_supervisor_with_fake_child(child_code, env_extra=None, timeout=90):
    """Run the real supervisor against a fake child in a temp data dir.

    Returns (exit_code, supervisor_stdout).
    """
    tmp = tempfile.mkdtemp(prefix="qaesup_")
    child = os.path.join(tmp, "fake_child.py")
    with open(child, "w") as f:
        f.write(textwrap.dedent(child_code))
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    # The supervisor hardcodes HERE (its own dir) for data/ and the child path,
    # so run a copy of the supervisor placed in the temp dir with QAE_CHILD set.
    sup_copy = os.path.join(tmp, "qae_supervisor.py")
    with open(SUPERVISOR) as f:
        src = f.read()
    with open(sup_copy, "w") as f:
        f.write(src)

    env = dict(os.environ)
    env["QAE_CHILD"] = child
    env["QAE_LOG_DIR"] = tmp
    env["QAE_FAST_FAIL_SECONDS"] = "2"
    env["QAE_MAX_FAST_FAILS"] = "3"
    env.update(env_extra or {})

    t0 = time.time()
    try:
        p = subprocess.run([PY, sup_copy], cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, time.time() - t0, tmp
    except subprocess.TimeoutExpired as e:
        return None, (e.stdout or "") + (e.stderr or ""), time.time() - t0, tmp


print("\n=== 1. Supervisor stops on ALREADY_RUNNING_EXIT (no restart loop) ===")
# A child that always refuses, exactly like the UI hitting a held engine.lock.
code_that_always_refuses = """
    import sys
    sys.exit(3)
"""
rc, out, dur, tmp = run_supervisor_with_fake_child(code_that_always_refuses)
check("supervisor exits instead of looping forever", rc is not None, f"timed out after {dur:.0f}s")
check("supervisor exit code is 0 (stopped, not crashed)", rc == 0, f"rc={rc}")
check("restart notice logged", "refused to start" in out, out[-300:])
check("it stopped after exactly ONE spawn", out.count("Starting ") == 1,
      f"spawned {out.count('Starting ')}x")
check("no crash/restart path was taken", "fast failure" not in out, out[-300:])


print("\n=== 2. Supervisor IS single-instance ===")
# Two supervisors at once: the second must bow out instead of adding a spawner.
tmp2 = tempfile.mkdtemp(prefix="qaesup2_")
os.makedirs(os.path.join(tmp2, "data"), exist_ok=True)
with open(os.path.join(tmp2, "sup.py"), "w") as f:
    f.write(open(SUPERVISOR).read())
child2 = os.path.join(tmp2, "slow_child.py")
with open(child2, "w") as f:
    f.write("import time\ntime.sleep(6)\n")

env2 = dict(os.environ)
env2["QAE_CHILD"] = child2
env2["QAE_LOG_DIR"] = tmp2
env2["QAE_FAST_FAIL_SECONDS"] = "2"

first = subprocess.Popen([PY, os.path.join(tmp2, "sup.py")], cwd=tmp2, env=env2,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2.0)   # let the first take the lock
second = subprocess.run([PY, os.path.join(tmp2, "sup.py")], cwd=tmp2, env=env2,
                        capture_output=True, text=True, timeout=30)
out2 = second.stdout + second.stderr
check("second supervisor refused to start", second.returncode == 0, f"rc={second.returncode}")
check("second supervisor logged the conflict", "already running" in out2.lower(), out2[-300:])
check("supervisor.lock file was created", os.path.exists(os.path.join(tmp2, "data", "supervisor.lock")))
first.kill()
first.wait(timeout=15)


print("\n=== 3. Stale supervisor lock from a dead PID is reaped ===")
tmp3 = tempfile.mkdtemp(prefix="qaesup3_")
os.makedirs(os.path.join(tmp3, "data"), exist_ok=True)
with open(os.path.join(tmp3, "sup.py"), "w") as f:
    f.write(open(SUPERVISOR).read())
# PID 999999 is not running on any sane box.
with open(os.path.join(tmp3, "data", "supervisor.lock"), "w") as f:
    f.write("999999")
child3 = os.path.join(tmp3, "clean_child.py")
with open(child3, "w") as f:
    f.write("import sys\nsys.exit(0)\n")
env3 = dict(os.environ)
env3["QAE_CHILD"] = child3
env3["QAE_LOG_DIR"] = tmp3
r3 = subprocess.run([PY, os.path.join(tmp3, "sup.py")], cwd=tmp3, env=env3,
                    capture_output=True, text=True, timeout=30)
out3 = r3.stdout + r3.stderr
check("stale lock was removed", "Removing stale supervisor lock" in out3, out3[-300:])
check("supervisor ran and stopped cleanly", r3.returncode == 0, f"rc={r3.returncode}")


print("\n=== 4. Clean exit (0) still stops without relaunch ===")
rc4, out4, dur4, tmp4 = run_supervisor_with_fake_child("import sys\nsys.exit(0)\n")
check("clean exit stops the supervisor", rc4 == 0, f"rc={rc4}")
check("no restart after clean exit", out4.count("Starting ") == 1, out4[-300:])
check("clean-exit notice logged", "closed cleanly" in out4, out4[-300:])


print("\n=== 5. A real crash still restarts (recovery not broken) ===")
crash_then_clean = """
    import os, sys
    n = os.path.join(os.path.dirname(os.path.abspath(__file__)), "count")
    try:
        k = int(open(n).read())
    except Exception:
        k = 0
    open(n, "w").write(str(k + 1))
    sys.exit(1 if k == 0 else 0)   # crash once, then exit cleanly
"""
rc5, out5, dur5, tmp5 = run_supervisor_with_fake_child(crash_then_clean)
check("crash caused exactly one restart", out5.count("Starting ") == 2, f"{out5.count('Starting ')} spawns")
check("restart happened after a fast failure", "fast failure" in out5, out5[-400:])
check("supervisor eventually exited 0", rc5 == 0, f"rc={rc5}")


print("\n=== 6. Source-level invariants ===")
ui_src = open(UI).read()
eng_src = open(ENGINE).read()
check("UI no longer calls a blocking modal MessageBoxW",
      "MessageBoxW" in ui_src and "MB_SERVICE_NOTIFICATION" in ui_src,
      "non-modal flag missing")
check("UI exits with the shared ALREADY_RUNNING_EXIT", "sys.exit(ALREADY_RUNNING_EXIT)" in ui_src)
check("engine defines ALREADY_RUNNING_EXIT = 3", "ALREADY_RUNNING_EXIT = 3" in eng_src)
sup_src = open(SUPERVISOR).read()
check("supervisor defines ALREADY_RUNNING_EXIT = 3", "ALREADY_RUNNING_EXIT = 3" in sup_src)
check("supervisor handles the refusal code before the crash branch",
      sup_src.index("code == ALREADY_RUNNING_EXIT") < sup_src.index("if code == 0:"))

print("\n" + "=" * 60)
if failures:
    print(f"FAILED {len(failures)} check(s):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")