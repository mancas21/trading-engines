"""Fail if any other copy of the card toolchain has drifted from card_live/.

Why: live_card.py / live_read.py existed in BOTH quant-alert-engine/card_live/ and
Downloads/trading-engines-master/live-analysis/. They diverged (the P3 edit landed in
the repo copy only, dropping the tape USD, the book depth, the spread, the R1/R10 rule
labels, the thinnest-book line and the freshest-alerts line, and adding a real
correlation instead of the hardcoded 60.4/1.65). The card you get depended on which
directory the caller ran from — the same defect class as two copies of the plan builder.

The repo copy is canonical: quant-alert-engine/card_alert.py is what the Telegram
snapshot path imports, and the chat path must use the same one.

  python check_drift.py          # verify all mirrors match
  python check_drift.py --sync   # copy repo -> mirror(s) first
"""
import filecmp
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANON = os.path.join(HERE, "card_live")
MIRRORS = [r"C:\Users\Lenovo\Downloads\trading-engines-master\live-analysis"]
FILES = ["live_card.py", "live_read.py", "fetch_live.py"]


def sync():
    for m in MIRRORS:
        if not os.path.isdir(m):
            print(f"skip (missing): {m}")
            continue
        for f in FILES:
            src, dst = os.path.join(CANON, f), os.path.join(m, f)
            shutil.copy2(src, dst)
            print(f"synced {f} -> {m}")


def check():
    bad = 0
    for m in MIRRORS:
        if not os.path.isdir(m):
            print(f"skip (missing): {m}")
            continue
        for f in FILES:
            a, b = os.path.join(CANON, f), os.path.join(m, f)
            if not os.path.exists(b):
                print(f"DRIFT  {f}: mirror missing at {b}")
                bad += 1
                continue
            # ignore CRLF vs LF when comparing content
            if filecmp.cmp(a, b, shallow=False):
                print(f"ok     {f} == {m}")
            else:
                da = open(a, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
                db = open(b, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
                if da == db:
                    print(f"ok     {f} == {m} (line endings differ only)")
                else:
                    print(f"DRIFT  {f}: {m} differs from canonical")
                    bad += 1
    if bad:
        print(f"\n{bad} file(s) out of sync — run: python check_drift.py --sync")
    else:
        print("\nall copies in sync")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--sync" in sys.argv:
        sync()
    sys.exit(check())
