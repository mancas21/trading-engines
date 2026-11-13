# PLAN — trading-engines

**Escrito 2026-10-11 09:10.** Este archivo es la respuesta a "por qué cada chat
decidimos de nuevo qué hacer". El orden de abajo es el orden real. No se salta
al item 4 sin cerrar el 1.

Estado verificado. Actualizado 2026-10-11 10:05 tras cerrar el item 1.

```
supervisor  pid=1364   (venv via pyvenv.cfg)
scanner     pid=25704  (hijo del 1364)     <- UNO y UNO, escribe normal
:8090/health -> 200     froz=0  err=0
pendientes  127 -> 91    resueltos +108    unscorable +0   (5 min, fix activo)
watchdog fires: 0
```

---

## 1. El 41% de filas unscorable — HECHO y verificado

**Causa raíz:** `pending_rows` ordenaba `ORDER BY ts DESC` — las más NUEVAS
primero — pero `resolve_pending` sólo puede puntuar una fila cuando su barra de
horizonte ya CERRÓ, y un horizonte de 5 barras cierra 5 minutos después de
escribirse. Las filas más nuevas son justo las que todavía NO se pueden
resolver. Con `DESC` el resolver gastaba cada ciclo pidiendo sus 12 filas más
nuevas, no encontraba ninguna lista, no resolvía nada, y **nunca llegaba a las
viejas que sí estaban listas.** Esas envejecían y `mark_unscorable` las retiraba
a los 1800s.

Medido antes del fix en la base viva:
- **298 filas candidato** retiradas unscorable = **52% de las 569 señales**
- filas sentadas a **edad 1790s con horizon_bars=5** — listas a los ~300s,
  esperando a los 1790s, a un poll de la retirada

**Fix:** `ORDER BY ts ASC`. El más viejo primero es el más probablemente listo,
que es el orden que necesita un consumidor con retardo temporal.

**Verificado en vivo (5 min con el fix):**

```
pendientes : 127 -> 91     (drena, monótono)
resueltos  : +108
unscorable : +0            (antes: ráfagas de 130-350/hora)
>30min     : 0             (nada varado pasado el límite)
```

Test de regresión: `tests/test_outcomes.py::test_pending_batch_is_oldest_first`.
Comprobado que **falla con `DESC` y pasa con `ASC`** — no es un test decorativo.
Monitor reutilizable: `watch_outcomes.py [minutos]`.

**Pendiente de este item:** las 298 señales perdidas **son recuperables** —
Binance `/klines` acepta `startTime`, así que la barra histórica existe aunque
el pipeline ya la diera por muerta. Backfill = +298 mediciones gratis sobre una
muestra de 569. Vale la pena porque duplica la evidencia disponible para el
item 2.

---

### Corrección: el "Python312 vs venv" NO era un problema

Lo di por incoherencia en la versión anterior de este plan y **estaba
equivocado.** En Windows el `python.exe` de un venv **es** el binario base
(`Python312\python.exe`) con un `pyvenv.cfg` al lado; el process list muestra
el exe base, no el venv. Ejecutado desde el venv reporta:

```
sys.executable : ...\quant-alert-engine\venv\Scripts\python.exe
sys.prefix     : ...\quant-alert-engine\venv
base_prefix    : ...\Python312          -> en venv? True
```

O sea: **"Python312" en la lista de procesos es lo esperado**, no evidencia de
intérprete equivocado. Los supervisores duplicados eran dos lanzamientos
separados, no dos intérpretes peleando. No hay nada que arreglar acá.

Lo que sí se mantiene: `stop-scanner.ps1` antes de cada start, porque el
supervisor se relanza solo (`--max-attempts 999`) y matar sólo al padre deja al
hijo huérfano escribiendo.

---

## 2. Saber si la regla 4 sirve o no — RESPONDIDO (y la respuesta es NO)

**Veredicto final, sobre 662 señales con horizonte coherente:**

```
regla 4   n=662   media -0.000052   t=-1.34   hit 48.0%   ->  SIN EDGE
ningun par alcanza significancia
```

Media -0.0052% por operación, t=-1.34. Es una moneda al aire que pierde un poco
contra los costos. **No hay edge demostrable y no lo va a haber por pulir más.**

### Cómo se llegó acá, porque el camino importa más que el número

Primero había 213 señales medidas y el resultado era inconcluso (t=0.15).

Después el backfill subió a 658 y el resultado se volvió **significativamente
negativo** (media -0.000111, t=-2.37, hit 41.9%), con SUI/USD a t=-4.81. Eso
invitaba a invertir la señal y operar al revés.

