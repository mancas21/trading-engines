"""Genera TOP10_ESTRATEGIAS.md: descripcion, reglas exactas, metodologia y metricas."""
from pathlib import Path

import numpy as np
import pandas as pd

import btlib as B

WORK = Path(__file__).parent
TOP10 = [60, 44, 73, 71, 28, 56, 95, 26, 34, 59]

DESC = {
60: ("**Keltner breakout con confirmacion de RSI** (HYPE 1h). Compra rupturas de volatilidad "
     "solo cuando el impulso acompana: precio que cierra fuera del canal Keltner superior "
     "y RSI en zona alcista. Es la unica del lote de rupturas con filtro de momento."),
44: ("**Ruptura de rango largo (7 dias)** (BTC 1h). Canales de Donchian asimetricos tipo "
     "turtle: entrada cuando el close supera el maximo de 168 horas, salida cuando cae bajo "
     "el minimo de 72 horas. Estrategia de tendencia pura, sin indicador."),
73: ("**Reversion de domingo a lunes** (ETH 1h). Si el domingo cerro con una caida mayor que "
     "1 ATR, compra la apertura del lunes y mantiene ~48 h. Apuesta al rebote posterior a un "
     "fin de semana debil. La unica del top que bate a buy & hold en OOS con gate completo."),
71: ("**EMA cross restringido a hora del dia** (JUP 1h). El cruce clasico EMA20/EMA50 pero "
     "solo aceptando senales generadas entre las 14:00 y 17:59 UTC (apertura de EU/US). "
     "Reduce los falsos cruces del resto del dia en un altcoin ruidoso."),
28: ("**Donchian breakout clasico** (BTC 1h). Entra cuando el close rompe el maximo de 55 "
     "barras (~2.3 dias) y sale cuando cae bajo el minimo de 20 barras. El mas antiguo del "
     "top y el que tiene mas trades OOS (138): la senal de tendencia mas robusta del lote."),
56: ("**Donchian con salida chandelier** (JUP 1h). Entra por ruptura del maximo de 30 barras "
     "y deja correr la ganancia con un stop movil tipo chandelier (maximo desde entrada "
     "menos 3 ATR). Sin salida discreta: todo el exit es el trail."),
95: ("**Divergencia precio-funding** (SUI 1h). Short cuando el precio hace un nuevo minimo "
     "de 48 barras mientras el funding esta en su maximo de ~30 settlements: multitud pagando "
     "funding alto justo en el suelo. Time stop de 24 barras; el short cobra el funding "
     "positivo mientras dura la posicion."),
26: ("**Cruce de medias EMA20/50** (ETH 1h). El trend-following canonico, long y short, "
     "salida por cruce contrario y stop amplio de 3 ATR. 1134 trades: el de mayor muestra "
     "del top; su OOS PF 1.09 es marginal pero el exceso vs B&H es positivo."),
34: ("**Pendiente de regresion lineal** (SOL 1h). Pendiente OLS de los ultimos 50 closes; "
     "long cuando la pendiente cruza a positivo, short cuando cruza a negativo. Version "
     "suavizada del momentum que filtra mejor el ruido que un cruce de medias corto."),
59: ("**Expansion de rango intrabar** (SOL 1h). Compra velas cuya amplitud dobla al ATR y "
     "que ademas cierran en el 20% superior de la propia vela (momento de cierre). Stop "
     "en el minimo de la vela de senal y salida si el close cae bajo el minimo previo."),
}

