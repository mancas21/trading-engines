# AUDITORÍA — Gate de señales del engine (verdict.py + Reglas 6/7) — PAQUETE AUTOCONTENIDO

**Fecha:** 2026-11-12 · **Solicitante:** Manuel
**Pregunta original:** "paso a paso, qué mide este engine en un análisis en
vivo — el edge, las reglas, etc. — qué es lo que hace que no dispare ninguna
señal nunca."
**Este documento es AUTOCONTENIDO:** toda la evidencia que cita está
incrustada en los Apéndices A–E. El auditor NO necesita acceso al repo para
evaluar las afirmaciones; las verificaciones que requieren filesystem se
marcan como tales en §7. Para integridad, los dos ficheros clave llevan sha256:

```
sha256(atas-bridge/rules.json)  = 3803c10f6f5c7803aacd2b67c341ea42296725f014afdb388b0cfe862ffcf718
sha256(reader/verdict.py)       = 0944c60ea97ba93a5fab88836b6dc4fa4d4dcd9948af85befb0b3a69c93d2278
```

---

## §1 — Qué mide el engine en una lectura en vivo

Pipeline del reader (granularidad 30s), todo llega a `reader/verdict.py`:

1. **Footprint ATAS por nivel de precio:** % del volumen que fue agresión
   compradora vs vendedora en cada nivel de la barra 1h en curso.
2. **CVD:** delta acumulado por barra 1h; rachas consecutivas; integridad de
   serie (huecos, barras faltantes).
3. **Libro multi-venue (scanner-engine, cada 15s):** top-5 del libro en
   Binance/OKX/Bybit/Kraken, normalizado por venue (cada venue se compara
   contra SU propia media/std, `dom_vac_calibration.json`), z-score por venue.
4. **Tape:** % de prints compradores, tamaño medio de print.
5. **Estructura TradingView 1m:** EMAs/RSI/stack — **contexto declarado, no
   señal** (el propio verdict la marca `validated: false`).
6. **Ventana de coincidencia:** las condiciones (a)/(b)/(c) de una regla deben
   coincidir dentro de una **ventana de 60 segundos** (`condition_window_s:
   60`) — no en el mismo tick.

Salida: `bias` ∈ {BULLISH, BEARISH, NEUTRAL} + cada condición con estado
MET / FAIL / UNEVALUABLE / SPLIT + `probability: "unvalidated"` SIEMPRE.

## §2 — Las dos puertas direccionales (números vivos de rules.json v3.4.1)

### Regla 7 → BULLISH (`enforcement: "hard"`) — las TRES a la vez, en 60s:
- **(a)** CVD subiendo ≥ 2 barras 1h consecutivas (`cvd_consecutive_up_min: 2`)
- **(b)** Footprint: ≥ 3 niveles con ≥ 65% del volumen comprador
  (`footprint_levels_min: 3`, `footprint_buy_pct_min: 65`)
- **(c)** Consenso multi-venue: ≥ 3 de 4 venues con el libro top-5 cargado a
  BUY más allá de la banda 0.25 **normalizada por venue** (voto = z ≥ 1.0),
  sostenido ≥ 2 polls del reader (`dom_vac_venues_min: 3`, `dom_vac_polls_min:
  2`, `dom_vac_thr: 0.25`, `dom_vac_z_min: 1.0`)

### Regla 6 → BEARISH (`enforcement: "disabled"` — cuarentena)
Espejo de la 7 (4 niveles vendedores). Se evalúa y se publica su detalle,
pero **NO puede decidir el sesgo** — el código de publicación es explícito
(ver Apéndice B, bloque líneas 1257–1274): `if r7_met: BULLISH / elif r6_met
and enforcement != "disabled": BEARISH / else: NEUTRAL`.

**Sobre la ventana de 60s — la medición más incómoda del sistema** (nota
`condition_window_note` en rules.json, incrustada en Apéndice A): durante el
rally de JUP del 2026-10-15 (08:54 → 10:17, +8.55%), el motor emitió **86
NEUTRAL y 1 solo BULLISH, a las 10:06, con el rally ya hecho**. No faltaba
evidencia — (a) estuvo MET 34 veces, (b) 41 veces, (c) 10 veces en esa
ventana — pero exigirlas en el MISMO tick descartaba los near-miss. Con la
ventana de 60s el primer disparo habría sido 09:35 @ 0.3022 (31 min antes).
La nota dice literalmente: **"NO VALIDADO COMO EDGE: que dispare antes está
medido; que esos disparos ganen NO"** (no medible con 209h de registro al 13%
de cobertura).

## §3 — Inventario de edge medido (todo el sistema)