**Era artefacto de un bug, no un hallazgo.** Al verificar filas del backfill
contra un recálculo independiente, 3 de 6 no coincidían — y las tres eran pares
de 5m mientras los de 1m coincidían al último dígito.

Causa: `resolve_pending` calculaba `at_ms = ts + horizon_bars * 60_000`. Pero
`horizon_bars` está en las barras PROPIAS del par: un par de 1m lleva
`horizon_bars=5` (300s ✓) y uno de 5m lleva `horizon_bars=1`, que son **300s,
no 60s**. Los pares de 5m medían 60 segundos. Y una ventana de 60s en crypto es
casi puro ruido y microestructura, así que medir ahí producía una negativa
**sistemática** — y un sesgo estable es exactamente lo que parece edge en vez de
parecer error.

Corregido (`bar_ms_for` con el tamaño de barra real, `backfill --all` recomputó
4,216 filas): SUI pasó de t=-4.81 a **t=+0.16**, y la regla de t=-2.37 a
**t=-1.34**.

**Regla que queda escrita:** antes de reportar cualquier resultado que alcanza
significancia, recomputar una muestra al azar contra una fuente independiente y
exigir coincidencia exacta. Y desconfiar especialmente de un negativo
significativo: invertirlo es la forma más fácil de fabricar una estrategia que
nunca existió.

### Qué hacer con esto

- **NO invertir la señal.** El "edge invertido" no existe; era el instrumento.
- **NO seguir optimizando features** para subir un AUC de 0.552 (permutado
  0.510). Más tuning no arregla una señal que no existe.
- La regla 4 queda medida y **sin edge**. Si se quiere seguir, hay que buscar
  OTRA regla, no pulir esta.

### Corrección 2026-10-11 (post-auditoría): ese veredicto ya no es medible por el código actual

El n=662 de arriba quedó fuera de la población auditable por la exclusión de
`price_basis_ok` (5,560 filas legacy con fill venue no recuperable) y, encima,
la auditoría encontró que la selección de barra de salida medía en los pares
de 5m un horizonte real de 0–300s (media ~150s), nunca los 300s declarados.
Ambos defectos están corregidos (`outcomes.select_exit_bar`, semántica ceil;
`exit_close_ms` grabado por fila; `MEASUREMENT_VERSION = 3`; backfill --all
recomputó 6,034 filas: horizonte realizado mínimo 300s en los 8 pares). La
regla 4 vuelve a ser hipótesis: el auditor v3 la reporta `insufficient`
(n=50, n_eff=23.6) hasta que la base v3 llegue a n_eff >= 50.

---

## 3. Nada de esto sobrevive un borrado — el repo está sucio

- HEAD: `8e666c2` (watchdog stall + lock atómico).
- El trabajo de ML completo (`features.py`, `ml_train.py`, `ml_backfill.py`,
  `ml_filter.py`, `ml_regime.py`, `expectancy_report.py`) sigue **sin commitear**.
- `atas-bridge/atas-exporter/obj/` y `bin/` están **trackeados en git** — ruido
  en cada `git status` y modificados en cada build.

**Qué hacer:**
- `git rm -r --cached` de `obj/` y `bin/`, agregar al `.gitignore`.
- Commitear la capa ML como una unidad propia.
- Verificación: `git status` limpio salvo cambios intencionales.

---

## 4. El cron de auditoría no entrega nada

`scanner-engine audit` (cada 30m) tiene `last_status: delivery_failed`,
`last_delivery_error: no delivery target resolved for deliver=all`. Corre bien
el script (10 ejecuciones), pero **el reporte nunca llega a ningún lado**. Si la
regla 4 se rompiera, nadie se enteraría.

**Qué hacer:** darle un destino real (o quitar `deliver: all` y dejarlo en log).
Verificación: forzar un cambio de bucket y ver que el mensaje sale.

---

## 5. Lo que NO es el problema (descartado hoy, para no volver a mirarlo)

- **El cron NO lanza el supervisor.** `scanner_audit_tick.py` corre
  `quant-alert-engine\venv` y `audit_tick.py` no arranca nada. Descartado
  leyendo el código, no adivinando.
- **La asociación `.py` sí es una trampa:** `.py = Python.File` →
  `"C:\WINDOWS\py.exe" "%L" %*`. Cualquier cosa que invoque `supervisor.py` como
  ruta desnuda cae en `py.exe` → Python312. Por eso el supervisor viejo es
  Python312 y el mío es venv. **Nunca invocar un `.py` desnudo en un lanzador.**
