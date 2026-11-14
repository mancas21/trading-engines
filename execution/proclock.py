"""Lock por PID para los procesos que pueden enviar ordenes.

Un solo auto-router y un solo reconciliador por cuenta. Si el PID del lock ya
no existe (proceso muerto), el lock se toma: no hay que borrarlo a mano.

audit 2.6: pid_alive usaba `os.kill(pid, 0)` sin rama Windows. Probado en la
maquina objetivo (Windows, CPython 3.12.9): el proceso NO muere, os.kill(pid,0)
es un chequeo de existencia valido ahi. Aun asi se unifica con la rama ctypes
de trigger_entry.py: OpenProcess+GetExitCodeProcess distingue bien "existe" de
"sin permiso" (os.kill confunde EPERM con proceso muerto) y las dos
implementaciones dejan de discrepar.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _path(name: str) -> Path:
    return STATE / f"{name}.lock"


def acquire(name: str) -> bool:
    p = _path(name)
    p.parent.mkdir(exist_ok=True)
    if p.is_file():
        try:
            pid = int(p.read_text().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and pid_alive(pid):
            return False
    p.write_text(str(os.getpid()))
    return True


def release(name: str) -> None:
    p = _path(name)
    try:
        if p.is_file() and p.read_text().strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


def holder(name: str) -> int:
    p = _path(name)
    try:
        pid = int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0
    return pid if pid_alive(pid) else 0
