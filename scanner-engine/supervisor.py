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

Run:  python scanner-engine/supervisor.py
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
# 2026-10-15 — aqui vivia `STALE_LOCK_GRACE_S = 0.0`, con un comentario
# ("Never read a stale lock held by a PID that no longer exists") que describia
# una politica que NADIE implementaba con la constante: no tenia un solo lector
# en todo el arbol. La politica SI se aplica — `InstanceLock.acquire` consulta
# `_alive(pid)` y reclama el fichero si el titular murio — pero con la funcion,
# no con un plazo configurable. Se quita para no hacer creer que existe.

# Exit code reported when the watchdog kills a hung child. Module level so it
# can be imported and asserted on: it must be NON-ZERO, or should_restart()
# would read a hang as a clean exit and refuse to relaunch.
WATCHDOG_KILLED = -9

# STALL limit, not a runtime limit.
#
# TWO earlier values were both wrong, in the same way. A "kill after N
# seconds" rule cannot supervise a daemon: `scanner.py` runs run_forever(), an
# infinite loop, so EVERY child eventually exceeds any runtime bound. 300s
# killed healthy scanners; raising it to 1200s only made the kill cycle
# longer. Measured from the log: "exceeded the 1200s watchdog" three times,
# exactly 1200s apart, each terminating a scanner that was writing rows
# normally.
#
# The correct signal is PROGRESS. The scanner writes 8 rows per cycle, so the
# SQLite file's mtime advances while it works and stops the instant it wedges.
# Measured worst-case cycle is ~583s, so 900s of true silence means wedged,
# while a slow-but-working cycle is never punished.
STALL_S = 900.0