- **El watchdog ya no es el problema.** El original mataba scanners sanos cada
  300s/1200s porque medía runtime, y `scanner.py` corre `run_forever()` — un
  bucle infinito nunca termina bien. Ahora mide **progreso** (mtime de
  `scanner.db`), con `--stall 900`. `tests/live_stall.py` fija la semántica (4
  casos) y `tests/live_lock_race.py` prueba el lock con 2/4/8 arranques
  concurrentes → exactamente 1 gana.
- **No hay ejecución de órdenes en ningún lado.** Cero hits de `create_order` /
  `set_leverage` / `fapi/v1/order`. Es sistema de alertas y análisis, no bot.
  Las claves de exchange son solo para datos públicos.

---

## 6. Dónde vive esto en el servidor — NO ESTÁ DESPLEGADO

Verificado por SSH el 2026-10-11 (no asumido desde el checklist):

```
host      vps-japan -> 52.195.120.63   (ip-172-26-14-97, Ubuntu 24.04, AWS, us-east)
repo      /home/ubuntu/workspace/trading-engines
branch    master
commit    75e6c73          <- 3 commits ATRÁS del local (local: fca8fce)
remote    github-trading:mancas21/trading-engines.git
```

**`scanner-engine/` NO existe en el servidor.** El repo allá tiene sólo
`atas-bridge/`, `docs/`, `quant-alert-engine/`, `scripts/`, `tradingview-bridge/`.

Ojo con la discrepancia: `scanner-engine/docs/VPS-CHECKLIST.md` planea
`/opt/trading-engines/scanner-engine` con un unit systemd, pero **la convención
real de la máquina es `/home/ubuntu/workspace/trading-engines`**. Al desplegar,
seguir la realidad, no el checklist.

Corriendo hoy en la VPS (nada de esto es el scanner):

```
freqtrade trade            --userdir /home/ubuntu/freqtrade     (desde Oct 6)
binance_rest_collector.py  --out /home/ubuntu/of-collector/...  (x2, desde Oct 7)
grid_paper.py --days 120                                        (empezado 15:15 hoy)
```

**Para desplegar el scanner hace falta, en orden:**
1. Commitear y pushear lo local (el servidor está 3 commits atrás y el fix del
   horizonte — §2 — cambia los números, así que desplegar la versión vieja no
   sirve).
2. `git pull` en `/home/ubuntu/workspace/trading-engines`.
3. Unit systemd apuntando a `/home/ubuntu/workspace/trading-engines/scanner-engine`.
   **NO** envolverlo además en `supervisor.py`: systemd y el supervisor ambos
   reinician, y un crash persistente se vuelve un doble loop de reinicios. En
   Linux elegir systemd (el checklist ya lo dice).
4. Verificación: `curl -s localhost:8090/health` con `frozen_venues` y
   `failures` vacíos.
Sin dependencias: el scanner es stdlib puro, `python3` alcanza.

---

## Orden de ejecución

1. **(ahora)** Nada. Dejar correr. El scanner acumula en la máquina local.
2. **Commitear la capa ML + destrackear `obj/bin` (§3)** — 15 min, riesgo cero.
3. Arreglar la entrega del cron (§4) — 10 min.
4. Decidir si se despliega el scanner a la VPS (§6) — requiere commit+push primero.
5. **La regla 4 ya está respondida: SIN EDGE (§2).** No pulirla más. Si se quiere
   seguir, hay que medir OTRA regla contra este mismo banco de pruebas.

---

## 7. Post-auditoría 2026-10-11 (tarde): el plan de quitar/añadir, ejecutado

La auditoría completa del repo midió las tres reglas del scanner en la era v3:
todas DROP (bruto ≈ 0, neto −9.6/−11.3/−11.8 bps, t −5.6/−9.5/−12.5). El margen
está en eficiencia de evidencia y costos, no en más reglas. Ejecutado:

**QUITAR:**
- Reglas 1 y 2 RETIRADAS con su medida escrita (`DROP refuted n=... t=...` en
  rules.json, vía `promotion.py --retire` — el camino de bajada que faltaba).
  Siguen disparando como observación; ya no producen candidatos ni consumen
  celdas Bonferroni. La regla 4 queda como única hipótesis direccional viva.
- `bias_score` de signal_engine ELIMINADO (pesos v3.4.1 medidos anti-predictivos,
  spread −4.0). El bridge queda en estado + chequeos de reglas; scoring solo en
  verdict.py.
- Celdas PER LOCAL HOUR fuera del audit (hora local sobre días de muestra =
  ruido que diluía el Bonferroni de las celdas reales).