RULES = {
60: ("- **Entrada (long):** close > canal Keltner superior, con Keltner(20, 2.0) = "
     "EMA(20) ± 2×ATR(20); ademas RSI(14) > 60. Senal al cierre; fill en la apertura siguiente.\n"
     "- **Salida:** close < EMA(20) (senal al cierre, fill en la apertura siguiente).\n"
     "- **Stop:** 2×ATR(14) por debajo del precio de entrada, estatico.\n"
     "- **Direccion:** solo long."),
44: ("- **Entrada (long):** close > maximo de las 168 barras anteriores (rolling 168 "
     "desplazado 1 barra; ~7 dias).\n"
     "- **Salida:** close < minimo de las 72 barras anteriores (~3 dias).\n"
     "- **Stop:** 2.5×ATR(14) bajo la entrada, estatico.\n"
     "- **Direccion:** solo long."),
73: ("- **Senal:** el retorno del domingo (close de la barra de las 23:00 UTC menos la "
     "apertura de las 00:00 UTC) es menor que −1×ATR(14).\n"
     "- **Entrada (long):** fill en la apertura del lunes 00:00 UTC.\n"
     "- **Salida:** senal el lunes 23:00 UTC → fill en la apertura del martes 00:00 UTC "
     "(hold ≈ 48 h). Tambien cierra por stop.\n"
     "- **Stop:** 2×ATR(14) bajo la entrada, estatico.\n"
     "- **Direccion:** solo long."),
71: ("- **Entrada (long):** EMA(20) cruza por encima de EMA(50) y la hora de la barra de "
     "senal es 14, 15, 16 o 17 UTC. **Entrada (short):** cruce inverso en la misma ventana.\n"
     "- **Salida:** cruce contrario en cualquier hora del dia.\n"
     "- **Stop:** 2×ATR(14) contra la entrada, estatico.\n"
     "- **Direccion:** long y short."),
28: ("- **Entrada (long):** close > maximo de las 55 barras anteriores (desplazado 1 barra; "
     "~2.3 dias).\n"
     "- **Salida:** close < minimo de las 20 barras anteriores.\n"
     "- **Stop:** 3×ATR(14) bajo la entrada, estatico.\n"
     "- **Direccion:** solo long."),
56: ("- **Entrada (long):** close > maximo de las 30 barras anteriores (desplazado 1 barra).\n"
     "- **Salida:** stop movil chandelier: stop = max(stop, high de la barra − 3×ATR(14)), "
     "actualizado en cada barra mientras la posicion esta abierta. No hay salida por senal.\n"
     "- **Stop inicial:** 3×ATR(14) bajo la entrada (es tambien la distancia de sizing).\n"
     "- **Direccion:** solo long."),
95: ("- **Senal (short):** close ≤ minimo de las 48 barras anteriores (nuevo minimo) "
     "Y funding actual ≥ maximo del funding de las ultimas ~30 settlements (8 h cada uno).\n"
     "- **Entrada:** fill en la apertura siguiente al cierre de la barra de senal.\n"
     "- **Salida:** time stop a las 24 barras (fill en la apertura de la barra 25) o stop.\n"
     "- **Stop:** 2×ATR(14) sobre la entrada, estatico.\n"
     "- **Funding:** el short cobra (+rate × notional) en cada settlement que atraviesa; "
     "se acredita en el R del trade.\n"
     "- **Direccion:** solo short."),
26: ("- **Entrada (long):** EMA(20) cruza por encima de EMA(50). **Entrada (short):** "
     "EMA(20) cruza por debajo de EMA(50).\n"
     "- **Salida:** cruce contrario.\n"
     "- **Stop:** 3×ATR(14) contra la entrada, estatico.\n"
     "- **Direccion:** long y short."),
34: ("- **Senal:** pendiente de la regresion lineal (minimos cuadrados, formula cerrada con "
     "sumas rodantes) de los ultimos 50 closes.\n"
     "- **Entrada (long):** la pendiente cruza de ≤0 a >0. **Entrada (short):** cruza de ≥0 "
     "a <0.\n"
     "- **Salida:** cruce de pendiente contrario.\n"
     "- **Stop:** 2×ATR(14) contra la entrada, estatico.\n"
     "- **Direccion:** long y short."),
59: ("- **Senal (long):** rango de la barra (high−low) > 2×ATR(14) Y el close esta en el "
     "20% superior de la propia vela ((close−low)/(high−low) > 0.8).\n"
     "- **Entrada:** fill en la apertura siguiente al cierre de la vela de senal.\n"
     "- **Stop:** el minimo de la vela de senal (distancia de sizing = close − low).\n"
     "- **Salida:** senal al cierre cuando close < minimo de la barra anterior.\n"
     "- **Direccion:** solo long."),
}

NOTES = {
60: "Datos HYPE 1h desde 2025-05-30 (listing del perp), completado por API hasta hoy. "
    "El OOS alcista de HYPE hace que B&H gane +40pp a la estrategia; el PF se mantiene "
    "incluso a 22 bps (OOS 1.47).",
44: "La #44 del doc original ('168-bar breakout') era la mas viva de la criba prompts-100 "
    "de 2026-10-30 y aqui confirma: gate completo + OOS + sensibilidad 22 bps (OOS 1.39).",
73: "Apuesta al rebote posterior a un fin de semana debil. Es la unica del top que bate a "
    "B&H en OOS con gate completo (+22pp). Muestra pequena (42 OOS): validar antes de creer.",
71: "JUP 1h viene de la API (listing del perp 2024-01-31). Solo 30 trades OOS: en el limite "
    "del gate. Aguanta 22 bps (OOS 1.31).",
28: "El mas robusto por muestra (138 trades OOS) y el mas viejo del lote. Es el unico del "
    "top que NO aguanta la sensibilidad de 22 bps (OOS PF cae a 1.16).",
56: "No pasa el gate completo (net total −1.6%: el IS lo pierde) pero su OOS es sólido "
    "(PF 1.27, +11.7%). Candidato solo si el IS se descarta como regime distinto.",
95: "Exceso vs B&H en OOS +65pp: gana al activo con claridad. OOS PF 1.17 queda bajo el "
    "gate 1.2 — a la espera de validacion formal. Requiere feed de funding en vivo.",
26: "Mayor muestra del top (1134 trades, 385 OOS). El OOS PF 1.09 no pasa el gate y a 22 "
    "bps cae a 1.00: edge real pero comido por el coste.",
34: "Exceso vs B&H OOS +51pp (bate al activo). OOS PF 1.06 marginal; a 22 bps cae a 0.98.",
59: "Exceso vs B&H OOS +47pp. OOS PF 1.03: practicamente break-even en OOS.",
}


