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
hash del bloque rules.json incrustado (verificable contra Apéndice A):  e046ed7cb8054406d2aa30097533587e076b1e585ceb6d989dff8876ad93274c
hash del bloque verdict.py incrustado (verificable contra Apéndice B):  5d73e376ed2ce1f0447f4a22347844a3cf03724433f066ca5e35eb208af6c98c
hash del CSV decodificado (verificable contra Apéndice F):              002f854c4351ecc3b140e6d18aa113a6dfd1af44af6e53e447051743a22f7129
(procedencia: los bloques A y B son copia exacta de los ficheros del repo;
 difieren de ellos solo en newlines finales de fence)
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
}```

# APÉNDICE B — `reader/verdict.py` COMPLETO (1,457 líneas, 71016 chars)

**sha256 del bloque incrustado (sin el fence markdown):** `5d73e376ed2ce1f0447f4a22347844a3cf03724433f066ca5e35eb208af6c98c`

```python
"""Fase 3 — motor de veredictos. Implementa rules.json v3.3 como salida principal.

POR QUÉ ESTE MÓDULO
===================
El motor viejo generaba señales y no tenía capa de veredicto: por eso sus
lecturas no sostenían una conclusión. Aquí las REGLAS son la salida, y el
scorecard es un RANKING que nunca sustituye a la puerta.

Las tres reglas duras que este módulo implementa (verificadas contra
atas-bridge/rules.json v3.3, no de memoria):

  Regla 6 — Distribution (bajista). Las TRES (umbrales LEIDOS de rules.json):
      (a) CVD cae 2+ barras consecutivas          [cvd_consecutive_down_min]
      (b) footprint: 4+ niveles con >= sell_pct% venta en el lado bid
                                                        [footprint_sell_pct_min]
      (c) consenso de libro multi-venue del scanner: N venues con asks
          pesados fuera de banda, sostenido P lecturas del reader
                                   [dom_source, dom_vac_thr, dom_vac_venues_min,
                                    dom_vac_polls_min - Fix 2-A, 2026-10-11]
  Regla 7 — Accumulation (alcista). Las TRES:
      (a) CVD sube 2+ barras consecutivas          [cvd_consecutive_up_min]
      (b) footprint: 3+ niveles con >= buy_pct% compra en el lado ask
                                                        [footprint_buy_pct_min]
      (c) consenso de libro multi-venue del scanner: N venues con bids
          pesados fuera de banda, sostenido P lecturas del reader
                                   [dom_source, dom_vac_thr, dom_vac_venues_min,
                                    dom_vac_polls_min - Fix 2-A, 2026-10-11]

  ESTE FICHERO NO TIENE NUMEROS FIJOS. Los lee todos de atas-bridge/rules.json
  al importar el modulo, y avisa por stdout si no puede leerlos. Si un numero de
  rules.json no aparece aqui, es porque no se usa.
  Regla 4 — Score, Don't Predict. Scorecard 0-100; >=60 alcista, <=40 bajista.

LA ASIMETRÍA ES INTENCIONAL: regla 6 pide 4 niveles, regla 7 pide 3. Un footprint
con 3 niveles predominantemente vendedores NO satisface ninguna de las dos.

TRES REGLAS DE DISCIPLINA QUE ESTE MÓDULO APLICA
=================================================
1. El sesgo viene de la PUERTA, no del scorecard. Si las reglas no disparan,
   el veredicto es NEUTRAL aunque el score sea 66. Cuando score y puerta
   discrepan, se declara el conflicto y la razón: miden cosas distintas.
2. `probability: unvalidated` SIEMPRE que no exista un hit rate validado por
   backtest. Hoy no existe, así que siempre.
3. Una condición que no se puede evaluar NO es "no satisfied": es
   UNEVALUABLE, y fuerza NEUTRAL. Una regla que nunca puede evaluarse es peor
   que no tenerla, porque parece estar activa y no lo está.

Uso:
  python verdict.py --json          # veredicto de una lectura
  python verdict.py --text          # salida para Telegram
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from pathlib import Path

BRIDGE = "http://127.0.0.1:8000"
LIVE_MAX_AGE_S = 300.0
HERE = Path(__file__).resolve().parent

# ── Umbrales: LEIDOS de atas-bridge/rules.json v3.3 ──
# 2026-10-07: estaban transcritos a mano aqui. Eso es exactamente lo que causo el
# bug que se acaba de arreglar: las reglas 6/7 declaraban
# `dom_persistence_polls_min: 3` y el codigo, al transcribir solo los tres
# numeros que le interesaban, se dejo ese campo fuera — y nadie lo noto porque
# los otros tres si coincidian. Una transcripcion manual es una fuente de verdad
# mas que puede quedarse obsoleta en silencio.
#
# Ahora se leen del fichero. Si rules.json cambia, esto cambia con el, y si el
# fichero no existe o no trae el campo, se avisa en vez de adivinar.
#
# 2026-10-07 — ESTA FUNCION DEVUELVE UN DICT PLANO, con las conditions
# mezcladas con el enforcement bajo una clave "_enforcement". Antes devolvia
# {"conditions": {...}, "enforcement": ...} y cada consumidor tenia que acordarse
# de descender un nivel. La regla 2 se olvido (bug: leia spike_multiple del
# nivel equivocado y caia al default 2.0 en silencio), y cualquier consumidor
# nuevo tiene el mismo error a la vista.
#
# Con el dict plano no existe el error: o hay la clave o no la hay.
# `_enforcement` lleva el prefijo para que no choque con ningun nombre de campo
# de las condiciones.
def _load_rule_conditions(rule_id: int) -> dict:
    try:
        import json as _json
        rules_path = HERE.parent / "atas-bridge" / "rules.json"
        raw = _json.loads(rules_path.read_text(encoding="utf-8"))
        for r in (raw.get("rules") or raw):
            if int(r.get("id", -1)) == rule_id:
                out = dict(r.get("conditions") or {})
                out["_enforcement"] = r.get("enforcement", "hard")
                out["_title"] = r.get("title", "")
                out["_present"] = True          # la regla EXISTE en el JSON
                return out
        print(f"[verdict] AVISO: la regla {rule_id} NO existe en rules.json "
              f"(version {raw.get('version')}). Se usan umbrales por defecto.")
    except Exception as _e:            # noqa: BLE001
        print(f"[verdict] AVISO: no se pudo leer rules.json regla {rule_id}: {_e}")
    # Sin fichero o sin esa regla: se marca _present=False para que el consumidor
    # pueda distinguir "la regla dice que no" de "no hay regla que consultar".
    return {"_enforcement": "hard", "_title": "", "_present": False}


# Las condiciones son PLANAS: _R6["dom_vac_thr"], sin descender niveles.
_R6 = _load_rule_conditions(6)
_R7 = _load_rule_conditions(7)
_R2 = _load_rule_conditions(2)

# FIX 2-A (docs/FIXES-gate-DOM.md, 2026-10-11): la condición (c) ya NO es la EMA
# del libro de Binance (std 7.5, 19-23 cambios de estado/día — no era un gate).
# Ahora es consenso multi-venue del scanner: N venues con touch_imbalance del
# lado de la regla más allá de la banda, sostenido P lecturas del reader.
# La fuente se declara en rules.json ("dom_source") y el test de contrato
# comprueba que este código lee EXACTAMENTE lo que el JSON declara.
DOM_SOURCE = "scanner_multi_venue"
if _R6.get("dom_source") != DOM_SOURCE or _R7.get("dom_source") != DOM_SOURCE:
    print(f"[verdict] AVISO: rules.json declara dom_source="
          f"{_R6.get('dom_source')!r}/{_R7.get('dom_source')!r} y este código "
          f"solo implementa {DOM_SOURCE!r} — contrato roto, no evaluar en serio.")

RULE6 = {"streak": int(_R6.get("cvd_consecutive_down_min", 2)),
         "levels_min": int(_R6.get("footprint_levels_min", 4)),
         "vac_thr": float(_R6.get("dom_vac_thr", 0.15)),
         "vac_z": float(_R6.get("dom_vac_z_min", 1.0)),
         "vac_venues": int(_R6.get("dom_vac_venues_min", 3)),
         "vac_polls": int(_R6.get("dom_vac_polls_min", 2)),
         "enforcement": _R6.get("_enforcement", "hard")}

RULE7 = {"streak": int(_R7.get("cvd_consecutive_up_min", 2)),
         "levels_min": int(_R7.get("footprint_levels_min", 3)),
         "vac_thr": float(_R7.get("dom_vac_thr", 0.15)),
         "vac_z": float(_R7.get("dom_vac_z_min", 1.0)),
         "vac_venues": int(_R7.get("dom_vac_venues_min", 3)),
         "vac_polls": int(_R7.get("dom_vac_polls_min", 2)),
         "enforcement": _R7.get("_enforcement", "hard")}

# La regla 7 lee el lado COMPRADOR del consenso y la 6 el VENDEDOR: la
# acumulación pide bids descansando bajo el precio; la distribución, asks encima.
RULE6_SIDE, RULE7_SIDE = "sell", "buy"

_UNEVALUABLE = "UNEVALUABLE"

# ── CONDICIÓN (c): CALIBRACIÓN POR VENUE ────────────────────────────────────
#
# 2026-10-15 — MEDIDO, y es la causa de que la condición (c) fuera MUDO.
#
# La (c) comparaba `book_imb_5` de 4 venues contra un umbral ABSOLUTO. Medido
# sobre la muestra historica (dom_vac_raw de reader.db), los venues NO son
# comparables entre sí:
#
#   venue    media     std      >+0.25%   <-0.25%
#   binance  +0.080    0.214      21.1       8.9
#   okx      -0.199    0.145       1.5      38.2
#   kraken   +0.129    0.499      41.9      25.1
#   bybit    +0.081    0.155      17.4       1.5
#
# OKX cruza +0.25 el 1,5% de las veces: por ESTRUCTURA no puede votar al lado
# comprador, así que exigir 3 de 4 equivalía a exigir los otros tres a la vez.
# Kraken, con std 3,4x la de bybit, cruza el umbral el 42% con puro ruido. Un
# umbral absoluto mide "qué venue vive fuera de su propia banda", no acuerdo.
#
# El arreglo: normalizar cada venue contra SU propia distribución. Un voto pasa
# a significar "este venue está a `vac_z` sigmas de su media", que es la única
# forma de que los cuatro números sean comparables entre sí.
#
# Selectividad CONSERVADA, no aflojada: medido sobre 331 ciclos,
#   umbral absoluto 0.25 / 3 de 4  ->  consenso en el 6,0%
#   normalizado z>=1.0 / 3 de 4    ->  consenso en el 5,7%
# Es decir, el gate no se ha vuelto más permisivo; se ha vuelto comparable.
#
# Generado por build_dom_vac_calibration.py desde reader.db. Si falta o un venue
# no está calibrado, la condición es UNEVALUABLE — nunca se vuelve al umbral
# absoluto en silencio, que sería reintroducir el defecto.
_CAL_VAC_PATH = HERE / "dom_vac_calibration.json"
_CAL_VAC: dict = {}


def _load_vac_calibration() -> dict:
    """Lee la calibración por venue. Cachea; vacío si no existe o no es legible."""
    global _CAL_VAC
    if _CAL_VAC or not _CAL_VAC_PATH.exists():
        return _CAL_VAC
    try:
        _CAL_VAC = json.loads(_CAL_VAC_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        _CAL_VAC = {}
    return _CAL_VAC


# Estado propio del mercado PARTIDO. No es MET (no abre la puerta) pero tampoco
# FAIL: "los venues se contradicen" es información, y antes se tiraba.
_SPLIT = "SPLIT"


# 2026-10-15 — VENTANA DE PERSISTENCIA de las condiciones, declarada en
# rules.json (`condition_window_s`). 0 = conjunción estricta en el mismo tick,
# que es el comportamiento historico. Ver el bloque de condition_window_eval()
# para la medicion que justifica el valor.
COND_WINDOW_S = float(_R6.get("condition_window_s", 0) or 0)
if float(_R7.get("condition_window_s", 0) or 0) != COND_WINDOW_S:
    print(f"[verdict] AVISO: la regla 6 declara condition_window_s="
          f"{COND_WINDOW_S:g} y la 7 {_R7.get('condition_window_s')!r}; "
          f"se usa la de la 6.")

# ── Condición (c) multi-venue: consenso de libro N-de-M del scanner ─────────
# La fuente es scanner-engine (poller continuo, 4 venues, UNEVALUABLE declarado
# cuando falta dato — nunca el libro de una GUI). La persistencia vive en un
# fichero de estado porque cada lectura del reader es un ciclo del gate: dos
# lecturas consecutivas por encima de banda son lo que la regla llama "hold".

SCANNER_URL = "http://127.0.0.1:8090"
_DOM_GATE_STATE = HERE / "dom_gate_state.json"


def _symbol_to_scanner_pair(symbol: str) -> str | None:
    """'XRPUSDT' -> 'XRP/USD', solo si el scanner tiene el par registrado."""
    base = str(symbol or "").upper().replace("-", "")
    changed = True
    while changed:
        changed = False
        for suffix in ("SWAP", "USDT", "USD", "PERP"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[:-len(suffix)]
                changed = True
                break
    return base + "/USD" if base else None


def _scanner_book_imb5(symbol: str, timeout: float = 4.0) -> dict | None:
    """Imbalance del libro top-5 por venue (escala ±1), desde el scanner.
    None si no hay dato vivo — y None NO es 0: 'no hay dato' y 'libro
    balanceado' son claims distintos y solo uno de ellos es bajista.

    NOTA de calibración (2026-10-11): el touch (mejor nivel) se DESCARTÓ como
    base del consenso — medido, es estructuralmente unilateral (mediana
    |x-0.5|=0.275) y desacoplado entre venues (consenso 3-de-4: 0 en 480
    ciclos). El consenso vive en el libro acumulado, misma construcción que
    los números DOM de ATAS/Binance.
    """
    pair = _symbol_to_scanner_pair(symbol)
    if not pair:
        return None
    try:
        import urllib.parse as _up
        url = f"{SCANNER_URL}/features?pair={_up.quote(pair, safe='')}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        x = (d or {}).get(pair) or {}
        if not x.get("live"):
            return None
        imb = x.get("book_imb_5") or {}
        return {k: float(v) for k, v in imb.items() if isinstance(v, (int, float))} or None
    except Exception:
        return None


def _dom_gate_load() -> dict:
    try:
        return json.loads(_DOM_GATE_STATE.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        return {}


def _dom_gate_save(s: dict) -> None:
    try:
        tmp = _DOM_GATE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        tmp.replace(_DOM_GATE_STATE)
    except Exception:                               # noqa: BLE001
        pass                                        # el gate degrada, no truena


# 2026-10-15 — BUG DE PERSISTENCIA (mismo patrón que ya costó un día aquí).
#
# rules.json declara `dom_vac_polls_min: 2` = "lecturas del reader (~30s)".
# Pero UN ciclo del supervisor invoca el gate DOS veces sobre el MISMO snapshot
# del scanner: `verdict_recorder.record` (reader_supervisor.py:261) y, si llegó
# a registrar, el notificador vía `build_verdict` (reader_supervisor.py:306 →
# verdict.py:878). Las dos lecturas ocurren separadas por milisegundos y el
# scanner solo refresca cada ~12s, asi que la "persistencia" exigida por AT-6 y
# AT-7 se cumplia con UNA sola observacion real: el gate direccional disparaba
# con media evidencia.
#
# Solución: una racha solo avanza cuando la observación es NUEVA. Cualquier
# segunda llamada dentro de _DOM_GATE_POLL_S LEE la racha sin moverla, así que
# el doble tick deja de ser posible venga de donde venga.
_DOM_GATE_POLL_S = 15.0    # una observación nueva del reader (ciclo ~30s)
# Y no sobrevive a una parada: si nadie ha leído el gate durante más de esto,
# las lecturas anteriores dejan de valer. Una caída de ATAS o una pausa del
# supervisor no puede dar por buena una racha empezada horas atrás — era el
# contrato prometido por el propio docstring ("la racha muere") y solo lo
# cumplía si moría el scanner.
_DOM_GATE_STALE_S = 120.0


def _dom_gate_tick(symbol: str, buy_met: bool, sell_met: bool,
                   observe: bool = True) -> tuple[int, int]:
    """Rachas consecutivas de consenso por lado. Un fallo en cualquier fuente
    (scanner caído, venues insuficientes) pasa buy_met/sell_met=False y la
    racha muere — conservador por diseño.

    Solo avanza una vez por observación real: ver _DOM_GATE_POLL_S. Con
    `observe=False` solo se LEE (ver dom_vac_eval).
    """
    st = _dom_gate_load()
    rec = st.get(symbol) or {"buy": 0, "sell": 0}
    if not observe:
        return int(rec.get("buy", 0)), int(rec.get("sell", 0))
    now = time.time()
    ts = float(rec.get("ts") or 0.0)
    age = now - ts
    if age < _DOM_GATE_POLL_S:
        # Misma observación (el otro hijo del mismo ciclo): se lee, no se avanza.
        return int(rec.get("buy", 0)), int(rec.get("sell", 0))
    if age > _DOM_GATE_STALE_S:
        rec["buy"], rec["sell"] = 0, 0      # la racha muere con la parada
    rec["ts"] = now
    rec["buy"] = int(rec.get("buy", 0)) + 1 if buy_met else 0
    rec["sell"] = int(rec.get("sell", 0)) + 1 if sell_met else 0
    st[symbol] = rec
    _dom_gate_save(st)
    return rec["buy"], rec["sell"]


def _vac_z(tb: dict, cal_venues: dict) -> dict:
    """z de cada venue contra SU propia distribucion: (x - media) / std.

    Es lo que hace comparables los cuatro libros. Sin esto, un umbral absoluto
    mide "que venue vive fuera de su banda" (okx cruza +0.25 el 1,5% de las
    veces y por estructura no puede votar compra; kraken cruza el 42% con
    ruido), no acuerdo.
    """
    out = {}
    for k, v in tb.items():
        st = cal_venues.get(k) or {}
        m, sd = st.get("mean"), st.get("std")
        if m is None or not sd:
            continue
        out[k] = (float(v) - float(m)) / float(sd)
    return out


def dom_vac_eval(symbol: str, observe: bool = True) -> dict:
    """La condición (c) completa para las reglas AT-6/AT-7, compartida por la
    puerta (build_verdict) y por el grabador (verdict_recorder): UNA sola
    implementación, o las dos desincronizan como ya pasó con los umbrales.

    Devuelve {"buy": {"state", "detail"}, "sell": {...}, "venues": n} donde
    state es MET/FAIL/UNEVALUABLE. MET exige: >= vac_venues venues del lado,
    durante vac_polls lecturas consecutivas del reader.

    `observe=False` LEE la racha sin avanzarla. Solo el poll del grabador
    (`verdict_recorder.record`, el ciclo real del reader) es una observación:
    sin esto, el notificador del MISMO ciclo y `live_reader --text` (invocado a
    mano) también contarían, y dos lecturas de un CLI hecho a pulso podrian
    llevar la racha a MET sin que el grabador nunca la observara.
    """
    # 2026-10-15 — cada lado con LOS UMBRALES QUE DECLARA. Antes se leían los de
    # la regla 7 para los DOS lados mientras la etiqueta anunciaba los de la 6
    # (`c_vac_sell_{RULE6['vac_venues']}` / "... {RULE6['vac_thr']:g}"):
    # coincidían solo porque rules.json hoy pone 0.25/3/2 en las dos, así que
    # ni el test de contrato lo veía. Si alguien tunesa solo la 6, el motor
    # seguiría aplicando los de la 7 y publicando los de la 6 — la clase
    # exacta de "declara una cosa y evalúa otra" que ya costó un día.
    tb = _scanner_book_imb5(symbol)
    sides = (("buy", RULE7), ("sell", RULE6))   # 7 = acumulación (lado bid)
    if tb is None:                              # 6 = distribución (lado ask)
        _dom_gate_tick(symbol, False, False, observe=observe)
        return {"buy": {"state": "UNEVALUABLE",
                        "detail": "scanner multi-venue no disponible"},
                "sell": {"state": "UNEVALUABLE",
                         "detail": "scanner multi-venue no disponible"},
                "venues": 0}
    need = max(RULE6["vac_venues"], RULE7["vac_venues"])
    if len(tb) < need:
        _dom_gate_tick(symbol, False, False, observe=observe)
        det = (f"solo {len(tb)} venue(s) vivo(s); el consenso pide {need}")
        return {"buy": {"state": "UNEVALUABLE", "detail": det},
                "sell": {"state": "UNEVALUABLE", "detail": det},
                "venues": len(tb)}

    # ── Normalización por venue contra su propia distribución ──
    #
    # Antes: `v > vac_thr` con vac_thr absoluto (0.25). Medido: eso media
    # "qué venue vive fuera de su banda", no acuerdo (okx cruza +0.25 el 1,5%
    # de las veces y por estructura jamás puede votar compra; kraken cruza el
    # 42% con ruido). Ahora cada venue se compara consigo mismo.
    cal = _load_vac_calibration()
    cal_venues = (cal.get("venues") or {}) if cal else {}
    uncalibrated = [k for k in tb if k not in cal_venues]
    if not cal_venues or uncalibrated:
        _dom_gate_tick(symbol, False, False, observe=observe)
        det = ("calibracion dom_vac ausente/vieja"
               + (f" (venues sin calibrar: {', '.join(sorted(uncalibrated))})"
                  if uncalibrated else "")
               + "; regenerar con build_dom_vac_calibration.py")
        return {"buy": {"state": "UNEVALUABLE", "detail": det},
                "sell": {"state": "UNEVALUABLE", "detail": det},
                "venues": len(tb)}

    zthr = max(RULE6["vac_z"], RULE7["vac_z"])
    counts = {s: sum(1 for zv in _vac_z(tb, cal_venues).values()
                     if (zv >= zthr if s == "buy" else zv <= -zthr))
              for s, R in sides}
    b_n, s_n = _dom_gate_tick(symbol, counts["buy"] >= RULE7["vac_venues"],
                               counts["sell"] >= RULE6["vac_venues"],
                               observe=observe)
    streaks = {"buy": b_n, "sell": s_n}
    out = {"buy": {}, "sell": {}, "venues": len(tb),
           # 2026-10-15 — LOS DATOS CRUDOS, no solo el veredicto de la puerta.
           #
           # La condicion (c) FALLA su propio criterio pre-registrado en
           # rules.json ("N=10 transiciones MET<->no-MET por dia"): medido
           # 61-63/dia sobre 27,5 h reales, contra 19-23/dia del gate viejo que
           # sustituyo. Y la causa esta medida: los venues NO son comparables
           # (kraken 60% de las muestras por encima de +0,25 y solo 16,9% por
           # debajo de -0,25; okx con std 0,34 y 10,6% en el lado vendedor), asi
           # que un umbral ABSOLUTO mide "que venues viven fuera de la banda",
           # no acuerdo.
           #
           # El candidato es normalizar por venue contra su propia distribucion.
           # Pero eso NO se puede validar con una muestra de minutos: la tasa por
           # dia necesita horas. Sin estos crudos, cada intento de rediseno
           # obliga a volver a muestrear a mano. Con ellos, la decision se toma
           # offline sobre el historico real.
           "raw": {k: round(float(v), 4) for k, v in tb.items()},
           "counts": dict(counts),
           "detail": (f"{counts['buy']}C/{counts['sell']}V venues por lado "
                      f"({len(tb)} vivos); racha C={b_n} V={s_n}")}
    for side, R in sides:
        count, streak = counts[side], streaks[side]
        other = "sell" if side == "buy" else "buy"
        # PARTIDO = los DOS lados tienen votos. No basta con que el lado opuesto
        # los tenga: un mercado UNANIME (4 venues comprando, 0 vendiendo) dejaba
        # el lado vendedor marcado como SPLIT con la condicion anterior
        # (`counts[other] >= 1`), que es exactamente al reves de lo que significa.
        # Lo detecto el test test_n_of_m_counting, que venia del commit anterior.
        parted = counts["buy"] >= 1 and counts["sell"] >= 1
        # 2026-10-15 — SPLIT: estado propio del mercado CONTRADICHO.
        #
        # Antes, si kraken votaba compra y okx venta en el mismo instante, los dos
        # lados salían FAIL, que es INDISTINGUIBLE de "no hay señal". Medido: con
        # el umbral absoluto, el 27,8% de 331 ciclos eran mercado partido y todos
        # se publicaban como FAIL. Eso tiraba información real: "los venues se
        # contradicen" no es "no hay lecturas".
        #
        # SPLIT NO abre la puerta (no es MET), pero sí se nombra. Conservador por
        # diseño: un gate que no sabe qué hace el libro no debe disparar.
        if parted:
            out[side] = {"state": _SPLIT,
                         "detail": (f"mercado PARTIDO: {counts[side]} venue(s) a favor "
                                    f"y {counts[other]} en contra (z>={zthr:g}); "
                                    f"el consenso no es interpretable")}
        elif count < R["vac_venues"]:
            out[side] = {"state": "FAIL",
                         "detail": (f"{count} venues del lado {side} con z>={zthr:g} "
                                    f"(piden {R['vac_venues']})")}
        elif streak < R["vac_polls"]:
            out[side] = {"state": "UNEVALUABLE",
                         "detail": f"consenso {side} en racha {streak} de {R['vac_polls']} lecturas"}
        else:
            out[side] = {"state": "MET",
                         "detail": (f"{count} venues del lado {side} con z>={zthr:g}, "
                                    f"racha {streak}>= {R['vac_polls']}")}
    return out


# ── VENTANA DE PERSISTENCIA DE LAS CONDICIONES (reglas 6 y 7) ───────────────
#
# 2026-10-15 — MEDIDO, y es la causa de que el motor fuera MUDO.
#
# Durante el rally de JUP (08:54 0.2903 -> 10:17 0.3151, +8,55%) el motor emitio
# 86 NEUTRAL y 1 solo BULLISH, a las 10:06 con el rally ya hecho (+8%).
#
# No faltaba evidencia: en esa ventana (a) estuvo MET 34 veces, (b) 41 veces y
# (c) 10 veces. Lo que fallaba es que la regla exige las TRES en la MISMA
# lectura de 30 s, y son tres sensores que no conmutan a la vez. En la linea
# temporal se ve el near-miss: a las 09:37 (a)=MET (b)=MET (c)=FAIL, y (c)
# habia estado MET dos minutos antes, a las 09:35. La evidencia estaba y se
# descartaba por el desfase de muestreo.
#
# Medido sobre las 3.339 filas del ledger:
#   R7 (a)Y(b)Y(c) en el mismo tick -> 14 filas (0,42%)   primer disparo 10:06
#   R7 cada una vista en 60 s       -> primer disparo 09:35 @ 0.3022
#
# La ventana NO anade evidencia: impide tirar la que ya habia. Y el parametro NO
# esta optimizado: la hora del primer disparo es IDENTICA a 60, 180, 300 y 600 s,
# asi que 60 s es la intervencion minima suficiente, no un ajuste fino. (Misma
# prueba que exige quant-research: una meseta, no un pico rodeado de vecinos
# malos.)
#
# NO VALIDADO COMO EDGE. Que dispare antes es medible y esta medido. Que esos
# disparos GANEN no lo esta, y no se puede medir con lo que hay: 209 h de
# registro al 13% de cobertura dejan ~35 observaciones independientes a 60 min
# tras deduplicar. Se trata como regla NO validada, igual que la 7 ya estaba.
_COND_KEYS = ("r6_a", "r6_b", "r6_c", "r7_a", "r7_b", "r7_c")


def _cond_state_path():
    return HERE / "condition_window_state.json"


def condition_window_eval(symbol: str, states: dict, window_s: float,
                          observe: bool = True) -> dict:
    """Eleva a MET las condiciones que estuvieron MET dentro de la ventana.

    Solo puede elevar FAIL -> MET, nunca tocar un UNEVALUABLE actual: la falta
    de dato sigue bloqueando, que es el contrato de la regla.

    Y solo OBSERVA la lectura del grabador (el ciclo real del reader); la puerta
    LEE. Contarlo dos veces por ciclo es exactamente el fallo que ya se arreglo
    en la condicion (c).
    """
    if window_s <= 0:
        return states
    now = time.time()
    st = {}
    try:
        if _cond_state_path().exists():
            st = json.loads(_cond_state_path().read_text(encoding="utf-8")) or {}
    except Exception:                               # noqa: BLE001
        st = {}
    hist = st.get(symbol) or []
    if observe:
        hist.append([now] + [states.get(k) for k in _COND_KEYS])
    cutoff = now - window_s
    hist = [h for h in hist if h and h[0] >= cutoff]
    st[symbol] = hist
    if observe:
        try:
            tmp = _cond_state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
            tmp.replace(_cond_state_path())
        except Exception:                           # noqa: BLE001
            pass                                    # degrada, no truena
    out = dict(states)
    for i, k in enumerate(_COND_KEYS):
        if out.get(k) in (_UNEVALUABLE, "MET"):
            continue
        if any(len(h) > i + 1 and h[i + 1] == "MET" for h in hist):
            out[k] = "MET"
    return out


# ── COBERTURA DE REGLAS ────────────────────────────────────────────────────
# Que reglas declara rules.json y cuales implementa REALMENTE este modulo.
#
# El JSON declara 8 (v3.4.1). Este modulo implementa 2, 6 y 7 como puertas
# direccionales, 3 como limite de salida, y 8 como registro de perdidas (que se
# dispara desde verdict_recorder al resolver un veredicto, no aqui). La 5 es una
# regla de CONDUCTA — "no anclar en patrones pasados" — que un programa no puede
# comprobar; lo que si hace el sistema es no persistir el analisis anterior como
# entrada, y eso ya ocurre porque cada veredicto se evalua solo con la lectura
# actual.
#
# La 4 esta RETIRADA en rules.json (enforcement=disabled, 2026-10-13). Su
# scorecard() sobrevive solo para reproducir la medicion que lo retiro (a93147f)
# y NINGUNA ruta de produccion lo llama: declararla implementada era anunciar
# una regla que ya no corre — que es justo lo que hizo `_IMPLEMENTED[4]` hasta
# el 2026-10-16.
#
# La 1 no tiene codigo propio porque es la condicion (a) de las reglas 6 y 7.
_IMPLEMENTED = {
    1: "condicion (a) de las reglas 6 y 7: exige 2+ barras consecutivas",
    2: "rule2_exhaustion() sobre los deltas por barra",
    3: "solo puede emitir 'unvalidated' (lineas probability)",
    6: "3 condiciones AND, umbrales de rules.json",
    7: "3 condiciones AND, umbrales de rules.json",
    8: "_audit_record_loss(), llamada desde verdict_recorder al resolver",
}
_NOT_IMPLEMENTED = {
    4: ("scorecard() + SCORE_BANDS existen en el modulo pero NINGUNA ruta de "
        "produccion los llama: retirados en a93147f tras medirse sin poder "
        "predictivo (pearson +0.0175, spearman -0.0156 sobre 1.205 filas; "
        "rules.json los marca disabled). Declararla implementada era anunciar "
        "una regla retirada."),
    5: "regla de conducta: no comprobable por codigo",
}


def _rule_coverage() -> list:
    """Lee rules.json y declara, por regla, si este modulo la implementa."""
    out = []
    try:
        import json as _json
        raw = _json.loads((HERE.parent / "atas-bridge" / "rules.json")
                          .read_text(encoding="utf-8"))
        rules = raw.get("rules") or []
    except Exception as _e:            # noqa: BLE001
        return [{"id": None, "error": f"no se pudo leer rules.json: {_e}"}]
    for r in rules:
        rid = int(r.get("id", -1))
        if rid in _IMPLEMENTED:
            impl, why = True, _IMPLEMENTED[rid]
        elif rid in _NOT_IMPLEMENTED:
            impl, why = False, _NOT_IMPLEMENTED[rid]
        else:
            impl, why = False, "sin implementacion en este modulo"
        out.append({"id": rid, "title": r.get("title", ""),
                    "enforcement": r.get("enforcement", "hard"),
                    "implemented": impl, "how": why})
    return out


# Regla 2 (CVD extremes = exhaustion). Sus dos campos estaban escritos a mano
# dentro de rule2_exhaustion(); despues se leyeron del JSON, pero del nivel
# equivocado del dict anidado y caian al default en silencio. Ver 9d0e90f.
# _R2 se cargo arriba, junto a _R6/_R7, ahora que el dict es plano.
RULE2 = {"spike_multiple": float(_R2.get("spike_multiple", 2.0)),
         "min_prior_bars": int(_R2.get("min_prior_bars", 3))}
if not _R2.get("_present"):
    print("[verdict] AVISO: la regla 2 no esta en rules.json; "
          "spike_multiple usara el default 2.0.")
# 2026-10-07 — Umbrales del footprint, LEIDOS de rules.json. Los dos son
# `footprint_sell_pct_min` / `footprint_buy_pct_min`; las reglas 6 y 7 los
# declaran iguales, asi que se toma el de la 6 y se avisa si difieren.
_FP_PCT = None
for _rid, _r in ((6, _R6), (7, _R7)):
    _v = _r.get("footprint_sell_pct_min", _r.get("footprint_buy_pct_min"))
    if _v is None:
        continue
    if _FP_PCT is None:
        _FP_PCT = float(_v)
    elif float(_v) != _FP_PCT:
        print(f"[verdict] AVISO: regla {_rid} declara {_v}% pero la otra declara "
              f"{_FP_PCT}%. Se usa {_FP_PCT}%. Las reglas deben coincidir.")
FOOTPRINT_PCT = 70.0 if _FP_PCT is None else _FP_PCT
if _FP_PCT is None:
    print("[verdict] AVISO: rules.json no declara footprint_sell_pct_min; "
          "se usa el default 70/30, que puede NO cumplir rules.json.")

if not _R6 or not _R7:
    print("[verdict] AVISO: rules.json no leido; se usan umbrales por defecto. "
          "Los veredictos pueden NO cumplir rules.json.")
SCORE_BANDS = {"bullish_min": 60, "bearish_max": 40}
WEIGHTS = {"footprint_depth": 25, "dom_imbalance": 25, "tape_ratio": 20,
           "volume_climax": 15, "footprint_confirmation": 15}


def _rule_enforcement(rule_id: int) -> str:
    """'hard' | 'soft' | 'disabled', leido de rules.json. Default 'hard'.

    Una regla 'disabled' sigue evaluandose y se publica su detalle, pero no
    cuenta para el sesgo. Sirve para poner en cuarentena una regla con evidencia
    en contra sin perder la trazabilidad de por que se cumple.
    """
    return _load_rule_conditions(rule_id).get("_enforcement", "hard")


# El v2 del scorecard se reprodujo inyectando la calibracion aqui. Antes se
# cargaba desde disco al importar el modulo, lo que hacia que este motor
# leyera un .json en cada arranque aunque no lo fuera a usar nunca: una
# dependencia de runtime sobre una medicion retirada.
_CAL_OVERRIDE = None


# ── SCORECARD: RETIRADO DE PRODUCCION (2026-10-13, commit a93147f) ──────────
# MEDICION sobre 1.205 filas de reader.db con forward_return resuelto:
#
#   pearson(score, forward_return)  = +0.0175    (ruido)
#   spearman(score, forward_return) = -0.0156
#   tercil alto ("mejor" score): winrate 32,8%, retorno medio -0.392%
#   tercil bajo               : winrate 37,9%, retorno medio -0.382%
#
# El "mejor" score ganaba MENOS y perdia MAS que el peor. El componente de
# peso 50 (dom_ema) media spread +27,3pp sobre top150/bottom150, pero con
# autocorrelacion de forward_return rho=+0.995 eso equivale a ~1 observacion
# independiente (z=0.35): la calibracion no tenia respaldo estadistico.
#
# QUEDE ESTE CODIGO, POR QUE
# =========================
# _CAL, _percentile_rank(), _scorecard_percentile(), _scorecard_v341() y
# scorecard() NO los llama ninguna ruta de produccion: build_verdict() dejo de
# publicar v["scorecard"] y v["score_gate_conflict"]. Se conservan para poder
# REPRODUCIR la medicion que lo retiro -- incluido el v2 percentile-median que
# se construyo el 2026-10-11 sobre los mismos 1.205 registros, y que sufrio la
# misma falta de respaldo al ser medido sobre la misma muestra autocorrelada.
#
# Mover scorecard_calibration.json a reader/build_scorecard_calibration.py y
# ejecutar ese script reconstruye la calibracion completa. Despues:
#   verdict._CAL_OVERRIDE = json.load(open("calib_features.json"))
#   verdict.scorecard(None, None, ema, None, None, sell_h, buy_h)
#
# SI ALGUIEN REACTIVA EL SCORECARD: no reutilices el spread +27,3pp de esta
# cabecera como evidencia. Con rho=+0.995 no es un spread de 1.205
# observaciones, es aproximadamente una. Ver LINT en verdict.py.


def _get(path: str, timeout: float = 25.0):
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:70]}"}


# ── Regla 2: los extremos de CVD son AGOTAMIENTO, no continuación ──
def rule2_exhaustion(cvd: float | None, high: float | None, low: float | None,
                     prev_high: float | None, prev_low: float | None) -> dict:
    """Un salto de CVD >2x el máximo previo de una barra es agotamiento.

    Flag como agotamiento; NO llamar continuación en ninguna dirección.
    """
    if cvd is None:
        return {"applicable": False, "reason": "sin CVD"}
    out = {"applicable": False}

    # 2026-10-07 — TRES DEFECTOS ARREGLADOS aqui:
    #
    # (1) `spike_multiple` (2.0) y `min_prior_bars` (3) estan en rules.json y
    #     estaban escritos a mano. Mismo fallo que la condicion (c) de 6/7.
    # (2) `abs(high) > 2*abs(prev_high) > 0` es una COMPARACION ENCADENADA de
    #     Python, no lo que parece. Se evalua como
    #     (abs(high) > 2*abs(prev_high)) AND (2*abs(prev_high) > 0).
    #     Con prev_high == 0 —el estado inicial, y el valor por defecto en el
    #     puente— la segunda mitad es 0 > 0 = False, asi que la regla nunca se
    #     activaba en el primer tramo. No era "siempre aplicable" como sospeche al
    #     principio: era "nunca aplicable", por una razon distinta.
    # (3) `high`/`low` son extremos ACUMULADOS de la sesion, no el maximo de una
    #     barra. La regla habla de "un salto >2x el max previo", es decir, de un
    #     maximo de una barra comparado con el maximo previo. Comparar el acumulado
    #     de la sesion contra el acumulado anterior compara dos ventanas
    #     distintas y no dice nada sobre el salto de UNA barra.
    #
    # Ahora: si no hay referencia previa utilizable, no se inventa — se declara no
    # aplicable con el motivo. Un extremo sin comparacion previa no se puede
    # clasificar como agotamiento.
    if prev_high is None or prev_low is None:
        return {"applicable": False,
                "reason": "sin referencia previa (prev_high/prev_low no disponibles)"}
    if not (abs(prev_high) > 0 or abs(prev_low) > 0):
        return {"applicable": False,
                "reason": "referencia previa en cero: aun no hay historial comparable"}

    mult = RULE2.get("spike_multiple", 2.0)
    if abs(high or 0) > mult * abs(prev_high) > 0:
        out = {"applicable": True, "side": "up",
               "note": (f"CVD en {abs(high or 0):.0f} supera {mult}x el maximo "
                        f"previo {abs(prev_high):.0f}: AGOTAMIENTO, no continuacion")}
    elif abs(low or 0) > mult * abs(prev_low) > 0:
        out = {"applicable": True, "side": "down",
               "note": (f"CVD en {abs(low or 0):.0f} supera {mult}x el minimo "
                        f"previo {abs(prev_low):.0f}: AGOTAMIENTO, no continuacion")}
    return out


# ── REGLA 1: INTEGRIDAD DE LA SERIE QUE USA LA REGLA ─────────────────────
# 2026-10-07. Las reglas 6 y 7 miran una RACHA de 2 barras de bar_deltas, y la
# regla 2 mira las ultimas 4. Nada de eso exige que los 200 ultimos minutos de CVD
# esten completos: exige que las barras CONTIGUAS que se comparan esten ahi.
#
# Seacia lo contrario. Una comprobacion `missing_bars == 0` sobre la serie entera
# seria correcta en teoria y toxica en la practica: hoy missing_bars=148 de 204
# barras por un corte de hace horas, y el veredicto no podria emitir sesgo
# durante semanas por huecos que no afectan a la racha que se esta evaluando. Un
# gate que bloquea todo el tiempo es un gate que nadie lee.
#
# Lo que se comprueba es lo que la regla usa:
#   1. que el endpoint trae suficientes barras (len >= lo que pide la regla)
#   2. que la ultima barra no es un hueco disfrazado de barra
#   3. que hay barras limpias recientes desde el ultimo hueco, suficientes para
#      cubrir la ventana que la regla examina
#
# Si algo falla, la condicion (a) de las reglas 6 y 7 pasa a UNEVALUABLE, que es
# lo que ya hace cuando no hay barras: neutral, no direccion.
CVD_INTEGRITY_MIN_BARS = 2        # lo que necesitan las reglas 6 y 7
CVD_INTEGRITY_WINDOW = 4          # ventana que se quiere limpia (regla 2 usa 4)


def cvd_integrity(cvd_state: dict, need_bars: int = CVD_INTEGRITY_MIN_BARS,
                  window: int = CVD_INTEGRITY_WINDOW) -> dict:
    """Comprueba la integridad de la FRAGMENTO de CVD que la regla va a mirar.

    No juzga la serie historica completa: los huecos de hace horas no invalidan
    una racha de las ultimas 3 barras. Si los juzgara, un hueco antiguo
    bloquearia el sistema de forma permanente.
    """
    out = {"ok": True, "reason": ""}
    bd = cvd_state.get("bar_deltas") or []
    if len(bd) < need_bars:
        out.update(ok=False,
                   reason=f"solo {len(bd)} barra(s) cerrada(s); la regla necesita {need_bars}")
        return out
    if cvd_state.get("continuity") not in (None, "clean"):
        out.update(ok=False, reason=f"continuity={cvd_state.get('continuity')}")
        return out
    # Cuantas barras limpias hay DESDE el ultimo hueco. El puente lo publica como
    # bars_since_gap.
    #
    # 2026-10-14 — BUG ARREGLADO: `None` significaba DOS cosas a la vez.
    # bridge_server.py inicializa bars_since_gap=None y lo documenta como "nunca
    # hubo hueco" (L772), y su PROPIO payload lo traduce a continuity="clean"
    # (L1074-1078). Este lector interpretaba ese mismo None como "el campo no
    # viene" y devolvia ok=False. Consecuencia medida: la serie MAS sana posible
    # (gaps=0, missing_bars=0, continuity clean) se declaraba no verificable,
    # (a) quedaba UNEVALUABLE y las reglas 6/7 — las unicas direccionales, la 4
    # esta deshabilitada — no podian dispararse tras un reinicio del puente.
    # Peor aun: el incremento del puente (L996) esta guardado con `is not None`,
    # asi que None se quedaba None hasta que ocurriera un hueco por accidente.
    # Un centinela con dos significados, y el lector elegia el malo.
    #
    # Distincion ahora explicita: el campo realmente ausente va SIN continuity
    # (quien no calcula bars_since_gap tampoco calcula esa continuidad). Si
    # continuity viene, None es "nunca hueco" y TODAS las barras que hay son
    # contiguas por definicion.
    since = cvd_state.get("bars_since_gap")
    if since is None:
        if cvd_state.get("continuity") is None:
            out.update(ok=False,
                       reason="el puente no publica bars_since_gap ni continuity; "
                              "no se puede confirmar que las barras usadas sean "
                              "contiguas")
            return out
        since = len(bd)          # nunca hueco: todas las barras son contiguas
    # La ventana son las ultimas `window` barras. Si el hueco esta mas lejos, las
    # barras que mira la regla son limpias; si esta dentro, no.
    if since < min(window, len(bd)):
        out.update(ok=False,
                   reason=(f"hueco hace {since} barra(s); la regla mira las ultimas "
                           f"{min(window, len(bd))} y hay una discontinuity"))
        return out
    out["bars_since_gap"] = since
    out["window"] = min(window, len(bd))
    return out


def streak_from_bar_deltas(bar_deltas):
    """(streak_up, streak_down) sobre las barras CERRADAS, de la más nueva atrás.

    Termina en el primer cambio de signo: dos barras hijas alternas NO son
    'streak', y esa es exactamente la condición (a) de las reglas 6 y 7.
    """
    up = dn = 0
    for d in reversed(bar_deltas or []):
        if d > 0:
            if dn:
                break
            up += 1
        elif d < 0:
            if up:
                break
            dn += 1
        else:
            break
    return up, dn


def classify_footprint(top_levels, sell_pct=None):
    """Clasifica niveles del footprint como venta/compra dominante.

    Regla 6 cuenta niveles con `footprint_sell_pct_min`% o mas de volumen en el
    lado bid; regla 7 cuenta los que lo tienen en el lado ask.

    2026-10-07 — BUG ARREGLADO. Los umbrales estaban FIJOS en la funcion
    (`bp > 70.0` / `bp < 30.0`) mientras rules.json declara otros. Con v3.4.1
    los umbrales son 65, asi que:

      - el codigo rechazaba niveles entre 65% y 70% que la regla 6 cuenta
        (con 4 niveles pedidos, el codigo podia devolver 3 y la regla no se
        cumplia)
      - el complemento estaba mal por construccion: con 65 el ask-dominante es
        bid<35, no bid<30. Entre 30% y 35% hay una franja que la regla 7 cuenta
        y el codigo ignoraba — justo la de ask-dominante, que es la que importa
        para la regla 7.

    Ahora se leen de rules.json. Los defaults (70/30) solo aplican si el JSON no
    se puede leer, y avisa de eso al cargar el modulo.

    2026-10-15 — TRAMPA ELIMINADA. La firma traia un `buy_pct` que se guardaba
    en `_bp` y NUNCA se usaba: el lado compra se evaluaba con `100.0 - _sp`.
    El comportamiento era el correcto (ask-dominante = complemento exacto del
    umbral de venta: con 65, bid<=35 es ask>=65), pero el parametro invitaba a
    creer lo contrario. Llamadas reales pasaban `buy_pct=FOOTPRINT_PCT` (=65),
    y quien "arreglara" el parametro muerto para que se usara pasaria a aceptar
    `bid <= 65%` como compra dominante — casi todo el libro — y romperia la
    regla 7 de golpe. El complemento es una identidad, no una configuracion.
    """
    # Defaults solo si rules.json no se pudo leer. load_footprint_pct() avisa
    # de esa situacion al importar el modulo.
    _sp = 70.0 if sell_pct is None else float(sell_pct)
    _low = 100.0 - _sp            # lado ask dominante = complemento exacto
    rows, sell_h, buy_h, thin = [], 0, 0, 0
    for lv in top_levels or []:
        if not isinstance(lv, dict):
            continue
        bid = lv.get("bid", 0) or 0
        ask = lv.get("ask", 0) or 0
        tot = bid + ask
        if not tot:
            continue
        bp = 100.0 * bid / tot
        is_thin = (bid == 0 or ask == 0)
        if is_thin:
            thin += 1
        if bp >= _sp:
            sell_h += 1
            tag = f"SELL>{_sp:g}"
        elif bp <= _low:
            buy_h += 1
            tag = f"BUY>{_sp:g}"
        else:
            tag = "mixed"
        rows.append({"price": lv.get("price"), "bid_pct": round(bp, 1),
                     "bid": round(bid, 1), "ask": round(ask, 1),
                     "delta": lv.get("delta", 0), "tag": tag, "thin": is_thin})
    rows.sort(key=lambda r: (r["price"] is None, r["price"]))
    return rows, sell_h, buy_h, thin


def scorecard(rows, imbalance, ema, tape_buy_pct, volume_ratio, sell_h, buy_h):
    """RETIRADO (2026-10-13). No llamar. Se conserva solo para poder reproducir
    la medicion que la retiro; ninguna ruta de produccion la usa.

    MEDICION que la retiro (sobre 1.205 filas de reader.db con forward_return):

      pearson(score, forward_return)  = +0.0175
      spearman(score, forward_return) = -0.0156
      tercil alto (el "mejor" score): winrate 32.8%, retorno medio -0.392%
      tercil bajo                    : winrate 37.9%, retorno medio -0.382%

    El score no correlaciona con el retorno y su tercil alto perdia MAS que el
    bajo. Ademas el componente de peso 50 (dom_ema = imbalance_pct_ema, el libro
    Binance retirado del gate) media un spread de +27.3pp sobre top150/bottom150
    que con autocorr rho=+0.995 equivale a ~1 observacion independiente
    (z=0.35, no significativo): la calibracion no tenia respaldo.

    El sesgo nunca dependio de esto: sale de `if r7_ok / elif r6_ok / else`.

    NINGUNA RUTA DE PRODUCCION LLAMA A ESTA FUNCION. Se conserva unicamente
    para reproducir la medicion que retiro el scorecard.

    El v2 percentile-median (2026-10-11) tambien esta aqui, pero comparte el
    mismo problema que se corrigio arriba: se calibro sobre los MISMOS 1.205
    registros autocorrelacionados. Que "se centrara solo" y "?spread +36pp?"
    no lo convierte en valido.
    """
    # Sin _CAL (retirado del import en el mismo commit que esta nota) la
    # reproduccion del v2 requiere cargarlo a mano:
    #   import json; verdict._CAL = json.load(open(CAL_PATH))
    cal = _CAL_OVERRIDE
    if cal:
        return _scorecard_percentile(cal, ema, sell_h, buy_h)
    return _scorecard_v341(rows, imbalance, ema, tape_buy_pct, volume_ratio,
                           sell_h, buy_h)


def _percentile_rank(values, x):
    """Rank de x dentro de una lista de percentiles calibrada. Devuelve 0-100."""
    if x is None or not values:
        return 50.0
    import bisect as _bs
    return _bs.bisect_left(values, float(x)) / len(values) * 100.0


def _scorecard_percentile(cal, ema, sell_h, buy_h):
    dists = cal.get("distributions", {})
    weights = cal.get("weights", {})
    raw = {
        "dom_ema": ema,
        "footprint_sell_heavy": sell_h,
        "footprint_buy_heavy_inv": buy_h,
    }
    parts, notes = {}, {}
    total = 0.0
    wsum = 0.0
    for comp, w in weights.items():
        v = raw.get(comp)
        pr = _percentile_rank(dists.get(comp), v)
        if comp.endswith("_inv"):
            pr = 100.0 - pr
        parts[comp] = round(pr * w / 100.0, 1)
        notes[comp] = f"{v} -> percentil {pr:.0f}" + (" (invertido)" if comp.endswith("_inv") else "")
        total += pr * w
        wsum += w
    score = round(total / wsum, 1) if wsum else 50.0
    ev = cal.get("evidence", {})
    notes["_useful_spread"] = {k: ev[k]["useful_spread"] for k in parts if k in ev}
    notes["_method"] = "percentile-median (v2) — RETIRADO, ver nota de cabecera"
    notes["_calib_sample"] = cal.get("sample_size")
    notes["_WARNING"] = ("calibrado sobre la misma muestra autocorrelada que la "
                         "medicion que retiro el scorecard (rho=+0.995)")
    return score, parts, notes


def _scorecard_v341(rows, imbalance, ema, tape_buy_pct, volume_ratio, sell_h, buy_h):
    """v3.4.1 original. MEDIDO COMO ANTI-PREDICTIVO (spread -4,0 medido)."""
    parts, notes = {}, {}

    parts["footprint_depth"] = min(WEIGHTS["footprint_depth"], 3 * len(rows))
    notes["footprint_depth"] = f"{len(rows)} niveles"

    if isinstance(imbalance, (int, float)):
        v = max(0, min(25, int(round(12.5 + (imbalance / 100.0) * 12.5))))
    else:
        v = 0
    parts["dom_imbalance"] = v
    notes["dom_imbalance"] = (f"instantáneo {imbalance:+.2f}%" if isinstance(imbalance, (int, float))
                               else "instantáneo AUSENTE")

    if tape_buy_pct is None:
        t = 0
        notes["tape_ratio"] = "tape AUSENTE"
    else:
        t = max(0, min(20, int(round(tape_buy_pct / 100.0 * 20))))
        notes["tape_ratio"] = f"compra {tape_buy_pct:.1f}%"
    parts["tape_ratio"] = t

    if isinstance(volume_ratio, (int, float)) and volume_ratio > 0:
        vc = max(0, min(15, int(round(min(volume_ratio, 3.0) / 3.0 * 15))))
        notes["volume_climax"] = f"ratio {volume_ratio:.2f}x"
    else:
        vc = 0
        notes["volume_climax"] = "sin baseline de volumen"
    parts["volume_climax"] = vc

    parts["footprint_confirmation"] = min(15, (5 if buy_h >= 3 else 0) + (5 if sell_h >= 3 else 0))
    notes["footprint_confirmation"] = f"clusters {buy_h}/{sell_h}"

    return sum(parts.values()), parts, notes


def build_verdict() -> dict:
    """Un veredicto. Nunca lanza: una fuente caída produce un veredicto que la nombra."""
    now = time.time()
    v = {"ts": now, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))}

    ind = _get("/indicators")
    if ind.get("_error") or ind.get("ticker") is None:
        v["bias"] = "UNAVAILABLE"
        v["reason"] = f"ATAS no disponible: {ind.get('_error', 'payload nulo')}"
        v["probability"] = "unvalidated"
        # La cobertura se declara tambien en los retornos tempranos: si falta,
        # un fallo de ATAS oculta que reglas existen y cuales estan activas.
        v["rule_coverage"] = _rule_coverage()
        v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                      if not r["implemented"]]
        return v

    ts = ind.get("timestamp")
    age = (now - ts / 1000.0) if ts else None
    v["symbol"] = str(ind.get("ticker")).upper()
    v["src_age_s"] = round(age, 1) if age is not None else None
    v["bar_index"] = ind.get("bar_index")
    v["bar_time"] = ind.get("time")

    # Regla live-only: un dato viejo NO es dato. Se descarta y se dice.
    if age is None or age > LIVE_MAX_AGE_S:
        v["bias"] = "UNAVAILABLE"
        v["reason"] = (f"dato NO vivo (edad {v['src_age_s']}s > {LIVE_MAX_AGE_S:.0f}s); "
                       f"no se emite veredicto sobre datos congelados")
        v["probability"] = "unvalidated"
        v["rule_coverage"] = _rule_coverage()
        v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                      if not r["implemented"]]
        return v

    price = ind.get("price") or {}
    dom = ind.get("dom") or {}
    fp = ind.get("footprint") or {}
    vol = ind.get("volume") or {}
    tape = ind.get("tape") or {}
    v["price_last"] = price.get("last")
    v["poc"] = fp.get("poc")
    v["value_area"] = [fp.get("value_area_low"), fp.get("value_area_high")]

    rows, sell_h, buy_h, thin = classify_footprint(
        fp.get("top_levels"), FOOTPRINT_PCT)
    imbalance = dom.get("imbalance_pct")
    ema = dom.get("imbalance_pct_ema")
    v["footprint"] = {"levels": len(rows), "sell_heavy": sell_h,
                     "buy_heavy": buy_h, "thin": thin, "table": rows}

    bq, sq = tape.get("buy_vol"), tape.get("sell_vol")
    if bq is None or sq is None:
        tr = tape.get("trades") or []
        bq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "buy")
        sq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "sell")
    tot = (bq or 0) + (sq or 0)
    tape_buy_pct = round(100.0 * bq / tot, 1) if tot else None
    v["tape_buy_pct"] = tape_buy_pct
    v["volume_ratio"] = vol.get("volume_ratio")
    v["dom"] = {"imbalance_pct": imbalance, "imbalance_pct_ema": ema,
                "ema_n": dom.get("imbalance_pct_ema_n"),
                "bid_ask_ratio": dom.get("bid_ask_ratio")}

    # ── Condición (a): rachas de CVD ──
    cv = _get("/cvd")
    cvd_state = (cv.get("cvd") or cv).get(v["symbol"]) or {}
    bd = cvd_state.get("bar_deltas") or []
    up, dn = streak_from_bar_deltas(bd)
    v["cvd"] = {"value": cvd_state.get("cvd"), "bars": cvd_state.get("bars"),
                "streak_up": up, "streak_down": dn,
                "continuity": cvd_state.get("continuity"),
                "gaps": cvd_state.get("gaps"), "missing_bars": cvd_state.get("missing_bars"),
                # Explicito, porque missing_bars por si solo no dice si AFECTA a
                # la ventana que mira la regla. Un total de 148 huecos con 90
                # barras limpias despues del ultimo hueco es una serie sana para
                # una racha de 3; el mismo total con un hueco hace una barra es
                # una serie rota. Sin este campo no se puede distinguir.
                "bars_since_gap": cvd_state.get("bars_since_gap")}

    # ── Regla 2 ──
    # 2026-10-07: se evaluaba contra `high_cvd`/`low_cvd`, que son los extremos
    # ACUMULADOS de la sesion. La regla habla de "un salto >2x el maximo previo
    # en una barra", asi que lo que corresponde son los deltas POR BARRA
    # (`bar_deltas`), no la serie acumulada. Con los acumulados se comparaba la
    # ventana completa contra la ventana anterior: no dice nada sobre el salto de
    # una barra, y puede marcarse por un extremo de hace 100 barras.
    #
    # min_prior_bars = 3 barras de historico antes de poder afirmar un extremo.
    _need = RULE2["min_prior_bars"]
    _int_r2 = cvd_integrity(cvd_state, _need, _need + 1)
    if not _int_r2["ok"]:
        v["rule2"] = {"applicable": False,
                      "reason": "serie CVD no integra: " + _int_r2["reason"]}
    elif len(bd) >= _need:
        _prior = bd[:-1]
        _cur = bd[-1]
        _prior_hi = max(_prior)
        _prior_lo = min(_prior)
        v["rule2"] = rule2_exhaustion(
            _cur,
            max(_prior_hi, _cur) if _cur > 0 else 0.0,
            min(_prior_lo, _cur) if _cur < 0 else 0.0,
            _prior_hi, _prior_lo)
    else:
        v["rule2"] = {"applicable": False,
                      "reason": (f"solo {len(bd)} barra(s) cerrada(s); la regla 2 "
                                 f"exige {RULE2['min_prior_bars']} de historico")}
    # ── Regla 8: CVD roto por un hueco no es una serie limpia ──
    v["cvd_clean"] = (cvd_state.get("continuity") == "clean"
                      and not (cvd_state.get("missing_bars") or 0))

    # REGLA 1 aplicada a la condicion (a): no basta con que haya 2 barras, tienen
    # que ser CONTIGUAS. Antes solo se comprobaba len(bd) >= 2, que cuenta barras
    # pero no dice si estan seguidas: un hueco en medio produce una racha de dos
    # barras que nunca fueron consecutivas.
    _int_a = cvd_integrity(cvd_state, RULE6["streak"], CVD_INTEGRITY_WINDOW)
    _int_b = cvd_integrity(cvd_state, RULE7["streak"], CVD_INTEGRITY_WINDOW)
    v["cvd"]["integrity"] = {"rule6_window": _int_a, "rule7_window": _int_b}
    v["cvd"]["integrity_ok"] = _int_a["ok"] and _int_b["ok"]

    if not _int_a["ok"]:
        a6 = "UNEVALUABLE"
    else:
        a6 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if dn >= RULE6["streak"] else "FAIL")
    if not _int_b["ok"]:
        a7 = "UNEVALUABLE"
    else:
        a7 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if up >= RULE7["streak"] else "FAIL")
    b6 = "MET" if sell_h >= RULE6["levels_min"] else "FAIL"
    b7 = "MET" if buy_h >= RULE7["levels_min"] else "FAIL"
    # 2026-10-07 — BUG ARREGLADO: la condicion (c) se evaluaba SOLO con el valor
    # del EMA. rules.json v3.3 exige ademas `dom_persistence_polls_min: 3` en las
    # reglas 6 Y 7: el imbalance debe hold 3+ lecturas antes de contar. El puente
    # ya publicaba ese contador (`imbalance_pct_ema_n`) y el veredicto lo leia para
    # IMPRIMIRLO, pero no lo usaba para decidir MET/FAIL — es decir, se evaluaba
    # un gate con menos datos de los que la regla pide.
    #
    # Consecuencia real medida: con n=1 la EMA todavia no se ha asentado y su
    # valor es practicamente el instantaneo. Una regla "hard" evaluada sobre una
    # muestra unica no es una regla hard.
    #
    # Ahora, si no hay 3+ muestras, la condicion es UNEVALUABLE — igual que ya
    # se hacia cuando el EMA era None. Y como las reglas son AND estricto,
    # UNEVALUABLE hace que la regla entera NO se cumpla: neutral, no direccion.
    # ── Condición (c): consenso de libro multi-venue (Fix 2-A) ──
    # ANTES: imbalance_pct_ema del libro Binance WS — std 7.5, el gate cambiaba
    # de estado 19-23 veces/día. AHORA: N venues del scanner fuera de banda,
    # sostenido P lecturas. La EMA se SIGUE PUBLICANDO como contexto (v["dom"])
    # pero ya no decide nada — un número que se muestra y un número que gatean
    # dejan de ser el mismo por accidente.
    gate = dom_vac_eval(v["symbol"], observe=False)   # la observación es del
    c6 = gate[RULE6_SIDE]["state"]                    # grabador, no de aquí
    c7 = gate[RULE7_SIDE]["state"]

    # Ventana de persistencia: (a), (b) y (c) deben coincidir DENTRO de la
    # ventana, no en el mismo tick de 30 s. Ver condition_window_eval().
    # observe=False: el grabador es quien anota; aquí solo se lee.
    _eff = condition_window_eval(
        v["symbol"],
        {k: s for k, s in zip(_COND_KEYS, (a6, b6, c6, a7, b7, c7))},
        COND_WINDOW_S, observe=False)
    a6, b6, c6 = _eff["r6_a"], _eff["r6_b"], _eff["r6_c"]
    a7, b7, c7 = _eff["r7_a"], _eff["r7_b"], _eff["r7_c"]

    v["persistence"] = {
        "window_s": COND_WINDOW_S,
        "note": ("las condiciones valen MET si lo estuvieron en los ultimos "
                 f"{COND_WINDOW_S:g}s; la ventana NO anade evidencia, solo deja "
                 "de descartar la que ya habia"),
    }

    # Estructura del chart (TradingView). CONTEXTO, NO señal: no entra en el
    # sesgo. Existe para que el silencio sea auditable. Import perezoso y
    # protegido: la puerta tiene que seguir funcionando aunque live_reader o el
    # chart no esten.
    try:
        import live_reader as _lr
        v["structure"] = _lr.structure_block()
    except Exception as _e:                             # noqa: BLE001
        v["structure"] = {"ok": False,
                          "error": f"{type(_e).__name__}: {str(_e)[:60]}",
                          "validated": False}

    v["dom_vac"] = {
        "symbol": v["symbol"],
        # Cada lado con LOS SUYOS: antes ambos reportaban los de la regla 7
        # mientras las claves de conditions (mas abajo) citaban los de la 6.
        # Coincidian solo porque rules.json hoy pone lo mismo en las dos.
        "thr": {"sell": RULE6["vac_thr"], "buy": RULE7["vac_thr"]},
        "z": {"sell": RULE6["vac_z"], "buy": RULE7["vac_z"]},
        "venues_min": {"sell": RULE6["vac_venues"], "buy": RULE7["vac_venues"]},
        "polls_min": {"sell": RULE6["vac_polls"], "buy": RULE7["vac_polls"]},
        "buy": gate["buy"], "sell": gate["sell"],
        "detail": gate.get("detail", ""),
    }

    r6 = [a6, b6, c6]
    r7 = [a7, b7, c7]
    r6_met = all(x == "MET" for x in r6)
    r7_met = all(x == "MET" for x in r7)
    unev = [x for x in (r6 + r7) if x == "UNEVALUABLE"]

    v["rule6"] = {
        "conditions": {"a_cvd_down_2plus": a6,
                       f"b_footprint_{RULE6['levels_min']}_sell_heavy": b6,
                       f"c_vac_sell_{RULE6['vac_venues']}deM_z{RULE6['vac_z']:g}": c6},
        "met": r6_met}
    v["rule7"] = {
        "conditions": {"a_cvd_up_2plus": a7,
                       f"b_footprint_{RULE7['levels_min']}_buy_heavy": b7,
                       f"c_vac_buy_{RULE7['vac_venues']}deM_z{RULE7['vac_z']:g}": c7},
        "met": r7_met}

    # ── Regla 5: No Pattern Anchoring ──
    # Validation: verify the verdict does not reference prior bar patterns as evidence.
    # The engine uses only current bar footprint, DOM, CVD, and tape — no lagged patterns.
    # HONESTO (2026-10-11 audit): None, no True. Un flag que no puede fallar no
    # es un control — era True por construccion mientras la cobertura de reglas
    # declara la 5 como "no comprobable por codigo". Lo que SI es cierto: las
    # entradas del veredicto son solo de la barra/lectura actual (arquitectura),
    # y eso se dice como tal en vez de simularse como chequeo pasado.
    v["rule5"] = {
        "anchoring_ok": None,       # no computable; ver _rule_coverage()
        "note": "Regla de conducta: no verificable por codigo. Cubierta por "
                "diseno — las entradas del veredicto son solo la lectura actual "
                "(footprint/DOM/CVD/tape de esta barra); ningun estado de "
                "analisis anterior entra como evidencia."
    }

    # ── El sesgo viene de la PUERTA ──
    # Regla 6 en cuarentena desde 2026-10-11: enforcement "disabled" en
    # rules.json por PRECAUCION, NO por anti-edge medido — su evidencia es
    # n_eff=2,7 sobre 159 filas solapadas (autocorr +0,97), que es ruido, y es
    # toda de la era del gate viejo. Ver el `disabled_reason` de rules.json.
    # Se sigue EVALUANDO y se publica el detalle, pero no decide el sesgo: una
    # senal con apariencia de analisis sin base cuesta mas que no dar ninguna.
    # Volver a "hard" exige n_eff >= min_n sobre el gate multi-venue.
    r6_enforcement = _rule_enforcement(6)
    if r7_met:
        bias = "BULLISH"
    elif r6_met and r6_enforcement != "disabled":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    v["rule6"]["enforcement"] = r6_enforcement
    v["rule6"]["counts_toward_bias"] = (r6_enforcement != "disabled")
    if r6_met and r6_enforcement == "disabled":
        v["rule6"]["note"] = (
            "condiciones satisfechas pero la regla esta DESACTIVADA "
            "(anti-edge medido 2026-10-11); no aporta al sesgo")

    # ── SCORECARD RETIRADO (2026-10-13) ──
    # Antes se calculaba aqui y se publicaba v["scorecard"] + v["score_gate_conflict"].
    # Retirado por medicion sobre 1.205 filas de reader.db con forward_return:
    #
    #   pearson(score, forward_return)  = +0.0175     (ruido)
    #   spearman(score, forward_return) = -0.0156
    #   tercil alto ("mejor" score): winrate 32.8%, retorno medio -0.392%
    #   tercil bajo                : winrate 37.9%, retorno medio -0.382%
    #
    # No correlaciona con el retorno y el tercil alto perdia MAS que el bajo. El
    # componente de peso 50 (dom_ema = imbalance_pct_ema, el libro Binance que se
    # retiro del gate) media spread +27.3pp sobre top150/bottom150, que con
    # autocorr rho=+0.995 son ~1 observacion independiente (z=0.35): la
    # calibracion no tenia respaldo estadistico.
    #
    # Se eliminan tambien `score_gate_conflict` y su detalle: comparaban el
    # veredicto de la puerta contra un numero sin poder predictivo, asi que el
    # "conflicto" que reportaban era ruido, no una senal de desacuerdo real.
    #
    # El sesgo no cambia: sigue saliendo de if r7_ok / elif r6_ok / else.
    v["bias"] = bias
    v["unevaluable"] = unev
    v["probability"] = "unvalidated"
    # ── COBERTURA DE REGLAS, generada desde rules.json ────────────────────────
    # 2026-10-07. Antes havia 11 lineas de la forma
    #     v["ruleN_annotate"] = "..." + v.get("ruleN", {}).get("note", "no configurado")
    # para las reglas 5 y 9-18. Esas reglas NO EXISTEN en rules.json v3.4.1 (8
    # reglas), y v["ruleN"] nunca se creaba, asi que las once imprimian
    # literalmente "no configurado". Un veredicto con diez lineas de "no
    # configurado" parece un motor con diez reglas mas de las que tiene, y es
    # justo lo que hace que alguien asuma que estan activas.
    #
    # Ahora se lee rules.json y se declara, por regla, si este modulo la
    # implementa o no. Un sitio unico, imposible de desincronizar del JSON.
    v["rule_coverage"] = _rule_coverage()
    v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                  if not r["implemented"]]

    v["reason"] = _reason(v)
    return v


def _reason(v) -> str:
    b = v["bias"]
    if b == "UNAVAILABLE":
        return v.get("reason", "fuente no disponible")
    if b == "BULLISH":
        return (f"Regla 7 satisfecha: CVD sube {RULE7['streak']}+ barras, "
                f"{RULE7['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% compra, "
                f"consenso de libro multi-venue comprador ({RULE7['vac_venues']} venues, z>={RULE7['vac_z']:g}, {RULE7['vac_polls']} lecturas)")
    if b == "BEARISH":
        return (f"Regla 6 satisfecha: CVD cae {RULE6['streak']}+ barras, "
                f"{RULE6['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% venta, "
                f"consenso de libro multi-venue vendedor ({RULE6['vac_venues']} venues, z>={RULE6['vac_z']:g}, {RULE6['vac_polls']} lecturas)")
    r6, r7 = v["rule6"], v["rule7"]
    miss6 = [k for k, s in r6["conditions"].items() if s != "MET"]
    miss7 = [k for k, s in r7["conditions"].items() if s != "MET"]
    if v["unevaluable"]:
        return ("NEUTRAL forzado: hay condiciones UNEVALUABLES ("
                + ", ".join(v["unevaluable"])
                + "). Una regla que no se puede evaluar no es una regla que no se cumple.")
    # El ramal "aunque el scorecard da N/100" se retiro con el scorecard
    # (2026-10-13): citaba un numero medido sin poder predictivo, asi que
    # adornaba la explicacion con precision falsa.
    _r6note = ("; la regla 6 esta DESACTIVADA (evidencia insuficiente, "
               "n_eff=2.7)" if r6.get("enforcement") == "disabled" else "")
    return (f"NEUTRAL: las reglas no disparan (regla 6 falla en {len(miss6)}, "
            f"regla 7 falla en {len(miss7)}){_r6note}.")


# ── Regla 8: Audit On Loss ──────────────────────────────────────────────
# After a call that fails (price moves >1% against within 2 bars), record loss
# with root cause in audit.log.json at START of next analysis.
# This function is called from the caller (bridge/bot) when a trade signal fails.
import os as _os
# 2026-10-07 — RUTA CORREGIDA. Apuntaba a atas-bridge/audit.log.json, que es el
# fichero de 19.848 bytes del subsistema /calls ya BORRADO (call_id, entry,
# outcome_r, mfe_r, mae_r). Escribir ahi mezclaria el registro de perdidas del
# grabador con los restos de un sistema que no existe. Ahora va a su propio
# fichero dentro de reader/, que es quien lo produce.
#
# Tambien se corrigio `import os as _os` + `os.path.exists(...)`: funcionaba por
# el `import os` de la cabecera del modulo, pero leerlo era pedir un NameError
# si alguien quita ese import.
_audit_log_path = _os.path.join(HERE, "audit_losses.jsonl")

# Umbral de la regla 8: "price moves >1% against within 2 bars". Se lee de
# rules.json si la regla lo declara; si no, 1% es el valor que la regla v3.x dice.
AUDIT_LOSS_PCT = 0.01
try:
    _r8 = _load_rule_conditions(8)
    if _r8.get("_present"):
        AUDIT_LOSS_PCT = float(_r8.get("loss_pct", AUDIT_LOSS_PCT))
except Exception:
    pass

def _audit_record_loss(symbol: str, price_last: float, price_entry: float,
                       loss_pct: float, root_cause: str) -> None:
    """Record a failed call with root cause in audit.log.json for post-analysis."""
    import json as _json
    import time as _time
    log_entry = {
        "ts": round(_time.time(), 1),
        "symbol": symbol,
        "price_last": price_last,
        "price_entry": price_entry,
        "loss_pct": round(loss_pct, 4),
        "root_cause": root_cause,
    }
    try:
        # JSONL: se ANADE una linea, no se reescribe el fichero entero. La version
        # anterior lo reescribia completo en cada perdida: O(n) por escritura y
        # cualquier fallo a mitad dejaba el registro truncado.
        with open(_audit_log_path, "a", encoding="utf-8") as bf:
            bf.write(_json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def render_text(v: dict) -> str:
    if v["bias"] == "UNAVAILABLE":
        return f"[LECTURA NO DISPONIBLE] {v.get('reason','')}"
    d = v.get("dom") or {}
    c = v.get("cvd") or {}
    lines = [
        f"{v['bias']}  probability: {v['probability']}",
        f"{v['symbol']}  px {v.get('price_last')}  barra #{v.get('bar_index')} "
        f"({v.get('bar_time')})  edad {v.get('src_age_s')}s",
        f"EMA {d.get('imbalance_pct_ema')}% (n={d.get('ema_n')}) vs "
        f"instantáneo {d.get('imbalance_pct')}%  |  tape compra "
        f"{v.get('tape_buy_pct')}%"
        + (f"  |  vol ratio {v['volume_ratio']:.2f}x"
           if isinstance(v.get("volume_ratio"), (int, float)) else "  |  vol ratio n/d"),
        f"footprint: {v['footprint']['sell_heavy']} venta>={FOOTPRINT_PCT:g}% / "
        f"{v['footprint']['buy_heavy']} compra>={FOOTPRINT_PCT:g}%  POC {v.get('poc')}  "
        f"VA {v.get('value_area')}",
        f"CVD {c.get('value')}  rachas +{c.get('streak_up')}/-{c.get('streak_down')}"
        + (f"  [contiguas: {c.get('bars_since_gap')} barras desde el ultimo hueco]"
           if c.get('integrity_ok') else
           f"  [NO integra: {(c.get('integrity') or {}).get('rule6_window', {}).get('reason', '?')}]")
        + "  "
        # cvd_clean mira la serie ENTERA (incluye huecos de hace horas) y
        # integrity_ok mira solo la VENTANA que usa la regla. Con 148 huecos
        # historicos y 90 barras limpias despues, los dos pueden ser ciertos a la
        # vez y decir cosas distintas. Se muestran los dos, con su nombre, en vez
        # de un "CON HUECO" que parece un aviso cuando la racha es perfectamente
        # utilizable.
        f"[serie {'limpia' if v.get('cvd_clean') else 'con huecos antiguos'} | "
        f"ventana de la regla {'utilizable' if c.get('integrity_ok') else 'NO utilizable'}]",
    ]
    for rid, r in (("6", v["rule6"]), ("7", v["rule7"])):
        conds = ", ".join(f"{k.split('_')[0]}={s}" for k, s in r["conditions"].items())
        # AT-: namespace de atas-bridge/rules.json (el scanner tiene sus
        # propias reglas 1-4 con los mismos numeros — ver Fix 4 de
        # docs/FIXES-gate-DOM.md).
        lines.append(f"Regla AT-{rid}: {'CUMPLE' if r['met'] else 'no cumple'} ({conds})")
    if v.get("rule2", {}).get("applicable"):
        lines.append(f"Regla 2: {v['rule2']['note']}")
    lines.append(v["reason"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--out", help="write JSON to this path")
    a = ap.parse_args()
    v = build_verdict()
    if a.out:
        Path(a.out).write_text(json.dumps(v, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    if a.text:
        print(render_text(v))
    elif a.json or not a.out:
        print(json.dumps(v, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())```

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

**Corte por lado (v2, etiquetas T3 corregidas 2026-11-12):**
LONG n=573 — hit 46.6%, favorable med +3.12% (p75 +6.47%), adversa med −3.27%.
SHORT n=1,269 — hit 47.2%, favorable med +3.21% (p75 +6.52%), adversa med
−2.96%. **Simétricos: sin edge en NINGUNO de los dos lados.** (La v1 del
paquete tenía MFE/MAE intercambiados en SHORT — bug del generador, corregido
en T3; el hit-rate y la mediana r24 no cambian, solo las excursiones.)

**Corte LONG por profundidad de funding (el hallazgo):**
```
funding 1.5-3x  (n=418): hit 45.5%  med_r24 -0.41%  MFE_p75 +5.83%
funding 3-10x   (n=123): hit 48.0%  med_r24 -0.15%  MFE_p75 +9.73%
funding >10x    (n=32):  hit 56.2%  med_r24 +1.00%  MFE_p75 +11.22%
```
Gradiente monótono; único cell verde: LONG con funding ≤ −0.1% (10× baseline).

**Oportunidad (T3 corregido):** 33.8% de las banderas tocó favorable ≥ 5%
en 24h; 12.6% ≥ 10% — la mitad de esos recorridos eran SHORT (que el hit-rate
neto no captura). Sigue sin ser edge: recorrido favorable ≠ retorno neto.
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

# APÉNDICE F — CSV completo de banderas del backtest (5,459 filas x 10 columnas)

**sha256 del CSV decodificado:** `002f854c4351ecc3b140e6d18aa113a6dfd1af44af6e53e447051743a22f7129`
**Columnas:** sym, side, variant, ts (ms UTC), entry, funding, mfe24, mae24, r24, r72

Cómo decodificar: (1) copia el contenido del bloque de texto más abajo
(lo que está entre los dos fences) en un fichero llamado flags.b64;
(2) corre el script de abajo. El sha256 impreso debe ser el declarado arriba.

```python
import gzip, base64, hashlib, io, pandas as pd
b64 = open('flags.b64', encoding='utf-8').read()
csv = gzip.decompress(base64.b64decode(''.join(b64.split()))).decode('utf-8')
print('sha256:', hashlib.sha256(csv.encode()).hexdigest())
df = pd.read_csv(io.StringIO(csv))
print(len(df), df.columns.tolist())
```

Bloque a copiar (de aquí abajo, entre fences):

APENDICE-F-B64-START
```text
H4sIAFg+9moC/6y9za4uyW4lNvez7PoQjP8YWobhHhhuoK0XENBqQwPdBiS5gX57c5FB5nd2pCeXoQvVqapTex2uyMwIkkEu/vv/
/Neff/+X//rPP//jn/7tX/7pb//x8x///vPPf/uPf/ufP//t//3bf/2Xv/0/P//63/45159//Sf89d/w/yP/L//rf/mHn//7P/3n
//KPP/8j/9AoVFKdCf/3kz4981/4bzPVvvDPNH7+Wp+06Oev8VmD+N/lWr5B6ALI3/77P9wwRfjklGd1kFWawtCYlH/qJ8/+81f5
lMIw6VNG/fmLCqO2g9MFoM3rhknKLZeRDWh0/i91iWaf/PufkniJ5mfyP2CJWsE/5dRPamEcY3YNqNTpj37WojiltE4/WIsKgJ4Z
Ln9yw3LNtc714f8+OUqmP2D4dWEzCP+mAmBV/sf6eXmDWqbuKCPRRqmrg18u8h4ChH87LVj2tsZBFFuYO8b0Vf0NXLwg9gbWNPGc
+JXEA1ryIjZitP6pPb+YEwbaa9zXfJ7Uat8w/Lfy1lSSp70KIGn1E2SsZ20WtbFReh2d3xoa/LrQZ5SJxSHYVD9jnLtXHMdWJ26Q
EpvjWZ1Fw5Znzbl4lVPtWNeJrZA/BN4+sD3Wk1cUxmiFzVFWazzf+GrDv86a2dxPnRXrkcbEY19r4Z/WyietMI7xugVUUm++Da4p
5xUWiJek/WDHITx36tgTe+LVosYvAx0rxPtlfr6u/faU1tr8mR/KHXbMVvlVKn0ICu+zJwr/QQ9K75tWbpN3Tvq0iR9lc8oQQnUK
Up7rWOkbSLZEN4xSemXm/Tbyf5/bfhtzHbx38LpOQZhddo/R5bBI/dwT4zhGLW6QEmuNsuMMqrbVE7sJvBF2eYsq/3tstPyv8Iq+
PLQojNEKm6Os+ijdYWqfe3X44/tZn4FDGD/YfzqfJ3yG8LGRz28+imKc/m6Y//M//1//x2a0Sn2cHnZOM3Zz2eYZhz/UVmVjr7JM
Y4mLV+kLhW6gbEa3YGZv/YFJZcPktAo8MBxgOKzkaWf258ZntH6szBx5Pig47r5Q6DPLwkmRM3z7uoDS1/iFUmdbjy3sX26QQrxd
4+dgHFU+P6u6c23/On+v8RUkXaA7Rgk9dnyew4L3jmaLVNucvNSyE35awvbP7h/e0HKYEwPR1WGM5/greDR/QAwSDD7Z4Gcu+RP6
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
```

# APÉNDICE G — Protocolo de auto-verificación del paquete

Corre este script contra el fichero MD (stdlib + pandas). Debe imprimir
True x3 en los hashes y recalcular las tablas de §3/D — el gradiente
esperado: 45.5 → 48.0 → 56.2 (hit net) con MFE_p75 +11.2% en >10x.

```python
# AUDIT_VERIFY — extrae los bloques incrustados, verifica hashes y recalcula
# las tablas de §3/Apéndice D. stdlib + pandas.
import gzip, base64, hashlib, io
import pandas as pd

doc = open("AUDIT-GATE-SIGNAL-ENGINE.md", encoding="utf-8").read()
sha = lambda s: hashlib.sha256(s.encode('utf-8')).hexdigest()

# 1. rules.json (Apéndice A) — bloque entre fences json
rj = doc.split("# APÉNDICE A")[1].split("```json")[1].split("```")[0].strip(chr(10))
print("A rules.json:", sha(rj) == "e046ed7cb8054406d2aa30097533587e076b1e585ceb6d989dff8876ad93274c")

# 2. verdict.py (Apéndice B) — bloque entre fences python
vp = doc.split("# APÉNDICE B")[1].split("```python")[1].split("```")[0].strip(chr(10))
print("B verdict.py:", sha(vp) == "5d73e376ed2ce1f0447f4a22347844a3cf03724433f066ca5e35eb208af6c98c")

# 3. CSV (Apéndice F) — requiere flags.b64 (copia del bloque de texto del apéndice)
b64 = open("flags.b64", encoding="utf-8").read()
csv = gzip.decompress(base64.b64decode("".join(b64.split()))).decode("utf-8")
print("F csv:", sha(csv) == "002f854c4351ecc3b140e6d18aa113a6dfd1af44af6e53e447051743a22f7129")
df = pd.read_csv(io.StringIO(csv))
COST = 0.11
for var in ("v2", "v1", "noB"):
    d = df[df["variant"] == var]
    print(var, "n=", len(d),
          "hit24_net%=", round(((d["r24"] - COST) > 0).mean() * 100, 1),
          "med_r24=", round(d["r24"].median(), 3),
          "med_mfe=", round(d["mfe24"].median(), 3))
v2 = df[df["variant"] == "v2"]
longs = v2[v2["side"] == "LONG"]
for lo, hi, tag in ((1.5, 3, "1.5-3x"), (3, 10, "3-10x"), (10, 1e9, ">10x")):
    dd = longs[(longs["funding"] <= -lo * 1e-4) & (longs["funding"] > -hi * 1e-4)]
    if len(dd):
        print("LONG", tag, "n=", len(dd),
              "hit_net%=", round(((dd["r24"] - COST) > 0).mean() * 100, 1),
              "med_r24=", round(dd["r24"].median(), 3),
              "MFE_p75=", round(dd["mfe24"].quantile(0.75), 3))
```