# 2026-10-15 — `_CLAIMED` se elimina: era un dict de "rutas de lock que YO he
# reclamado" que se declaraba, se escribia al adquirir y se borraba al soltar, y
# que NADIE LEIA en ningun sitio. El comentario decia que servia para rechazar
# "un segundo lock en el mismo interprete", pero esa proteccion la da la
# comprobacion del PID del fichero (`acquire` rechaza cuando el pid leido es el
# nuestro, que es exactamente el caso del segundo objeto en el mismo proceso).
# O sea: la proteccion existia por otra via y el dict solo acumulaba estado sin
# consumidor. Se quita el dict y se deja la explicacion donde corresponde.


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

        # ATOMIC claim via O_CREAT|O_EXCL, then liveness check.
        #
        # The previous version was a TOCTOU race: read the pid, test liveness,
        # then write. Two supervisors starting within the same second both read
        # "no lock", both wrote, and BOTH ran -- verified live, two supervisor
        # trees and two scanner children on one database. The lock is the only
        # thing preventing the duplicate-spawn class of failure (the flashing
        # "Already running" dialog in the sibling engine), so it has to be
        # atomic, not sequential.
        #
        # O_EXCL makes the create itself the claim: exactly one caller can win,
        # even across processes. A stale file is handled after the fact by
        # checking whether the pid inside is still alive.
        self.path.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(2):                     # one retry after clearing stale
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read_pid()
                if holder is not None and holder != os.getpid() and self._alive(holder):
                    return False               # a live process holds it
                if holder == os.getpid():
                    # The file says OUR pid. Either another InstanceLock object
                    # in this process holds it (must be refused -- that is the
                    # exclusivity the supervisor depends on) or this object
                    # already holds it, which the `self._held` check above
                    # already returned True for. So: refuse.
                    return False
                # dead holder, unreadable file, or same-process re-claim: clear it
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            else:
                try:
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)
                self._held = True
                return True

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
                 stall_s: float | None = None,
                 progress_marker=None,
                 poll_s: float = 10.0):
        self.command = list(command)
        self.lock = InstanceLock(lock_path)
        self.log_path = pathlib.Path(log_path) if log_path else \
            HERE / "data" / "supervisor.log"
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        # STALL, not runtime. See _run_with_watchdog.
        self.stall_s = STALL_S if stall_s is None else stall_s
        self.progress_marker = progress_marker
        self._poll_s = poll_s
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
        """Run the child, killing it only if it STOPS MAKING PROGRESS.

        A RUNTIME watchdog is wrong for this child, and both earlier versions
        were runtime-based. `scanner.py` calls `run_forever()` -- an infinite
        loop that never exits -- so any "kill after N seconds" rule kills a
        perfectly healthy scanner, always. Measured: the log shows
        "exceeded the 1200s watchdog" three times, exactly 1200s apart, each
        one terminating a scanner that was writing rows normally.

        Progress is the right signal. The scanner writes 8 rows every cycle to
        SQLite, so the database file's mtime advances continuously while it
        works and stops the moment it wedges. `stall_s` therefore means "no
        progress for this long", not "ran for this long".

        A child that has genuinely wedged stops touching the DB, which is the
        condition the original bug produced (312s with zero new rows while
        every venue aged past max_age_s).
        """
        import subprocess as sp
        kwargs = dict(stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                      errors="replace")
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)

        marker_fn = self.progress_marker or (lambda: 0.0)
        proc = sp.Popen(self.command, **kwargs)
        last_marker = marker_fn()
        last_progress = time.time()
        killed = False

        while True:
            try:
                out, _ = proc.communicate(timeout=self._poll_s)
            except sp.TimeoutExpired:
                now = time.time()
                # the marker moves on its own timeout; re-read it each poll
                try:
                    cur = marker_fn()
                except Exception:                        # noqa: BLE001
                    cur = last_marker
                if cur != last_marker:
                    last_marker = cur
                    last_progress = now
                elif self.stall_s and (now - last_progress) > self.stall_s:
                    self.log(f"  ! no progress for {now - last_progress:.0f}s "
                             f"(stall limit {self.stall_s:.0f}s) -- killing")
                    self._kill_tree(proc)
                    try:
                        out, _ = proc.communicate(timeout=15)
                    except Exception:                    # noqa: BLE001
                        out = ""
                    killed = True
                    break
                continue
            break

        rc = WATCHDOG_KILLED if killed else proc.returncode
        return sp.CompletedProcess(self.command, rc, out or "", None)

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
                    if self.stall_s and self.stall_s > 0:
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
                        self.log("  ! child stalled and was killed -- "
                                 "treating as a crash")
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
    ap.add_argument("--stall", type=float, default=STALL_S,
                    help="kill the child if it makes no progress for N seconds")
    args = ap.parse_args(my_flags)

    cmd = child_cmd or [sys.executable, str(HERE / "scanner.py")]

    # HARD GUARD: a supervisor must never supervise another supervisor.
    #
    # Observed live: two supervisor processes and two scanners appeared, one
    # supervisor running under the venv python and one under the SYSTEM python,
    # the second being the child of the first. A nesting supervisor multiplies
    # the lock's failure mode instead of being caught by it -- the inner one
    # takes the lock, the outer one keeps spawning, and the duplicate is
    # invisible because only the inner one logs.
    #
    # Whatever produced that chain, the supervisor refuses to reproduce it.
    if any("supervisor.py" in str(a).lower() for a in cmd):
        print("refusing to supervise another supervisor: "
              + " ".join(str(c) for c in cmd), file=sys.stderr)
        return 2

    # Progress signal: the scanner writes rows every cycle, so the SQLite
    # file's mtime advances while it works. Independent of the child's own
    # HTTP surface, which may itself be what is wedged.
    db_path = HERE / "data" / "scanner.db"

    def _db_progress() -> float:
        try:
            return db_path.stat().st_mtime
        except OSError:
            return 0.0

    sup = Supervisor(command=cmd, lock_path=args.lock, log_path=args.log,
                     max_attempts=args.max_attempts, backoff_s=args.backoff,
                     stall_s=args.stall, progress_marker=_db_progress)
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())