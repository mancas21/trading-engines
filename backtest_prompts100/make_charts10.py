"""Genera charts individuales para el top-10 y los deja en charts/."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

WORK = Path(__file__).parent
CH = WORK / "charts"
CH.mkdir(exist_ok=True)

df = pd.read_csv(WORK / "results_100.csv")
n11 = df[np.isclose(df.cost_bps, 0.0011)].copy()
eqs = np.load(WORK / "eqs.npy", allow_pickle=True)
eqmap = {}
for (no, c), v in eqs:
    if np.isclose(c, 0.0011):
        eqmap[int(no)] = v

TOP10 = [60, 44, 73, 71, 28, 56, 95, 26, 34, 59]

for no in TOP10:
    r = n11[n11["no"] == no].iloc[0]
    eq = np.asarray(eqmap[int(no)], dtype=float)
    x = np.arange(len(eq))
    sp = int(len(eq) * 0.7)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, eq, lw=0.9, color="tab:blue", label="equity")
    ax.axvline(sp, color="red", ls="--", lw=1.2, label="split IS/OOS (70%)")
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    ax2 = ax.twinx()
    ax2.fill_between(x, dd * 100, 0, color="salmon", alpha=0.35)
    ax2.set_ylabel("drawdown %", color="salmon")
    ax2.set_ylim(dd.min() * 130, 1)
    ax.set_yscale("log")
    ax.set_xlabel("barras")
    ax.set_title(f"#{no} {r['name']} — {r['sym']} {r['tf']} · coste 11 bps · "
                 f"net {r['net']*100:.0f}% · OOS PF {r['oos_pf']:.2f} (n={int(r['oos_n'])})")
    ax.legend(loc="upper left", fontsize=8)
    plt.tight_layout()
    plt.savefig(CH / f"top10_{no:03d}.png", dpi=110)
    plt.close()
print("charts top10 listos")