| # | Regla / capa | Medición | Veredicto |
|---|---|---|---|
| 1 | Scorecard 0-100 (Regla 4, ATAS) | pearson +0.0175 / spearman −0.0156 sobre 1,205 filas; tercil ALTO winrate 32.8% retorno −0.392%; tercil BAJO 37.9% / −0.382%; el componente dominante (dom_ema, spread +27.3pp) con autocorrelación rho=+0.995 ≈ **1 observación independiente** (z=0.35) | **RETIRADO** (commit `a93147f`, 2026-10-13) |
| 2 | Regla 6 (distribution, ATAS) | 161 disparos medibles = 32 barras = 9 eventos (rho lag-1 +0.973, **n_eff ≈ 1.8**); batía a un PLACEBO por +0.0341pp (t=+0.34); el "58.4% a favor" era la deriva del mercado, no la regla | **disabled** |
| 3 | Scanner regla 1 (basis dislocation) | v3: neto **−9.6 bps**, t=−5.6 | **RETIRADA** |
| 4 | Scanner regla 2 (flow dominance) | v3: neto **−11.3 bps**, t=−9.5 | **RETIRADA** |
| 5 | Scanner regla 4 (flow agreement) | n=662, media −0.0052%, **t=−1.34**, hit 48.0% — "moneda al aire que pierde un poco contra los costos" | sin edge, única hipótesis viva |
| 6 | Regla 7 (accumulation, ATAS) | evidencia: **~4 barras** — sin potencia estadística | `hard` pero NO validada |
| 7 | Capa vigilancia v3 (watch_early) | LONG funding ≤ 10× baseline: **56.2% hit neto, med r24 +1.0%, MFE p75 +11.2%** (n=32 histórico) | en producción, midiendo |

**Balance literal: el gate no tiene NI UNA regla direccional con edge
positivo MEDIDO.** Lo único verde del sistema (fila 7) vive FUERA del gate.

## §4 — Las cuatro razones por las que nunca dispara

1. **Conjunción AND con una condición rara por diseño:** la (c) exige
   unanimidad de libro (3 de 4 venues, banda normalizada, z ≥ 1, sostenida,
   coincidiendo con (a) y (b) en 60s). Observado en todas las lecturas del
   2026-10-24 → 11-12: el consenso devolvió **SPLIT** (venues en lados
   opuestos). Unanimidad de 4 venues casi no existe fuera de eventos.
2. **La única regla habilitada (7) nunca fue validada** con muestra
   suficiente.
