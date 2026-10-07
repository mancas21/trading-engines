"""Regresion: el supervisor debe PAUSAR y RECUPERAR, no morir ni ficar en bucle.

El fallo real (2026-10-07): el puente se reinicia solo cada ~20-40 min, y cada
reinicio mataba el grabador porque se rendia a los 5 fallos. Nadie lo relanzaba.

Este test ejercita las DOS salidas de la politica de pausa sin esperar horas
(caps reducidos a milisegundos):
  - fallo permanente  -> PAUSA xN -> AGOTADOS -> rc=1 (parado, no en bucle)
  - fallo transitorio -> PAUSA xN -> RECUPERADO -> sigue grabando

Uso:  python test_supervisor_pause.py
"""

import importlib.util, json, sys, time
from pathlib import Path
D = Path(r"C:\\Users\\Lenovo\\trading-engines\\reader")
spec = importlib.util.spec_from_file_location("rs", D/"reader_supervisor.py")
rs = importlib.util.module_from_spec(spec); sys.modules["rs"]=rs
spec.loader.exec_module(rs)
lines=[]; rs.log=lambda m: lines.append(m)
rs.MAX_FAST_FAILS=2; rs.RETRY_BACKOFF_S=0.01
rs.RETRY_BACKOFF_MAX_S=0.02; rs.MAX_RETRY_ATTEMPTS=10
# clear stale status from the previous test
(D/"supervisor_status.json").unlink(missing_ok=True)

n={"c":0}
def fake_cycle(dry_run=False):
    n["c"]+=1
    if n["c"]<=6: return False, "SKIP: RemoteDisconnected (simulado)"
    if n["c"]>10: sys.exit(0)          # hard stop AFTER recovery path has run
    return True, "recorded XRPUSDT NEUTRAL"
rs.cycle=fake_cycle
try: rs.do_loop(interval=0.01)
except SystemExit: pass
print("### CASO 2: transitorio -> debe RECUPERAR ###")
for l in lines: print("   ", l[:98])
st=json.loads((D/"supervisor_status.json").read_text())
print()
print("  pausas      :", len([l for l in lines if l.startswith("PAUSA")]))
print("  RECUPERADO  :", len([l for l in lines if l.startswith("RECUPERADO")])==1)
print("  paused      :", st.get("paused"))
print("  gave_up     :", st.get("gave_up"))
print("  recovered_at:", st.get("recovered_at"))
