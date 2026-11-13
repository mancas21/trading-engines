"""Genera charts de los supervivientes + REPORT.md."""
from __future__ import annotations

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

TOP = [60, 44, 73, 71, 28, 83]

fig, axes = plt.subplots(2, 3, figsize=(16, 9))
for ax, no in zip(axes.ravel(), TOP):
    r = n11[n11["no"] == no].iloc[0]
    eq = np.asarray(eqmap[int(no)], dtype=float)
    x = np.arange(len(eq))
    sp = int(len(eq) * 0.7)
    ax.plot(x, eq, lw=0.9)
    ax.axvline(sp, color="red", ls="--", lw=1, label="split IS/OOS")
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    ax2 = ax.twinx()
    ax2.fill_between(x, dd * 100, 0, color="salmon", alpha=0.35)
    ax2.set_ylim(dd.min() * 130, 1)
    ax2.set_ylabel("DD %", color="salmon")
    ax.set_yscale("log")
    ax.set_title(f"#{no} {r['name']} ({r['sym']} {r['tf']})\n"
                 f"net {r['net']*100:.0f}% · OOS PF {r['oos_pf']:.2f} (n={int(r['oos_n'])})",
                 fontsize=10)
    ax.legend(fontsize=8, loc="upper left")
plt.suptitle("Supervivientes — equity (log) + drawdown, coste 11 bps, 70/30 IS/OOS")
plt.tight_layout()
plt.savefig(CH / "top_survivors.png", dpi=110)
plt.close()

fig, ax = plt.subplots(figsize=(11, 7))
sub = n11[n11.oos_n >= 5].sort_values("oos_pf")
colors = {"trend": "tab:blue", "mean_reversion": "tab:red", "breakout": "tab:green",
          "session": "tab:orange", "funding": "tab:purple", "pairs": "tab:brown",
          "cross_sectional": "tab:cyan"}
ax.scatter(sub.oos_pf.clip(-1, 4), sub.oos_n, s=28,
           c=[colors.get(g, "gray") for g in sub.group], alpha=0.75)
ax.axvline(1.2, color="red", ls="--", lw=1)
ax.axvline(1.0, color="gray", ls=":", lw=1)
ax.set_xscale("symlog", linthresh=1)
ax.set_xlabel("OOS profit factor (11 bps)")
ax.set_ylabel("trades OOS")
ax.set_title("Las 100 estrategias: OOS PF vs nº de trades OOS")
from matplotlib.lines import Line2D
ax.legend(handles=[Line2D([], [], marker="o", ls="", color=c, label=g)
                   for g, c in colors.items()], fontsize=8)
for _, r in sub.iterrows():
    if r["oos_pf"] > 1.25 and r["oos_n"] >= 25:
        ax.annotate(f"#{int(r['no'])}", (r.oos_pf, r.oos_n), fontsize=8)
plt.tight_layout()
plt.savefig(CH / "oos_map.png", dpi=110)
plt.close()

# ---------------- REPORT.md
def pct(x):
    return f"{x*100:.1f}%"


lines = []
lines.append("# Criba rápida — 100 prompts de estrategia (Binance USDT-M perps)\n")
lines.append("**Fecha:** 2026-10-29 · **Motor:** `backtest_prompts100/` (trading-engines) · "
             "**Datos:** backtest-system (read-only) + rellenos JUP 1h/15m y cola HYPE 1h por API fapi\n")
lines.append("## Convenciones\n")
lines.append("- Señal en barra cerrada, fill en la apertura siguiente; el stop gana empates.")
lines.append("- Riesgo 1% de equity por trade desde la distancia del stop (ATR); compuesto.")
lines.append("- Costes: **11 bps roundtrip** (modelo del repo: 5 fee + 0.5 slip por lado) y "
             "**22 bps** como sensibilidad.")
lines.append("- Split **70/30 IS/OOS** por nº de barras. Datos: 1h desde listing "
             "(BTC 2019, SOL 2020, SUI 2023, HYPE 2025-05, JUP 2024-01), 15m/5m desde 2019-2024 "
             "según par, hasta 2026-10-15/27.")
lines.append("- Gates del repo aplicados como criba: PF ex-cost > 1.2 · n ≥ 30 · net > 0 · "
             "exceso vs buy & hold reportado.\n")
lines.append("## Veredicto global\n")
gate = n11[n11.gate]
oos_ok = n11[n11.gate & n11.oos_ok]
n22 = df[np.isclose(df.cost_bps, 0.0022)].set_index("no")
strict11 = []
for _, r in n11.iterrows():
    if r.gate and r.oos_ok:
        strict11.append(int(r["no"]))
sobrev22 = [no for no in strict11
            if (n22.loc[no]["oos_pf"] > 1.2 and n22.loc[no]["oos_net"] > 0
                and n22.loc[no]["oos_n"] >= 30)]