3. **Todo lo que se midió resultó sin edge y fue desactivado** — con el
   protocolo completo: placebo, n_eff, autocorrelación, y una lección
   institucional (el bug del horizonte de 5m que fabricó un "edge negativo
   significativo" falso — Apéndice E).
4. **Decisión de diseño explícita (2026-10-11):** el motor viejo emitía 19.9
   señales/símbolo-día; la cura fue silencio hasta evidencia completa. Costo
   asumido y aceptado: latencia total.

## §5 — Tesis de diseño (lo que se pide juzgar)

El engine **no es un generador de entradas — es un sistema anti-FOMO con
auditoría**. Su producto real es "no hay evidencia completa Y validada". Las
entradas tempranas viven FUERA del gate: capa de vigilancia
(`reader/watch_early.py`, v3: LONG-only, funding ≤ −0.001, ~5-6 banderas/año,
edge medido 56.2% en backtest) y lectura humana en vivo. Camino declarado
para que el gate dispare algún día: NO aflojar las reglas existentes; medir
una regla NUEVA hasta que tenga edge demostrado (protocolo placebo/n_eff
igual que mató a las reglas 1/2/4), y solo entonces darle el papel de
publicar sesgo.

## §6 — Qué se pide al auditor

1. **Consistencia interna** de este documento (los Apéndices A–E son la
   evidencia; verificar que §2–§4 los citan correctamente).
2. **Juzgar la tesis de §5** con los números de §3: ¿es correcto mantener el
   gate en silencio total mientras ninguna regla tenga edge validado?
3. **La pregunta abierta de diseño:** ¿existe una vía intermedia MEDIBLE entre
   silencio total y ruido (p.ej. gradiente de confianza publicado, banderas
   escalonadas por profundidad de funding) que no reproduzca el ruido del
   motor viejo? Restricciones: cualquier vía nueva debe llevar pre-registrado
   su protocolo de medición (placebo + n_eff + walk-forward) antes de
   publicar nada.
4. **Revisar el diseño de la condición (c):** la calibración por venue con
   estados SPLIT declara 27.8% de ciclos como "no interpretable". ¿Es razonable
   exigir unanimidad 3-de-4 en un mercado de microestructura fragmentada, o la
   puerta está diseñada para casi nunca abrirse?
5. **Revisar la capa de vigilancia v3** (Apéndice D): umbral 10× elegido por
   gradiente monótono con n=32 — ¿suficiente muestra para fijar el umbral, o
   se necesita más histórico antes de considerarlo edge?

## §7 — Qué NO se puede verificar sin acceso al sistema

- El estado runtime (scanner vivo, calibraciones `dom_vac_calibration.json`,
  logs del reader). Este doc registra valores de ficheros leídos el
  2026-11-12 con sha256 de anclaje.
- La reproducibilidad del backtest de la capa watch requiere los klines 1h/15m
  y funding de Binance (API pública) — metodología completa en Apéndice D.

---

# APÉNDICE A — `atas-bridge/rules.json` v3.4.1 (verbatim, 143 líneas)

```json
{
  "rules": [
    {
      "id": 1,
      "title": "No Single Bar Prediction",
      "description": "NEVER call a directional reversal from a single bar. Wait minimum 2 consecutive bars confirming the new direction.",
      "enforcement": "hard"
    },
    {
      "id": 2,
      "title": "CVD Extremes = Exhaustion",
      "description": "ALL: un pico de CVD de UNA barra (delta por barra, NUNCA la serie acumulada de la sesion) >= conditions.spike_multiple (1.5) x el maximo de las conditions.min_prior_bars (3) barras previas es EXHAUSTION, no continuacion. Simetrico para las caidas contra el minimo previo. Flag as exhaustion; do NOT call continuation. NOTE 2026-10-15: esta descripcion decia '>2x prior ATH' mientras conditions{} evalua 1.5x desde el changelog 3.4.1 (2026-10-07). Ahora cita los valores que el motor aplica (verdict.py, bloque Regla 2: compara bar_deltas, no high_cvd/low_cvd).",
      "enforcement": "hard",
      "conditions": {
        "spike_multiple": 1.5,
        "min_prior_bars": 3
      }
    },
    {
      "id": 3,
      "title": "No Invented Probabilities",
      "description": "Probabilities must come from validated hit-rate. No backtest = output 'probability: unvalidated'. Every probability claim must cite a rule from rules.json.",
      "enforcement": "hard"
    },
    {
      "id": 4,
      "title": "Score, Don't Predict",
      "description": "RETIRADA 2026-10-13. Exigia un scorecard 0-100 que se midio sin poder predictivo. El sesgo sale solo de las reglas 6/7.",
      "enforcement": "disabled",
      "disabled_reason": "MEDIDA, NO OPINION. 1.205 filas de reader.db con forward_return resuelto: pearson(score, forward_return) = +0.0175 y spearman = -0.0156. El tercil alto ('mejor score') ganaba 32,8% con retorno medio -0,392%; el bajo ganaba 37,9% con -0,382%. El mejor score perdia MAS que el peor. Ademas el componente de peso 50 mediava un spread de +27,3pp sobre top150/bottom150, pero con autocorrelacion de forward_return rho=+0.995 eso equivale a ~1 observacion independiente (z=0.35): el spread no es de 1.205 muestras, es aproximadamente una. Se desactiva porque el sesgo nunca dependio del score -- sale de if r7_ok / elif r6_ok / else -- y publicar un numero sin poder predictivo da apariencia de informacion. Reactivar solo con out-of-sample real, nunca con el spread de esta entrada.",
      "thresholds": {
        "bullish_min": 60,
        "bearish_max": 40,
        "neutral_min": 40,
        "neutral_max": 59
      }
    },
    {
      "id": 5,
      "title": "No Pattern Anchoring",
      "description": "Past shakeouts do not predict future shakeouts. Each bar is judged on its own data. Do not reference prior bar patterns as evidence. Only use current bar data + structural levels.",
      "enforcement": "hard"
    },
    {
      "id": 6,
      "title": "Distribution Threshold",
      "description": "ALL required: (a) CVD drops 2+ consecutive bars AND (b) footprint shows 4+ levels with >=65% bid-side selling AND (c) multi-venue book consensus: 3+ venues with top-5 book loaded on the SELL side beyond band 0.25, held 2+ reader polls (scanner_multi_venue). Only then output bearish bias. NOTE: umbrales alineados con conditions{} el 2026-10-12; antes decian 70% y +5%.",
      "enforcement": "disabled",
      "disabled_reason": "MEDIDO SIN EDGE CONTRA UN PLACEBO — ya no es solo falta de evidencia. Con el resolver arreglado (reader_supervisor.py solo lanzaba 'record', nunca 'resolve'; 1.163 filas carrying 'unmeasured' from 11-oct) hay 2.156 veredictos resueltos y 161 disparos de la regla 6 medibles. El titular ('58,4% a favor') es REAL pero VACIO. La regla 6 solo disparo entre 06/10 19:22 y 11/10 20:46, y el activo caia: sobre las 1.458 barras resueltas de ESA VENTANA, un BEARISH acierto el 68,0% de las veces con +0,3042% de retorno dirigido (t=+16.04). CONTROL CRUZADO, misma ventana, directed= -forward_return (BEARISH), horizonte 12 barras: (A) todas las barras +0,3042% | (B) barras donde la 6 NO disparo +0,3009% | (C) barras donde la 6 SI disparo, deduplicadas a 32 barras +0,3530% | (D) DIFERENCIAL C-B = +0,0521%, se 0,1534%, t=+0.34. Y un PLACEBO (BEARISH tirado a suert sobre las mismas barras) da +0,3189%: la regla 6 lo supera por +0,0341 pp, dentro del ruido de 32 barras. Ademas las 161 filas son 32 BARRAS y 9 EVENTOS (rho lag-1 +0,9730): n_eff 1,8 por evento, t=+0,45. Conclusion: la regla 6 no anticipo nada, coincidio con la deriva. Todo lo que 'ganaba' era el signo del mercado en esa semana, no el analisis. Se mantiene desactivada; reactivar exige (i) n_eff >= min_n sobre el gate MULTI-VENUE — los 161 disparos son de la era del gate viejo (condicion (c) por EMA del libro Binance), que es OTRA regla con el mismo numero — y (ii) superar el placebo en walk-forward out-of-sample, no solo batir el cero.",
      "conditions": {
        "cvd_consecutive_down_min": 2,
        "footprint_levels_min": 4,
        "footprint_sell_pct_min": 65,
        "dom_source": "scanner_multi_venue",
        "dom_vac_venues_min": 3,
        "dom_vac_polls_min": 2,
        "dom_vac_thr": 0.25,
        "condition_window_s": 60,
        "condition_window_note": "2026-10-15. Las condiciones (a)/(b)/(c) deben coincidir DENTRO de esta ventana, no en la misma lectura de 30s del reader. MEDIDO durante el rally de JUP (08:54 0.2903 -> 10:17 0.3151, +8.55%): el motor emitio 86 NEUTRAL y 1 solo BULLISH, a las 10:06 con el rally ya hecho. No faltaba evidencia -- (a) estuvo MET 34 veces, (b) 41 y (c) 10 en esa ventana -- pero exigirlas en el MISMO tick descartaba los near-miss: a las 09:37 (a)=MET (b)=MET (c)=FAIL, y (c) habia estado MET a las 09:35. Con 60s el primer disparo es 09:35 @ 0.3022 (31 min antes, +4.3% de recorrido por delante en vez de +1.2%). El valor NO esta optimizado: la hora del primer disparo es IDENTICA a 60/180/300/600s, o sea 60s es la intervencion minima suficiente, no un ajuste fino. La ventana NO anade evidencia, solo deja de descartar la que ya habia, y nunca eleva un UNEVALUABLE. NO VALIDADO COMO EDGE: que dispare antes esta medido; que esos disparos ganen NO, y no se puede medir con 209h de registro al 13% de cobertura (~35 observaciones independientes a 60 min).",
        "dom_vac_z_min": 1.0,
        "condition_c_note": "2026-10-15 — CONSENSO NORMALIZADO POR VENUE. Antes la (c) comparaba book_imb_5 de cada venue contra un umbral ABSOLUTO (0.25). Medido sobre la muestra historica de reader.db, eso media 'que venue vive fuera de su propia banda', no acuerdo: okx tiene media -0.199 y cruza +0.25 solo el 1.5% de las veces (por estructura no puede votar compra), kraken tiene std 0.499 y cruza el umbral el 42% con puro ruido. Ahora cada venue se compara contra SU propia media/std (dom_vac_calibration.json, generado por reader/build_dom_vac_calibration.py) y un voto significa z>=dom_vac_z_min. Selectividad conservada: 6.0% de ciclos con consenso antes, 5.7% ahora. Si falta la calibracion o un venue no esta calibrado, la condicion es UNEVALUABLE — no se vuelve al umbral absoluto en silencio. Ademas existe el estado SPLIT: si hay venues votando a ambos lados el consenso se declara NO INTERPRETABLE en vez de FAIL, que antes era indistinguible de 'no hay senal' (27.8% de ciclos medidos)."
      },
      "condition_c_note": "2026-10-11 Fix 2-A (docs/FIXES-gate-DOM.md): la condicion (c) deja de leer imbalance_pct_ema (libro Binance WS top-15, std 7.5, 19-23 cambios de estado/dia) y pasa a consenso multi-venue del scanner: N venues con book_imb_5 del lado sell mas alla de la banda, sostenido dom_vac_polls_min lecturas del reader. polls = lecturas del reader (~30s), no ciclos del scanner. UNEVALUABLE si el scanner no responde o hay menos de dom_vac_venues_min venues vivos.",
      "dom_vac_calibration": {
        "date": "2026-10-11",
        "sample": "50 ciclos del scanner (~10 min), 8 pares x ~4 venues = 1470 valores de book_imb_5; 49 ciclos de XRP para la simulacion de estados",
        "method": "thr al p58 de |imb5| (excursion por venue 61%); el touch de mejor nivel se DESCARTO como base del consenso: medido, es estructuralmente unilateral (mediana |x-0.5|=0.275) y desacoplado entre venues (consenso 3-de-4 en 0 de 480 ciclos)",
        "consensus_freq": "3+/mismo lado en 19.1% de ciclos-pair a thr 0.25 (pooled)",
        "simulated": "0 transiciones MET<->no-MET de la puerta en los 10 min muestreados, en TODO el rango de umbrales probados (el churn del gate viejo era entre estados no-MET)",
        "acceptance_bar": "PRE-REGISTRADO (doc §7.1, antes de medir): N=10 transiciones MET<->no-MET por dia en r7_c/r6_c de reader.db. Verificar manana con: SELECT y contar cambios de estado; si N>10, el gate sigue sin servir y se vuelve a disenar",
        "pending": "re-calibrar a 7 dias de datos cuando el ledger acumule"
      }
    },
    {
      "id": 7,
      "title": "Accumulation Threshold",
      "description": "ALL required: (a) CVD rises 2+ consecutive bars AND (b) footprint shows 3+ levels with >=65% ask-side buying AND (c) multi-venue book consensus: 3+ venues with top-5 book loaded on the BUY side beyond band 0.25, held 2+ reader polls (scanner_multi_venue). Only then output bullish bias. NOTE: umbrales alineados con conditions{} el 2026-10-12; antes decian 70% y +10%.",
      "enforcement": "hard",
      "conditions": {
        "cvd_consecutive_up_min": 2,
        "footprint_levels_min": 3,
        "footprint_buy_pct_min": 65,
        "dom_source": "scanner_multi_venue",
        "dom_vac_venues_min": 3,
        "dom_vac_polls_min": 2,
        "dom_vac_thr": 0.25,
        "condition_window_s": 60,
        "condition_window_note": "2026-10-15. Las condiciones (a)/(b)/(c) deben coincidir DENTRO de esta ventana, no en la misma lectura de 30s del reader. MEDIDO durante el rally de JUP (08:54 0.2903 -> 10:17 0.3151, +8.55%): el motor emitio 86 NEUTRAL y 1 solo BULLISH, a las 10:06 con el rally ya hecho. No faltaba evidencia -- (a) estuvo MET 34 veces, (b) 41 y (c) 10 en esa ventana -- pero exigirlas en el MISMO tick descartaba los near-miss: a las 09:37 (a)=MET (b)=MET (c)=FAIL, y (c) habia estado MET a las 09:35. Con 60s el primer disparo es 09:35 @ 0.3022 (31 min antes, +4.3% de recorrido por delante en vez de +1.2%). El valor NO esta optimizado: la hora del primer disparo es IDENTICA a 60/180/300/600s, o sea 60s es la intervencion minima suficiente, no un ajuste fino. La ventana NO anade evidencia, solo deja de descartar la que ya habia, y nunca eleva un UNEVALUABLE. NO VALIDADO COMO EDGE: que dispare antes esta medido; que esos disparos ganen NO, y no se puede medir con 209h de registro al 13% de cobertura (~35 observaciones independientes a 60 min).",
        "dom_vac_z_min": 1.0,
        "condition_c_note": "2026-10-15 — CONSENSO NORMALIZADO POR VENUE. Antes la (c) comparaba book_imb_5 de cada venue contra un umbral ABSOLUTO (0.25). Medido sobre la muestra historica de reader.db, eso media 'que venue vive fuera de su propia banda', no acuerdo: okx tiene media -0.199 y cruza +0.25 solo el 1.5% de las veces (por estructura no puede votar compra), kraken tiene std 0.499 y cruza el umbral el 42% con puro ruido. Ahora cada venue se compara contra SU propia media/std (dom_vac_calibration.json, generado por reader/build_dom_vac_calibration.py) y un voto significa z>=dom_vac_z_min. Selectividad conservada: 6.0% de ciclos con consenso antes, 5.7% ahora. Si falta la calibracion o un venue no esta calibrado, la condicion es UNEVALUABLE — no se vuelve al umbral absoluto en silencio. Ademas existe el estado SPLIT: si hay venues votando a ambos lados el consenso se declara NO INTERPRETABLE en vez de FAIL, que antes era indistinguible de 'no hay senal' (27.8% de ciclos medidos)."
      },
      "condition_c_note": "2026-10-11 Fix 2-A (docs/FIXES-gate-DOM.md): la condicion (c) deja de leer imbalance_pct_ema (libro Binance WS top-15, std 7.5, 19-23 cambios de estado/dia) y pasa a consenso multi-venue del scanner: N venues con book_imb_5 del lado buy mas alla de la banda, sostenido dom_vac_polls_min lecturas del reader. polls = lecturas del reader (~30s), no ciclos del scanner. UNEVALUABLE si el scanner no responde o hay menos de dom_vac_venues_min venues vivos.",
      "dom_vac_calibration": {
        "date": "2026-10-11",
        "sample": "50 ciclos del scanner (~10 min), 8 pares x ~4 venues = 1470 valores de book_imb_5; 49 ciclos de XRP para la simulacion de estados",
        "method": "thr al p58 de |imb5| (excursion por venue 61%); el touch de mejor nivel se DESCARTO como base del consenso: medido, es estructuralmente unilateral (mediana |x-0.5|=0.275) y desacoplado entre venues (consenso 3-de-4 en 0 de 480 ciclos)",
        "consensus_freq": "3+/mismo lado en 19.1% de ciclos-pair a thr 0.25 (pooled)",
        "simulated": "0 transiciones MET<->no-MET de la puerta en los 10 min muestreados, en TODO el rango de umbrales probados (el churn del gate viejo era entre estados no-MET)",
        "acceptance_bar": "PRE-REGISTRADO (doc §7.1, antes de medir): N=10 transiciones MET<->no-MET por dia en r7_c/r6_c de reader.db. Verificar manana con: SELECT y contar cambios de estado; si N>10, el gate sigue sin servir y se vuelve a disenar",
        "pending": "re-calibrar a 7 dias de datos cuando el ledger acumule"
      }
    },
    {
      "id": 8,
      "title": "Audit On Loss",
      "description": "After a call that fails (price moves against by more than conditions.loss_pct within 2 bars), record loss with root cause at START of next analysis. El fichero es reader/audit_losses.jsonl (una linea JSON por perdida, append; ver verdict.py _audit_record_loss). NOTE 2026-10-15: decia 'audit.log.json', que es el fichero de un subsistema /calls ya BORRADO — verdict.py corrigio la ruta el 2026-10-07 y la declara ahora aqui.",
      "enforcement": "hard",
      "conditions": {
        "loss_pct": 0.01
      }
    }
  ],
  "scoring_weights": {
    "_comment_v341": "Pesos v3.4.1. MEDIDOS COMO ANTI-PREDICTIVOS: score alto a favor 34,0%, score bajo a favor 38,0% (spread -4,0) sobre 1.205 registros. No se usan: verdict.py puntua por percentil con scorecard_calibration.json.",
    "footprint_depth": 25,
    "dom_imbalance": 25,
    "tape_ratio": 20,
    "volume_climax": 15,
    "footprint_confirmation": 15,
    "_retirado": "RETIRADO 2026-10-13: el scorecard se elimino por medicion verdict.py). Estos pesos no se usan en ninguna ruta de produccion."
  },
  "scoring_weights_v2": {
    "_method": "percentile-median. Cada componente se puntua por donde cae en su distribucion empirica (reader.db, n=1205) y se promedian los ranks. El score se centra solo: mediana ~48 si el mercado esta en su mediana.",
    "_measured_spread": "dom_ema +27,3 | footprint_buy_heavy -17,3 (invertido) | footprint_sell_heavy +4,7. EXCLUIDOS por spread negativo o nulo: imbalance_pct -10,0 | volume_ratio -10,0 | tape_buy_pct +0,7 | levels -17,3.",
    "_not_validated": "NO es un modelo validado. Split-half dentro de la misma ventana da 2 dias vs 2 dias del mismo dia: los componentes por separado fallan out-of-sample (-8,0 y -13,3) y solo la combinacion aguanta (+6,7). Firma de overfitting. Quita dano medido, no anade edge probado. Medir el spread sobre barras nuevas antes de llamarlo edge.",
    "_generated_by": "reader/build_scorecard_calibration.py",
    "dom_ema": 50,
    "footprint_sell_heavy": 25,
    "footprint_buy_heavy_inv": 25
  },
  "trigger_weights": {
    "taker_flow": 3.0,
    "book_imbalance": 3.0,
    "wall_pressure": 2.0,
    "value_location": 2.0,
    "oi_delta": 1.5,
    "funding_crowding": 1.5,
    "ls_ratio_crowding": 1.5
  },
  "version": "3.4.1",
  "changelog": "3.4.1 (2026-10-07): Parameter adjustments for Rules 2, 6, 7. spike_multiple 2.0->1.5, dom_imbalance_max 5->3, dom_imbalance_min 10->8, footprint thresholds 70->65, dom_persistence_polls_min 3->2. Enhanced sensitivity without changing rule structure.",
  "namespace": "atas",
  "namespace_note": "Namespace de reglas. EXISTEN DOS rules.json EN ESTE REPO con IDs numericos que se solapan (atas-bridge: reglas 1-8 de CVD/footprint/DOM; scanner-engine: reglas 1-4 de flujo cross-venue). Citar una regla sin su namespace es citar el numero equivocado. Ver docs/FIXES-gate-DOM.md Fix 4."
}
```

# APÉNDICE B — `reader/verdict.py` (excerptos con línea; 1,457 líneas totales)

**Excerpto 1 — docstring (líneas 1–33):** "ESTE FICHERO NO TIENE NÚMEROS
FIJOS. Los lee todos de atas-bridge/rules.json al importar el módulo... Si un
número de rules.json no aparece aquí, es porque no se usa." Define Reglas 6/7
como las TRES condiciones (a)/(b)/(c) con umbrales leídos del JSON.

**Excerpto 2 — carga de reglas (líneas 85–95):**
```python
def _load_rule_conditions(rule_id: int) -> dict:
    try:
        import json as _json
        rules_path = HERE.parent / "atas-bridge" / "rules.json"
        raw = _json.loads(rules_path.read_text(encoding="utf-8"))
        for r in (raw.get("rules") or raw):
            if int(r.get("id", -1)) == rule_id:
                out = dict(r.get("conditions") or {})
                out["_enforcement"] = r.get("enforcement", "hard")
```

**Excerpto 3 — condición (c) compartida (línea 347):** `dom_vac_eval(symbol,
observe=True)` — "UNA sola implementación" para la puerta y el grabador; MET
exige ≥ venues_min venues del lado durante polls_min lecturas CONSECUTIVAS;
`observe=False` lee sin avanzar la racha (solo el poll del grabador cuenta
como observación). Incluye el estado SPLIT (venues votando a ambos lados →
"NO INTERPRETABLE") y UNEVALUABLE si la calibración de una venue falta.

**Excerpto 4 — publicación del sesgo (líneas 1257–1300):**
```python
    # ── El sesgo viene de la PUERTA ──
    # Regla 6 en cuarentena desde 2026-10-11: enforcement "disabled" en
    # rules.json por PRECAUCION, NO por anti-edge medido — su evidencia es
    # n_eff=2,7 sobre 159 filas solapadas (autocorr +0,97), que es ruido, y es
    # toda de la era del gate viejo. (...)
    r6_enforcement = _rule_enforcement(6)
    if r7_met:
        bias = "BULLISH"
    elif r6_met and r6_enforcement != "disabled":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    v["rule6"]["enforcement"] = r6_enforcement
    v["rule6"]["counts_toward_bias"] = (r6_enforcement != "disabled")
```
Más abajo, el comentario de retiro del scorecard: "pearson +0.0175 (ruido)...
el componente de peso 50 (dom_ema) media spread +27.3pp sobre top150/bottom150,
que con autocorr rho=+0.995 son ~1 observación independiente (z=0.35)" y cierra
con `v["bias"] = bias; v["unevaluable"] = unev; v["probability"] = "unvalidated"`.

# APÉNDICE C — Evidencia del scorecard retirado (commit `a93147f`)

```
commit a93147fd45d74aa5aff90a3e329c6b06fedd8153
Date:   Tue Oct 13 17:22:50 2026 -0500
refactor(reader): RETIRAR el scorecard — medido sin poder predictivo

El score 0-100 se elimina de todas las rutas de produccion. No se
recalibra ni se maquilla: se mide, se ve que no significa nada, y se
saca del camino.

MEDICION (1.205 filas de reader.db con forward_return resuelto):
  pearson(score, forward_return)  = +0.0175
  spearman(score, forward_return) = -0.0156
  tercil bajo : n=401  winrate 37.9%  retorno medio -0.382%
  [tercil medio/alto en el cuerpo completo; verdict.py cita tercil alto
   winrate 32.8%, retorno medio -0.392%]

verdict.scorecard() se conserva como funcion marcada RETIRADO, con la
medicion en su docstring, para poder reproducir lo que la retiro.
Ninguna ruta de produccion la llama.
```

Además, `reader/scorecard_calibration.json` (v1.0, 2026-10-11) declara sobre
sí misma: "NO es un modelo validado... Componentes con spread negativo medido
están INVERTIDOS o excluidos" — p.ej. `dom_ema`: raw_spread +27.3pp,
top150 win 44.0% vs bottom150 win 16.7% pero con autocorrelación del forward
return rho=+0.995 (≈1 observación independiente).

# APÉNDICE D — Backtest de la capa de vigilancia (metodología + resultados)

**Diseño:** event study, replay de los criterios de `reader/watch_early.py`
sobre klines históricos 1h/15m + funding histórico de Binance (11 pares:
ARB, BTC, ETH, HYPE, JUP, SOL, SPCX, SUI, XRP, NVDA, 1000PEPE), coste oficial
del sistema 11 bps round-trip. Criterios v2 evaluados: (A) |funding| ≥ 1.5×
baseline 0.01%; (B) 2h sin nuevo extremo de 24h (15m); (C) primer swing
fractal 1h 3+3 a favor. Cooldown 24h por par/lado. Entrada = close de la
barra de detección. Métricas side-adjusted.

**Resultados (n=1,842 banderas v2; 573 LONG / 1,269 SHORT):**

```
Variante  n     hit24_net  med_r24   med_r72   med_MFE   med_MAE   MFE_p75  MAE_p75
v2        1842  47.0%      -0.182%   -0.185%   -1.398%*  +1.533%   +0.999%  +4.488%
v1        1764  46.8%      -0.199%   -0.151%   -1.410%*  +1.554%   +0.974%  +4.458%
noB (A+C) 1853  46.9%      -0.189%   -0.178%   -1.385%*  +1.542%   +1.060%  +4.453%
(* med_MFE global con signo mixto LONG/SHORT; por lado, ver abajo)
```

**Corte por lado (v2):** LONG n=573 — hit 46.6%, med MFE +3.12% / med MAE
−3.27%, MFE p75 +6.47%. SHORT n=1,269 — hit 47.2%, med MFE −3.05% (adverso
inmediato), MFE p75 −1.27% → **los SHORT no aportan nada**.

**Corte LONG por profundidad de funding (el hallazgo):**
```
funding 1.5-3x  (n=418): hit 45.5%  med_r24 -0.41%  MFE_p75 +5.83%
funding 3-10x   (n=123): hit 48.0%  med_r24 -0.15%  MFE_p75 +9.73%
funding >10x    (n=32):  hit 56.2%  med_r24 +1.00%  MFE_p75 +11.22%
```
Gradiente monótono; único cell verde: LONG con funding ≤ −0.1% (10× baseline).

**Oportunidad:** 10.7% de las banderas tocó MFE ≥ 5% en 24h; 4.0% ≥ 10%.
**Por par:** HYPE 58.3% (n=36), SUI 53.8%, BTC 50.8% vs ARB 24.3% (n=37).

**Muestra de filas del CSV (formato: ts, sym, side, entry, funding, mfe24,
mae24, r24, r72):**
```
1782489600000,XRP,LONG,1.0441,-0.00015255,3.122,-0.651,2.442,0.613
1782828000000,JUP,LONG,0.2061,-0.00020144,16.691,-0.97,14.313,17.952
1785002400000,ARB,LONG,0.08314,-0.00016264,0.301,-1.479,-0.926,-5.846
1785110400000,ARB,LONG,0.08236,-0.00021107,0.389,-7.224,-6.119,-4.213
1786028400000,JUP,LONG,0.1827,-0.00018601,2.737,-1.861,0.328,2.901
1787374800000,HYPE,SHORT,79.31,0.0004707,-2.242,5.663,1.904,-1.724
1790247600000,ARB,LONG,0.21381,-0.00018198,5.996,-1.679,5.308,6.164
1790524800000,ARB,LONG,0.22035,-0.00016857,6.422,-9.217,-6.848,-6.417
```
**Caveats declarados:** n=32 en el cell verde es muestra corta; el backtest
usa entry al close de la barra de detección (sin slippage intrabar); el coste
solo entra en el hit-rate neto, no en MFE/MAE; sample sesgado a la era del
mercado que tocara vivir a cada par.

**Decisión tomada sobre esta evidencia (v3):** LONG-only con funding ≤ −0.001
(10×), SHORT desactivado, B en 15m se conserva (v1 vs v2 idénticos en
resultado: 46.8 vs 47.0 — la ventana no cambia la calidad, solo la latencia).

# APÉNDICE E — Medición v3 de las reglas del scanner (cross-venue)

De PLAN.md §7 (post-auditoría 2026-10-11): "La auditoría completa del repo
midió las tres reglas del scanner en la era v3: todas DROP (bruto ≈ 0, neto
−9.6/−11.3/−11.8 bps, t −5.6/−9.5/−12.5)". Reglas 1 y 2 RETIRADAS con su
medida escrita (`DROP refuted n=... t=...` en scanner-engine/rules.json vía
`promotion.py --retire`); la regla 4 quedó como única hipótesis direccional
viva (su estudio completo: PLAN §2 — n=662 con horizonte coherente, media
−0.0052%, t=−1.34, hit 48.0%).

La lección metodológica de esa medición (citada porque el auditor debe
conocerla): una versión previa dio un "edge negativo significativo"
(t=−2.37, SUI t=−4.81) que resultó **artefacto de un bug de horizonte** — los
pares de 5m medían 60s en vez de 300s. Corregido y recomputado: t=−1.34. La
regla escrita: "antes de reportar cualquier resultado que alcanza
significancia, recomputar una muestra al azar contra una fuente independiente
y exigir coincidencia exacta. Y desconfiar especialmente de un negativo
significativo: invertirlo es la forma más fácil de fabricar una estrategia".
