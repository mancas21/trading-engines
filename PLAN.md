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

## 2. Saber si la regla 4 sirve o no — es la pregunta que importa

El veredicto de hoy, con n=65 filas dirigidas:

```
media  +0.000053   t=0.15   hit 44.6%   ->  INCONCLUSO, sin borde medible
```

Media positiva de 0.0053%, t=0.15. Eso es indistinguible del ruido. La regla
**no tiene ventaja demostrable**, y el pipeline de datos sí está sano (froz=0,
err=0, 3376 filas). O sea: no es un problema de datos, es que la señal no sirve.

**Qué hacer:**
- Dejarlo acumular hasta n≥200 dirigidas (~2-3 semanas a este ritmo: dir pasó
  de 145 a 485 en ~18h, así que puede ser mucho antes).
- NO promover la regla a nada mientras t<2. NO usarla para gatear alertas.
- Cuando n≥200: recalcular t y hit. Si sigue en t<2, **matar la regla** y no
  seguir puliéndola. Ese es el resultado honesto y hay que aceptarlo.

**Lo que NO hay que hacer:** seguir optimizando features para subir un AUC de
0.552. Ya está medido que no alcanza (permutado 0.510). Más tuning no arregla
una señal sin edge.

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

## Orden de ejecución

1. **(ahora)** Nada. Dejar correr. El scanner está acumulando.
2. Cerrar la incoherencia de intérprete (§1) en el próximo reinicio, no antes.
3. Commitear la capa ML + destrackear `obj/bin` (§3) — 15 min, riesgo cero.
4. Arreglar la entrega del cron (§4) — 10 min.
5. **Esperar a n≥200 y dictar sentencia sobre la regla 4 (§2).** Esta es la única
   decisión que cambia el rumbo del proyecto. Todo lo demás es mantenimiento.