lines.append(f"**Coste oficial del repo: 11 bps roundtrip** (5 bps fee + 0.5 bps slip por "
             f"lado, medido en libro vivo 2026-10-25; 22 bps es solo el preset viejo, se "
             f"reporta como sensibilidad y NO decide el veredicto).\n")
lines.append(f"De **100 estrategias**, con el coste oficial de 11 bps: **{len(gate)} pasan "
             f"el gate completo** (PF>1.2, n≥30, net>0 en ventana completa) y **{len(oos_ok)} "
             f"lo mantienen en OOS** (PF>1.2, n≥30, net>0): {strict11}. De esos, {len(sobrev22)} "
             f"también sobreviven a la sensibilidad de 22 bps: {sobrev22}.\n")
lines.append("Mediana de OOS PF por grupo: mean_reversion 0.65 (8% con OOS positivo), "
             "pairs 0.18, funding 0.53, breakout 0.58, trend 0.69, session 0.78, "
             "cross_sectional 1.22. **La reversión a la media a estos timeframes y con estos "
             "stops está muerta** — consistente con el veredicto v3 del repo (edge bruto "
             "cero, coste-motivado).\n")
lines.append("## Estrategias que sobreviven (gate completo + OOS, coste oficial 11 bps)\n")
lines.append("| # | Estrategia | Par | TF | Net | PF | n | DD | OOS PF | OOS n | OOS net | "
             "Exceso OOS vs B&H | ¿Sobrevive a 22 bps? |")
lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for no in strict11:
    r = n11[n11["no"] == no].iloc[0]
    ok22 = "sí" if no in sobrev22 else "no"
    lines.append(f"| {no} | {r['name']} | {r['sym']} | {r['tf']} | {pct(r.net)} | "
                 f"{r.pf:.2f} | {int(r.n)} | {pct(r.mdd)} | {r.oos_pf:.2f} | "
                 f"{int(r.oos_n)} | {pct(r.oos_net)} | {r.oos_excess*100:+.1f}pp | {ok22} |")
lines.append("")
lines.append("Cerca pero sin gate OOS (por n o PF): **83 Weekly basket rotation** "
             "(OOS PF 1.56, n=22<30), **95 Price-funding divergence SUI** (OOS PF 1.17, "
             "exceso +65pp), **26 EMA cross ETH** (OOS PF 1.09, exceso +28pp, PF decae a "
             "1.00 a 22 bps).\n")
lines.append("## Advertencias\n")
lines.append("- Criba con convenciones homogéneas, **no** artefactos del harness "
             "`vrun.py` (sin walk-forward, sin period gates, sin neighbourhood). Un "
             "superviviente aquí es **candidato a validación formal** en backtest-system, "
             "no un resultado validado.")
lines.append("- #91 Funding extreme fade pasa el gate completo pero su OOS tiene 1 trade: "
             "sin evidencia fuera de muestra.")
lines.append("- #85 (Kalman hedge) quedó **invalidada durante la corrida**: el Kalman "
             "escalar se congelaba y un bug de signo del coste en spreads con precio "
             "negativo inflaba el resultado (+9342% falso). Con beta OLS rodante es −81%. "
             "Los 4 bugs encontrados y corregidos: signo del coste en el motor de pares, "
             "lookahead en el motor de flips (posición cobraba el retorno de su propia "
             "barra), churn de re-entrada en rupturas diarias (47/55) y bloque 4h sin "
             "cerrar en #98.")
lines.append("- Los TF de 5m/15m están sistemáticamente por debajo de 1h: el coste de "
             "11-22 bps come cualquier edge bruto en 23 de 25 mean-reversion.")
lines.append("- JUP 1h/15m vienen de la API (listing 2024-01); HYPE 1h completada hasta "
             "hoy. XS/pares usan la intersección de historiales (HYPE limita a 2025-05).\n")
lines.append("## Tabla completa (11 bps, ordenada por OOS PF)\n")
lines.append("| # | Estrategia | Par | TF | Grupo | Net | PF | n | DD | WR | Sharpe | "
             "OOS net | OOS PF | OOS n | OOS DD | Gate |")
lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
srt = n11.sort_values("oos_pf", ascending=False)
for _, r in srt.iterrows():
    lines.append(f"| {int(r.no)} | {r['name']} | {r.sym} | {r.tf} | {r.group} | "
                 f"{pct(r.net)} | {r.pf:.2f} | {int(r.n)} | {pct(r.mdd)} | "
                 f"{pct(r.wr)} | {r.sharpe:.2f} | {pct(r.oos_net)} | {r.oos_pf:.2f} | "
                 f"{int(r.oos_n)} | {pct(r.oos_mdd)} | {'✅' if r.gate else ''} |")
lines.append("")
lines.append("Charts: `charts/top_survivors.png`, `charts/oos_map.png`. "
             "Datos crudos: `results_100.csv` (11 y 22 bps).")
(WORK / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
print("REPORT.md + charts escritos")