- quant-alert-engine CONGELADO (ARCHIVED.md); 37 one-off de atas-bridge a
  `_archive/` (quedó `_shared_read.py`, único con import vivo).

**AÑADIR:**
- **Ledger de puntos de decisión (v4)**: una fila por (par, ventana de salida)
  vía `recorder.upsert_verdict` con clave `exit_window_ms` + índice único
  parcial. Medido en vivo: 3 filas/75s frente a ~250 del esquema anterior
  (~80x menos inflación); n_eff debe converger a n. La regla de retracción
  v3→v4 ya disparó una vez y quedó baselinada.
- **Auditor ATAS** (`reader/audit_atas.py`): reglas 6/7 sobre reader.db con la
  misma vara (neto-de-costo 12bps, n_eff, Bonferroni sobre 2 celdas). Primera
  medición: r6 n=159 pero n_eff=2.7 (autocorr +0.97 — el reader graba cada 30s
  y la regla persiste); r7 n=3. INSUFFICIENT honesto. Nota para el futuro: el
  reader necesita su propio ledger de puntos de decisión (dedup por ventana),
  mismo patrón que el scanner.
- **Entrega del cron a Telegram**: `audit_tick.py` entrega al chat de
  `atas-bridge/.env` (los mismos candidatos que verdict_notify; override con
  AUDIT_TG_CHAT_ID) y ejecuta el tick ATAS en la misma tarea. Mensaje de prueba
  entregado y verificado.

**Fase 3 (cuando el ledger v4 acumule ~48h):** regla 4 con ejecución maker vía
`quant_engine/fills.py` (bruto +2bps contra taker 12bps es la única vía operable:
por el costo, no puliendo features); meta-labeling con `ml_train.py` sobre el
ledger eficiente; condicionamiento de régimen (cross_basis_bps ya se graba) como
diagnóstico, nunca gates.


## 8. Gate del DOM re-hecho (Fix 2-A de docs/FIXES-gate-DOM.md, noche 2026-10-11)

La condición (c) de AT-6/AT-7 ya no es la EMA del libro Binance (32 cambios de
estado/día): es consenso multi-venue del scanner — N venues con libro top-5
cargado del lado, sostenido P lecturas (`verdict.dom_vac_eval`, thr 0.25
calibrado con muestra viva, barra de aceptación N=10 transiciones/día
pre-registrada en rules.json). El touch se descartó como base: 0 consensos en
480 ciclos. El grabador del reader, que duplicaba la puerta con umbrales
divergentes, ahora usa la MISMA función. signal_engine ya no evalúa AT-6/AT-7.
Namespace de reglas en ambos JSON + informes citando fichero. 232 tests.
Mañana: medir N contra la barra.

## 9. Telegram: brief de fases en lugar del digest (2026-10-24)

El usuario pidió que a Telegram llegue el análisis de FASE de todos los pares
del scanner (rango/pullback/trend/reversal, tono de trader) y no el digest
templado de 15 min. Tres piezas: (1) `reader/phase_brief.py` — suelo
siempre-activo bajo el supervisor: clasificación mecánica (posición rango 24h
+ mínimos/máximos crecientes 1h + corrida) de los 10 pares, envía cada 1 h;
(2) cron del agente (ZCode, mientras la app está abierta) — versión redactada
por el modelo en la anatomía validada; ambos comparten marcador
`reader/phase_state.json` para no duplicar envíos; (3) el supervisor deja de
llamar `--digest` (el aviso por CAMBIO DE SESGO sigue inmediato). Lección
medida: paths de fapi son `/fapi/v1/...` — `/api/v1/` sobre fapi.binance.com
da 404 silencioso; y las ráfagas de ~20 llamadas seguidas se banean ~1 min
(pacing 0.4 s + retry en `_get`). El brief es estructura, no señal: el sesgo
sigue siendo exclusivamente verdict.py (Reglas 6/7).

## 10. Capa de VIGILANCIA TEMPRANA (2026-11-12) — banderas de pre-posicionamiento

Petición del usuario tras PEPE: el gate formal (Reglas 6/7) es de confirmación
y llega tarde siempre; el marcador que se vio temprano no era el precio sino
el FUNDING en extremo con el precio estabilizando. `reader/watch_early.py`:
bandera AMARILLA "PREPARARSE" cuando se cumplen TRES criterios
pre-registrados, en el mismo lado que el signo del funding:

- A. funding_extreme: |lastFundingRate| >= 0.00015 (1.5x baseline 0.01%)
- B. stabilized: sin nuevo extremo de 56 barras 1h en las últimas 8 barras
- C. first_higher_low (LONG) / first_lower_high (SHORT): swing fractal 3+3