def main():
    df = pd.read_csv(WORK / "results_100.csv")
    n11 = df[np.isclose(df.cost_bps, 0.0011)].copy()
    eqs = np.load(WORK / "eqs.npy", allow_pickle=True)
    eqmap = {int(no): v for (no, c), v in eqs if np.isclose(c, 0.0011)}

    L = []
    L.append("# Top 10 estrategias — criba de los 100 prompts (Binance USDT-M perps)\n")
    L.append("**Fecha:** 2026-11-01 · **Coste aplicado:** 11 bps roundtrip (modelo oficial "
             "del repo: 5 bps fee + 0.5 bps slip por lado) · **Ranking:** por OOS profit "
             "factor con ≥30 trades OOS · Fuente: `backtest_prompts100/results_100.csv`\n")
    L.append("Estas 10 son las mejores de las 100 estrategias backtested. Las 5 primeras "
             "pasan el gate completo del repo (PF ex-cost > 1.2 · n ≥ 30 · net > 0) tambien "
             "en OOS; las 5 siguientes se quedan cerca y se incluyen por contexto.\n")

    L.append("## 1. Como se calculo cada estrategia\n")
    L.append("Cada prompt del documento original se traduje a una especificacion vectorizada "
             "(`strategies.py`): indicadores estandar (RSI Wilder, ATR Wilder, EMA, Keltner, "
             "regresion OLS rodante, Donchian, funding por settlement mapeado a barras). "
             "Convenciones comunes a las 10:\n")
    L.append("- **Senal en barra cerrada:** todo indicador se calcula solo con datos hasta el "
             "cierre de la barra de senal (sin lookahead).")
    L.append("- **Fill en la apertura siguiente:** la entrada/salida por senal se ejecuta en "
             "la apertura de la barra posterior a la senal.")
    L.append("- **El stop gana empates:** si en la misma barra se tocan stop y salida, ejecuta "
             "el stop.")
    L.append("- **Stops intrabar con hueco:** si la barra abre mas alla del stop, el fill es "
             "en la apertura (peor), no en el nivel del stop.")
    L.append("- **Sizing:** 1% de la equity en riesgo por trade → notional = (0.01 × equity) / "
             "distancia_del_stop; equity compuesta trade a trade. El apalancamiento implicito "
             "es 1/(stop% del precio).")
    L.append("- **Costes:** 11 bps roundtrip aplicados al notional en el cierre de cada trade "
             "(en unidades R: 0.0011 × precio_entrada / distancia_stop).")
    L.append("- **Funding (solo #95):** devengo por settlement (cada 8 h) mientras la posicion "
             "esta abierta, acreditado/cargado al R del trade.\n")

    L.append("## 2. Como se hizo el backtest\n")
    L.append("**Motor:** eventos barra a barra (`backtest_prompts100/btlib.py`), un "
             "instrumento por estrategia. Ciclo por barra: (1) devengo de funding si hay "
             "posicion, (2) salidas — senal de la barra anterior, time stop, stop intrabar "
             "(con gap-through), trailing si aplica, (3) actualizacion del trailing, "
             "(4) entradas por senal de la barra anterior, (5) marca a mercado de la equity "
             "al cierre de la barra.\n")
    L.append("**Datos** (klines 1h de `backtest-system/data`, read-only; faltantes por API "
             "publica fapi de Binance, guardados en `backtest_prompts100/data/`):\n")
    L.append("| Par | Rango 1h |")
    L.append("|---|---|")
    rangos = {"BTCUSDT": "2019-09 → 2026-10-24", "ETHUSDT": "2019-11 → 2026-10-24",
              "SOLUSDT": "2020-09 → 2026-10-24", "SUIUSDT": "2023-05 → 2026-10-24",
              "HYPEUSDT": "2025-05-30 → hoy (cola por API)",
              "JUPUSDT": "2024-01-31 → hoy (API)"}
    for s, r in rangos.items():
        L.append(f"| {s} | {r} |")
    L.append("")
    L.append("**Split 70/30 IS/OOS** por numero de barras de cada serie: el 30% final de "
             "barras es out-of-sample. Las metricas IS y OOS se calculan sobre los trades "
             "cuya entrada cae en cada segmento; la equity es continua.\n")
    L.append("**Metricas:** net (retorno compuesto), max drawdown sobre la curva de equity "
             "marcada a mercado por barra, profit factor = suma de R ganadores / |suma de R "
             "perdedores| (R = P&L / distancia de stop, ya neto de costes), win rate, Sharpe "
             "anualizado (√8760 en 1h), buy & hold del par y exceso = net − B&H en el mismo "
             "segmento.\n")
    L.append("**Gates del repo aplicados como criba:** PF ex-cost > 1.2 · n ≥ 30 trades · "
             "net > 0 · exceso vs B&H reportado. En OOS se exige lo mismo. 22 bps se corrio "
             "como sensibilidad pero NO decide (coste oficial confirmado: 11 bps).\n")
    L.append("**Limites:** esto es una criba con convenciones homogeneas, NO artefactos del "
             "harness `vrun.py` (sin walk-forward, sin period gates, sin neighbourhood). "
             "Un superviviente aqui es candidato a validacion formal en backtest-system, no "
             "un resultado validado. El max DD se mide en cierres de barra; los fills "
             "intrabar de stop estan modelados pero la curva solo marca al cierre.\n")

    L.append("## 3. Resumen de las 10 (11 bps)\n")
    L.append("| # | Estrategia | Par · TF | Net | PF | n | DD | WR | OOS PF | OOS n | "
             "OOS net | OOS DD | Exceso OOS | Gate |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for no in TOP10:
        r = n11[n11["no"] == no].iloc[0]
        gate = "✅ completo" if (r.gate and r.oos_ok) else ("full" if r.gate else "—")
        L.append(f"| {no} | {r['name']} | {r['sym'].replace('USDT','')} · {r['tf']} | "
                 f"{r.net*100:+.1f}% | {r.pf:.2f} | {int(r.n)} | {r.mdd*100:.1f}% | "
                 f"{r.wr*100:.0f}% | {r.oos_pf:.2f} | {int(r.oos_n)} | "
                 f"{r.oos_net*100:+.1f}% | {r.oos_mdd*100:.1f}% | "
                 f"{r.oos_excess*100:+.0f}pp | {gate} |")
    L.append("")

    for no in TOP10:
        r = n11[n11["no"] == no].iloc[0]
        eq = np.asarray(eqmap[no], dtype=float)
        sym = r["sym"].replace("USDT", "")
        sp = int(len(eq) * 0.7)
        dfo = B.load(r["sym"], r["tf"])
        ts = pd.to_datetime(dfo["open_ts"], unit="ms", utc=True)
        L.append(f"---\n\n## #{no} {r['name']} — {sym} · {r['tf']}\n")
        L.append(DESC[no] + "\n")
        L.append("**Reglas exactas (tal como se backtestearon):**\n")
        L.append(RULES[no] + "\n")
        L.append(f"**Ventana:** {ts.iloc[0]:%Y-%m-%d} → {ts.iloc[-1]:%Y-%m-%d} "
                 f"({len(dfo):,} barras 1h) · **OOS desde:** {ts.iloc[sp]:%Y-%m-%d}\n")
        L.append(f"**Resultados (11 bps):** net {r.net*100:+.1f}% · PF {r.pf:.2f} · "
                 f"n {int(r.n)} · DD {r.mdd*100:.1f}% · WR {r.wr*100:.0f}% · "
                 f"Sharpe {r.sharpe:.2f} · B&H {r.bh_full*100:+.0f}% — "
                 f"OOS: net {r.oos_net*100:+.1f}% · PF {r.oos_pf:.2f} · n {int(r.oos_n)} · "
                 f"DD {r.oos_mdd*100:.1f}% · Sharpe {r.oos_sharpe:.2f} · "
                 f"exceso vs B&H {r.oos_excess*100:+.0f}pp\n")
        L.append(f"**Nota:** {NOTES[no]}\n")
        L.append(f"![#{no}]({'charts/top10_%03d.png' % no})\n")

    L.append("---\n\n**Veredicto:** las 5 con gate completo + OOS (#60 #44 #73 #71 #28) son "
             "candidatos a validacion formal en backtest-system (`vrun.py`, walk-forward). "
             "Orden sugerido por robustez de muestra: #28 (138 OOS) → #44 (52) → #60 (42) ≈ "
             "#73 (42) → #71 (30). #95 es el mas interesante por exceso vs B&H (+65pp) pero "
             "su OOS PF 1.17 no llega al gate; #26/#34/#59 tienen exceso positivo y PF OOS "
             "marginal. Las reversion a la media, los pares y todo el rango 5m/15m de la "
             "criba quedaron muertos (ver REPORT.md).")
    (WORK / "TOP10_ESTRATEGIAS.md").write_text("\n".join(L), encoding="utf-8")
    print("TOP10_ESTRATEGIAS.md escrito")


if __name__ == "__main__":
    main()
