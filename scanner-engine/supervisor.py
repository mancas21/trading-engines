"""Supervisor — restart the scanner on crash, never on a clean exit.

THE TWO FAILURE MODES THIS EXISTS TO PREVENT
=============================================

1. THE FLASHING "ALREADY RUNNING" DIALOG.
   The sibling engine's supervisor crash-looped `quant_alert_ui.py`, spawning
   a SECOND engine on every attempt. Each second instance hit the UI's
   single-instance lock, popped a modal, and was killed — so the modal flashed
   forever. The supervisor's own log showed skipping attempt numbers
   (1, 2, 4, 5, 6, 7), which was the only clue.

   The fix has two parts, and BOTH are required:
     * the supervisor takes the instance lock ITSELF, before launching a child,
       so a second supervisor exits 0 without spawning anything;
     * restart is bounded (`max_attempts`) so a permanent crash becomes a
       visible stopped state rather than an infinite spawn loop.

2. CLEAN EXIT TREATED AS A FAILURE.
   The sibling relaunched on any exit, so deliberately stopping the engine
   brought it back. `should_restart(returncode=0)` is False and the supervisor
   exits 0 with the lock released.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/supervisor.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_S = 3.0
# Never read a stale lock held by a PID that no longer exists.
STALE_LOCK_GRACE_S = 0.0

# Exit code reported when the watchdog kills a hung child. Module level so it
# can be imported and asserted on: it must be NON-ZERO, or should_restart()
# would read a hang as a clean exit and refuse to relaunch.
WATCHDOG_KILLED = -9

# Default watchdog deadline for the child.
#
# MEASURED, and the first value was wrong. A poll-only cycle takes ~21s, so
# 300s looked generous. It is not: the real cycle writes 8 rows then runs the
# outcome resolver, which issues one HTTP request per row per venue. Measured
# cycle-to-cycle interval with the resolver attached: median 422s, max 583s.
# A 300s watchdog therefore killed HEALTHY scanners -- verified live, it fired
# on a scanner that was polling normally, and the log shows the cycle being
# cut short at exactly 300s.
#
# The deadline must clear the SLOWEST legitimate cycle with headroom. 1200s
# (~20 min) is roughly 2x the observed worst case, which still catches a true
# wedge (the original bug ran past 300s with zero rows written, and a genuine
# hang is unbounded) without punishing a slow-but-working scanner.
WATCHDOG_S = 1200.0

# Which lock paths THIS process has claimed. The PID file alone cannot express
# "a second lock object in the same interpreter must be refused", because the
# pid it holds is our own and therefore always alive.
_CLAIMED: dict[str, int] = {}


def should_restart(returncode: int | None) -> bool:
    """Restart on a crash. `returncode is None` means the child was killed by
    a signal, which is a crash. `0` is a clean exit and must be respected."""
    return returncode != 0


class InstanceLock:
    """A PID file that is exclusive across processes and reclaimable when the
    holder is dead.

    Reclaimability matters: if the supervisor is killed hard, the PID file
    remains, and a supervisor that refuses to start because of a dead
    holder's file is indistinguishable from one that is wedged.
    """

    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self._held = False

    def _read_pid(self) -> int | None:
        try:
            txt = self.path.read_text(encoding="utf-8").strip()
            return int(txt.split()[0]) if txt else None
        except (FileNotFoundError, ValueError, IndexError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
            """Is `pid` a live process?

            POSIX `os.kill(pid, 0)` is NOT portable here: on Windows it raises
            `OSError: [WinError 87] El parametro no es correcto` for an arbitrary
            pid, which would have made every lock look like a dead holder and let a
            second supervisor claim it. Use OpenProcess on Windows and fall back to
            os.kill elsewhere.
            """
            if pid <= 0:
                return False
            if pid == os.getpid():
                return True

            if sys.platform == "win32":
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                try:
                    import ctypes
                    k32 = ctypes.windll.kernel32          # noqa: N806
                    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if not h:
                        # ERROR_ACCESS_DENIED (5) means it exists but is ours to
                        # deny; anything else means it is not there.
                        return k32.GetLastError() == 5
                    try:
                        code = ctypes.c_ulong()
                        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                            return True
                        return code.value == STILL_ACTIVE
                    finally:
                        k32.CloseHandle(h)
                except Exception:                              # noqa: BLE001
                    return False
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError):
                return False
            except PermissionError:
                # exists but owned by someone else -> treat as alive
                return True
            return True

    def write_pid(self, pid: int | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{pid if pid is not None else os.getpid()}\n",
                             encoding="utf-8")

    def acquire(self) -> bool:
        if self._held:
            return True
        # A second InstanceLock object on the SAME path inside THIS process must
        # be refused, or the exclusivity the supervisor depends on is a fiction.
        # The original code let it through: `if holder != os.getpid()` was meant
        # to let the supervisor reclaim its own lock, but it also let a fresh
        # object claim a lock the current process already owned.
        #
        # The stale-holder case is different and MUST still reclaim: a dead pid
        # makes the lock garbage regardless of any in-process claim. So the pid
        # liveness is tested FIRST, then the in-process claim.
        holder = self._read_pid()
        holder_alive = holder is not None and self._alive(holder)
        if holder_alive and holder != os.getpid():
            return False                      # another live process holds it
        if holder_alive and _CLAIMED.get(str(self.path)) is not None:
            return False                      # this process already holds it
        # no holder, dead holder, or only our own stale claim -> reclaim
        # free, stale, or already ours -> (re)claim it
        try:
            self.write_pid()
            _CLAIMED[str(self.path)] = os.getpid()
            self._held = True
            return True
        except OSError:
            return False

    def release(self) -> None:
        if not self._held:
            return
        # only remove the file if it is still ours
        if self._read_pid() == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        _CLAIMED.pop(str(self.path), None)
        self._held = False


class Supervisor:
    # A child that hangs must be a CRASH, not silence. Measured: a cycle wedged
    # in the outcome resolver held the scanner alive with no new rows for 312s
    # while every venue aged past max_age_s, and `subprocess.run` with no
    # timeout waited forever, so the restart policy never engaged. A watchdog
    # deadline turns a hang into exit != 0, which the existing policy already
    # knows how to handle.
    def __init__(self, command: list[str], lock_path: pathlib.Path | str,
                 log_path: pathlib.Path | str | None = None,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_s: float = DEFAULT_BACKOFF_S,
                 watchdog_s: float | None = None):
        self.command = list(command)
        self.lock = InstanceLock(lock_path)
        self.log_path = pathlib.Path(log_path) if log_path else \
            HERE / "data" / "supervisor.log"
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.watchdog_s = WATCHDOG_S if watchdog_s is None else watchdog_s
        self.attempts = 0

    # ── policy ─────────────────────────────────────────────────────────────

    def should_continue(self, attempt: int, returncode: int | None) -> bool:
        if not should_restart(returncode):
            return False
        return attempt < self.max_attempts

    # ── logging ────────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}  {msg}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    def _run_with_watchdog(self):
        """Run the child, killing it if it outlives the watchdog.

        `subprocess.run` has no timeout, so a wedged child looks identical to a
        healthy one from the supervisor's side: no exit, no output, no restart.
        Measured -- the scanner sat for 312s with zero new rows while every
        venue aged past max_age_s, and the restart policy never engaged.

        Killing the whole process group matters: the child may have spawned
        helpers, and killing only the parent would leave them holding the port.
        """
        import subprocess as sp
        kwargs = dict(stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                      errors="replace")
        if sys.platform == "win32":
            flags = getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = flags
        proc = sp.Popen(self.command, **kwargs)
        try:
            out, _ = proc.communicate(timeout=self.watchdog_s)
        except sp.TimeoutExpired:
            self._kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=15)
            except Exception:                               # noqa: BLE001
                out = ""
            done = sp.CompletedProcess(self.command, WATCHDOG_KILLED,
                                       out or "", None)
            return done
        return sp.CompletedProcess(self.command, proc.returncode, out or "", None)

    @staticmethod
    def _kill_tree(proc) -> None:
        import subprocess as sp
        try:
            if sys.platform == "win32":
                sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=20)
            else:
                import os as _os, signal as _sig
                _os.killpg(_os.getpgid(proc.pid), _sig.SIGKILL)
        except Exception:                                   # noqa: BLE001
            try:
                proc.kill()
            except Exception:                               # noqa: BLE001
                pass

    # ── the loop ───────────────────────────────────────────────────────────

    def run(self) -> int:
        # The lock is taken BEFORE any child exists. Without this, a second
        # supervisor spawns a competing child and the modal-flash loop begins.
        if not self.lock.acquire():
            self.log("another supervisor already holds the lock; exiting 0 "
                     "without launching a child")
            return 0

        self.log(f"supervisor start pid={os.getpid()} cmd={' '.join(self.command)} "
                 f"max_attempts={self.max_attempts}")
        last_rc: int | None = None
        try:
            while self.attempts < self.max_attempts:
                self.attempts += 1
                self.log(f"attempt {self.attempts}/{self.max_attempts} "
                         f"launching: {' '.join(self.command)}")
                try:
                    if self.watchdog_s and self.watchdog_s > 0:
                        proc = self._run_with_watchdog()
                    else:
                        proc = subprocess.run(
                            self.command, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace",
                        )
                    last_rc = proc.returncode
                    out = (proc.stdout or "").strip()
                    if out:
                        for line in out.splitlines()[-20:]:
                            self.log(f"  | {line}")
                    if last_rc == WATCHDOG_KILLED:
                        self.log(f"  ! child exceeded the {self.watchdog_s:.0f}s "
                                 f"watchdog and was killed -- treating as a crash")
                except Exception as e:                        # noqa: BLE001
                    last_rc = None
                    self.log(f"  ! failed to launch: {type(e).__name__}: {e}")

                self.log(f"attempt {self.attempts} exit={last_rc}")

                if not self.should_continue(self.attempts, last_rc):
                    break

                if self.backoff_s:
                    time.sleep(self.backoff_s)
        finally:
            self.lock.release()

        if last_rc == 0:
            self.log("child exited cleanly; not relaunching")
            return 0

        if self.attempts >= self.max_attempts and should_restart(last_rc):
            self.log(f"CRASH LOOP EXHAUSTED after {self.attempts} attempts "
                     f"(last exit={last_rc}). Not relaunching.")
            return 1

        self.log(f"supervisor exit last_rc={last_rc} attempts={self.attempts}")
        return 0


def main() -> int:
    import argparse
    # A supervised command arrives as raw argv, not through argparse.
    #
    # `add_argument("--", nargs=REMAINDER)` does NOT work here: argparse rejects
    # the bare "--" separator and exits with
    #   error: unrecognized arguments: -- python.exe scanner.py
    # so the supervisor never started. Everything after a literal "--" on the
    # command line is the child command, and argparse only ever sees the flags
    # before it.
    argv = sys.argv[1:]
    sep = argv.index("--") if "--" in argv else len(argv)
    my_flags, child_cmd = argv[:sep], argv[sep + 1:]

    ap = argparse.ArgumentParser(description="scanner-engine supervisor")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF_S)
    ap.add_argument("--lock", default=str(HERE / "data" / "scanner.lock"))
    ap.add_argument("--log", default=str(HERE / "data" / "supervisor.log"))
    args = ap.parse_args(my_flags)

    cmd = child_cmd or [sys.executable, str(HERE / "scanner.py")]

    sup = Supervisor(command=cmd, lock_path=args.lock, log_path=args.log,
                     max_attempts=args.max_attempts, backoff_s=args.backoff)
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())