Máximo una bandera por par y lado cada 24h; envío inmediato a Telegram
(reusa verdict_notify.send). JAMÁS es señal ni sesgo — el gate sigue siendo
verdict.py. Disciplina anti-ruido: cada bandera se graba en
`reader/watch_flags.jsonl` y `--measure` rellena r24h/r72h/mfe24h/mae24h;
`--report` imprime hit-rate y medianas. La capa vive o muere por esa medición
(mismo estándar que mató a las reglas 1/2/4 del scanner). Calibración
honesta: NO se aflojó el criterio B para que PEPE disparara el día 1 (su
mínimo del desplome estaba dentro de la ventana de 8h); si la medición dice
que nunca dispara, se ajusta con datos. Corre bajo el supervisor del reader
(ciclo de 30s, auto-throttle 5 min) y el brief horario de fases incluye las
banderas activas en una línea.

### 10.1 — Revisión v2 del criterio B (misma noche, a petición del usuario)

El usuario cuestionó las 8 barras de 1h (8h de latencia mínima). Simulación
sobre el propio PEPE: v1 (8h sobre 1h) no había disparado aún 7h tras el low
con el precio a +5.4%; 6h sobre 1h disparaba a +5.4%; 4h a +2.6%; y B medido
en 15m (2h sin nuevo extremo vs telón de 24h) disparaba a 2.2h y +0.9%.
Cambio: B pasa a medirse en 15m (ventana 8×15m=2h, telón 104×15m); C se
queda en 1h (la estructura necesita marco grande). Riesgo cuchillo añadido
lo cuantifica MAE en la medición — si la mediana de MAE es peor que el
beneficio de la latencia, se vuelve a apretar con datos. No fue
curve-fitting: la v1 demostró ser estructuralmente tarde para el propósito
de la capa (alerta temprana), con simulación documentada antes del cambio.
Primer disparo real de producción: LONG 1000PEPE @ 0.0045155, 2026-11-12.

### 10.2 — Backtest del criterio y retune v3 (2026-11-12)

Event study sobre los históricos de backtest-system (11 pares, klines 1h/15m
+ funding histórico, coste oficial 11bps RT), n=1,842 banderas v2. Veredicto:
la capa completa NO tiene edge medio (hit neto 47%, mediana r24 -0.18%); las
banderas SHORT no lo tienen en ninguna celda; el debate v1-vs-v2 (8h vs 2h)
fue inmaterial para el resultado. El único cell verde: LONG con funding
profundo — gradiente monótono 45.5% (1.5-3x) -> 48.0% (3-10x) -> 56.2% con
mediana r24 +1.0% y MFE p75 +11.2% en >10x (n=32, muestra corta).
Retune v3 aplicado: LONG-only con funding <= -0.001 (10x baseline), SHORT
desactivado. Las banderas serán raras (~5-6/año) — ese es el objetivo.
PEPE (1.7x) no dispara bajo v3: su evento vive en la celda sin edge medido.
Motor del estudio: scripts temporales sobre data/ de backtest-system
(solo lectura + fetch de funding/klines que faltaban vía API), CSV de
banderas en Temp. La capa sigue midiendo en vivo: watch_flags.jsonl +
--measure/--report siguen siendo el árbitro de si v3 vive o muere.


### 10.3 — Test §5.7 de sección cruzada: la capa watch se RETIRA (2026-11-13)

Test pre-registrado (docs/PROTOCOL-SHADOW-MEASUREMENT.md §7, criterios
declarados antes de ver resultados): mecanismo funding-capitulación en
sección cruzada de 60 perps por volumen, n=846 banderas >10x, 355 episodios.
VEREDICTO: NO VALIDADO — 6 de 7 criterios fallan: mediana r24 neta -0.052%,
hit 49.5%, IC95 bootstrap-fecha [-0.516, +0.322], no robusto sin top-4
(-0.067) ni sin par dominante (BNBUSDT, +0.032 ≈ cero), y el régimen se
INVIERTIE: flags en drawdown profundo de BTC rinden -0.225% vs +0.191% en
drawdown superficial. Los top-8 son micro-caps con recorridos no operables
(ARIA +667%). La celda verde del 11-pair backtest (56.2%, n=32) no
sobrevive la sección cruzada — era régimen y concentración, no mecanismo.
Ejecutado: watch_early.py retirada de producción (no emite banderas;
artefactos y --measure/--report conservados). El gate NO cambia: sigue
NEUTRAL salvo evidencia validada, y ahora también la última hipótesis de
entrada temprana tiene veredicto de retiro.
