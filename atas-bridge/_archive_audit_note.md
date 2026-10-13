# audit.log.json — ARCHIVADO, no lo leas como medicion (2026-10-11)

## Que es

Un fichero JSON-lines de 19.848 bytes con entradas de `call_id`, `entry`,
`outcome`, `outcome_r`, `mfe_r`, `mae_r`, fechadas entre el 17-sep y el
07-oct-2026. Son 69 lineas.

## Por que se archiva

**El subsistema que lo producia ya no existe.** El endpoint que lo alimentaba
respondia a `POST /calls` en el puente. Ese endpoint devuelve **404** hoy:

```
POST http://127.0.0.1:8000/calls  ->  404
```

`verdict.py:709-713` lo deja escrito, y es la razon por la que la Regla 8
escribio donde escribia:

```
# 2026-10-07 — RUTA CORREGIDA. Apuntaba a atas-bridge/audit.log.json, que es el
# fichero de 19.848 bytes del subsistema /calls ya BORRADO (call_id, entry,
# outcome_r, mfe_r, mae_r). Escribir ahi mezclaria el registro de perdidas del
# grabador con los restos de un sistema que no existe. Ahora va a su propio
# fichero dentro de reader/, que es quien lo produce.
```

Asi que este fichero:
- **no lo escribe nadie** desde el 07-oct-2026 (su ultimo timestamp es
  2026-10-07 10:00:04),
- **no refleja el sistema actual**, y
- cualquier win-rate, expectancy o statisticia sacada de el describe una
  arquitectura que ya no esta en pie.

## Correccion de una lectura anterior

Se publico un analisis de win rate por simbolo sobre este fichero
(69 entradas, 8.3% win rate, HYPEUSDT 25%, SUIUSDT 0%). **Era invalido como
medicion del sistema actual** y no debio extraerse ninguna conclusion de el.
La comprobacion que faltaba era una sola: si el endpoint `/calls` que lo
alimentaba seguia vivo. No lo estaba.

## Donde van las perdidas ahora

La Regla 8 escribe en:

```
reader/audit_losses.jsonl
```

Ese fichero **no existe todavia** porque `verdict_recorder.py` no ha podido
escribir ni una linea. No es un bug del codigo: el grabador exige ATAS
abierto con el LiveExporter activo, y devuelve SKIP con honestidad cuando no
lo hay:

```
SKIP: ATAS no disponible: {"status": "no_data",
     "message": "LiveExporter indicator not active on chart"}
```

La cadena de condiciones para que Regla 8 escriba de verdad:

1. ATAS abierto, con el LiveExporter cargado en un grafico, y
2. `reader_supervisor.py --loop` corriendo.

Sin la primera, el grabador no puede inventar datos. Sin la segunda, no
graba. Las dos se arreglan a mano; ninguna es un problema de reglas.

## Al borrar esto

Se puede borrar sin riesgo. `bridge_server.py` no lo lee (verificado), y
`verdict.py` escribio en el sitio nuevo desde el 07-oct. Si se conserva, es
solo como registro historico de un sistema retirado — nunca como fuente para
decidir.