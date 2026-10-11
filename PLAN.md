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
