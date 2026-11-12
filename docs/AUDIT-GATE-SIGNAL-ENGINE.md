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
hash del CSV decodificado (verificable contra Apéndice F):              4771e68680f5214fdcad32d93ee52ee2b44c475ecb047e722764ca8b7b8865fa
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

# APÉNDICE F — CSV completo de banderas del backtest (5,459 filas x 10 columnas)

**sha256 del CSV decodificado:** `4771e68680f5214fdcad32d93ee52ee2b44c475ecb047e722764ca8b7b8865fa`
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
print("F csv:", sha(csv) == "4771e68680f5214fdcad32d93ee52ee2b44c475ecb047e722764ca8b7b8865fa")
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
