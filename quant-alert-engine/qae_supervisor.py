"""
qae_supervisor.py — Supervisor for the Quant Alert Engine Pro UI.

WHY THIS EXISTS
---------------
The old `qae_supervisor.bat` restarted the UI on EVERY exit, including a clean
one:

    :loop
    python quant_alert_ui.py ...
    echo ... restarting in 3s ...
    timeout /t 3 /nobreak
    goto loop

Closing the window is a clean exit (code 0), so the supervisor relaunched the UI
three seconds later, forever. The app was literally impossible to close, and it
is launched fully detached (launch_qae.vbs, hidden window) so there was no
console to kill either. The log had 64 exits, 50 of them code 0.

Fixed policy:
  * exit code 0  -> the user closed the UI on purpose. Log it and STOP the
                    supervisor. Nothing is relaunched.
  * exit code != 0 -> genuine crash: restart after a short delay, but only up to
                    MAX_FAST_FAILS consecutive runs that died within
                    FAST_FAIL_SECONDS of starting. A config error or a missing
                    dependency otherwise produces an endless restart storm.

A stop file (`data/supervisor.stop`) also ends the loop cleanly; `stop-qae.ps1`
uses it in addition to force-killing.

Config (env vars):
  QAE_PYTHON    interpreter to run the UI with (default: the one the old
                supervisor already used, so nothing else changes)
  QAE_CHILD     child script (default: quant_alert_ui.py)
  QAE_MAX_FAST_FAILS / QAE_FAST_FAIL_SECONDS   crash-loop guard
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PYTHON = r"C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
# Logs live next to the engine; QAE_LOG_DIR redirects them (used by the tests so
# they never append to the real supervisor log).
LOGDIR = os.environ.get("QAE_LOG_DIR") or HERE
LOG = os.path.join(LOGDIR, "qae_supervisor.log")
CHILD_LOG = os.path.join(LOGDIR, "qae_stdout.log")
CHILD_ERR = os.path.join(LOGDIR, "qae_stderr.log")
STOP_FILE = os.path.join(HERE, "data", "supervisor.stop")

RESTART_DELAY_S = 3.0


def log(msg: str):
    line = f"[{time.strftime('%a %d/%m/%Y %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    python = os.environ.get("QAE_PYTHON") or DEFAULT_PYTHON
    child = os.environ.get("QAE_CHILD") or os.path.join(HERE, "quant_alert_ui.py")
    max_fast = int(os.environ.get("QAE_MAX_FAST_FAILS", "5"))
    fast_s = float(os.environ.get("QAE_FAST_FAIL_SECONDS", "30"))

    if not os.path.exists(python):
        log(f"FATAL: interpreter not found: {python}")
        return 2
    if not os.path.exists(child):
        log(f"FATAL: child script not found: {child}")
        return 2

    log(f"QAE supervisor started (child={os.path.basename(child)}) "
        f"clean exit stops the loop; restart only on crash "
        f"(guard: {max_fast} failures within {fast_s:.0f}s)")

    fast_fails = 0
    spawns = 0
    proc: subprocess.Popen | None = None

    def _terminate_child(*_):
        if proc and proc.poll() is None:
            log("Stop requested — terminating the UI")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    signal.signal(signal.SIGINT, _terminate_child)
    signal.signal(signal.SIGTERM, _terminate_child)

    while True:
        if os.path.exists(STOP_FILE):
            log("Stop file present — supervisor exiting")
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
            return 0

        spawns += 1
        log(f"Starting {os.path.basename(child)} (attempt {spawns})")
        started = time.time()
        mode = "ab" if os.path.exists(CHILD_LOG) else "wb"
        try:
            with open(CHILD_LOG, mode) as out, open(CHILD_ERR, "ab") as err:
                proc = subprocess.Popen([python, child], cwd=HERE,
                                        stdout=out, stderr=err)
                code = proc.wait()
        except KeyboardInterrupt:
            _terminate_child()
            log("Supervisor interrupted — exiting")
            return 0
        except OSError as e:
            log(f"Could not start the UI: {e}")
            code = 1

        ran = time.time() - started

        # -- clean exit: the user closed the window. THIS is the fix. --
        if code == 0:
            log("UI closed cleanly (exit code 0) — supervisor stopping, "
                "nothing will be relaunched")
            return 0

        fast = ran < fast_s
        fast_fails = fast_fails + 1 if fast else 0
        log(f"UI crashed (exit code {code}) after {ran:.0f}s — "
            f"{'fast failure' if fast else 'ran normally'} "
            f"({fast_fails}/{max_fast})")

        if fast_fails >= max_fast:
            log(f"Giving up after {fast_fails} consecutive fast failures — "
                f"fix the UI, then start it again. Not restarting.")
            return 1

        time.sleep(RESTART_DELAY_S)


if __name__ == "__main__":
    sys.exit(main())
