"""Lock por PID para los procesos que pueden enviar ordenes.

Un solo auto-router y un solo reconciliador por cuenta. Si el PID del lock ya
no existe (proceso muerto), el lock se toma: no hay que borrarlo a mano.
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
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
