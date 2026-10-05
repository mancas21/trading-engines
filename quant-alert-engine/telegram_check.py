"""Verify the revised periodic-snapshot alert end to end.

Runs the REAL production assembly (`send_periodic_snapshot`) with the engine's own
sender replaced by a capture, so what you see below is exactly what the phone gets:
one full block for the symbol ATAS is charting, one compact line per other symbol.

  --send   additionally post the captured message(s) for real

Checks first: the guard that used to make this method a no-op.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "quant_alert_engine.py")
spec = importlib.util.spec_from_file_location("qae", P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

cfg = m.load_config("config.json")
ps_attr = getattr(cfg, "periodic_snapshot", None)
ps_get = cfg.get("periodic_snapshot")
print(f"config type                        : {type(cfg).__name__}")
print(f"getattr(cfg,'periodic_snapshot')   -> {ps_attr!r}   <-- old code read it this way")
print(f"cfg.get('periodic_snapshot')       -> {type(ps_get).__name__}"
      f"(enabled={getattr(ps_get, 'enabled', None)},"
      f" interval={getattr(ps_get, 'interval_seconds', None)},"
      f" symbols={len(getattr(ps_get, 'symbols', []) or [])})")
print()

cls = next(getattr(m, n) for n in dir(m)
           if isinstance(getattr(m, n), type) and hasattr(getattr(m, n), "_build_snapshot"))
eng = object.__new__(cls)
eng.config = cfg

at = eng._snapshot_atas_symbol(getattr(ps_get, "symbols", []))
print(f"ATAS is currently charting         : {at}  (full block for this one)")
print()

real_send = cls._send_telegram.__get__(eng)
captured = []


def capture(msg):
    captured.append(msg)
    print("=== MESSAGE " + str(len(captured)) + f" ({len(msg)} chars) ===")
    print(msg)
    print()
    return True


eng._send_telegram = capture
eng.send_periodic_snapshot()

if "--send" in sys.argv:
    for i, msg in enumerate(captured, 1):
        print(f"telegram send {i} -> {real_send(msg)}")
else:
    print("(dry run; pass --send to post the same message)")
