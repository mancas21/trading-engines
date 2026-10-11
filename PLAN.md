# PLAN — trading-engines

**Escrito 2026-10-11 09:10.** Este archivo es la respuesta a "por qué cada chat
decidimos de nuevo qué hacer". El orden de abajo es el orden real. No se salta
al item 4 sin cerrar el 1.

Estado verificado hoy antes de escribir esto:

```
supervisor  pid=8052   Python312       <- UNO solo, vivo
scanner     (hijo del 8052)            <- escribe normal
lock: 8052
:8090/health -> 200   cyc=5  rows=3376  dir=485  froz=0  err=0
watchdog fires: 0
```

---

## 1. Dejar el scanner corriendo — HECHO, pero con una incoherencia que hay que cerrar

**Está corriendo y acumulando.** Eso es lo pedido y está cumplido.

El problema que queda: **hay dos intérpretes en juego.** El supervisor vivo es
`AppData\Local\Programs\Python\Python312\python.exe`, pero `start-scanner.ps1` /
`run_supervisor.bat` lanzan el venv (`quant-alert-engine\venv`). O sea: cada vez
que el script arranca uno y ya hay uno viejo vivo, gana el viejo, el nuevo sale
con código 0 por lock tomado, y quedan dos.

Por qué duele: dos supervisores compiten por el lock, el segundo gana a veces, y
un stop deja huérfanos. Es exactamente la clase de fallo que causó los
supervisores duplicados que vimos.

**Cerrar así:**
- Regla única: el scanner corre con **el venv**. Punto.
- `stop-scanner.ps1` ya mata el árbol y verifica; usarlo SIEMPRE antes de un start.
- Añadir a `scanner_audit_tick.py`: si hay >1 supervisor, reportarlo (hoy no lo ve).
- Verificación: `start-scanner.ps1` tras un stop limpio → exactamente 1 supervisor,
  y su intérprete debe contener `quant-alert-engine\venv`.

**No hacer:** reiniciar el scanner ahora solo por estética. Está trabajando.
El reinicio limpio se hace cuando haya que tocarlo por otra razón.

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
