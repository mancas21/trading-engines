# vps-reader — grabador headless de la condición (c), 24/7

El motor del PC está apagado el **84 % del reloj** (medido: 195,3 h de 232 h en
`reader.db`, 9 huecos >30 min, y la caída del 16-oct −4,7 % cayó **entera** dentro
de un hueco de 17,49 h). La causa no es software: el PC se apaga cada tarde
(`Kernel-Power id=109` a las 17:59 del 15-oct; arranque a las 11:02 del 16-oct).

La condición **(c)** — consenso de libro multi-venue — **no** depende de ATAS.
La sirve `scanner-engine`, que ya corre 24/7 en el VPS vía systemd. Este
componente la graba de forma continua usando el patrón de `paper-monitor`
(proceso corto + cron + watchdog externo de solo lectura + backup).

## Qué evalúa y qué no — sin adornos

| Condición | Estado | Por qué |
|---|---|---|
| (c) consenso multi-venue | **EVALUABLE** | `scanner-engine :8090`, la única fuente viva en el VPS (4 venues por REST público) |
| (a) racha de CVD | **UNEVALUABLE** | el CVD lo sirve `atas-bridge` por WebSocket de Binance; desde la IP del VPS Binance responde **HTTP 418** (throttled). Un CVD de okx/bybit/kraken **no** es el CVD de Binance: sustituirlo sería inventar |
| (b) footprint | **UNEVALUABLE** | lo sirve **ATAS**, app de escritorio de Windows |

Por eso **no emite señales direccionales**: las reglas 6/7 son `(a) AND (b) AND (c)`
y sin dos de las tres nunca pueden cumplirse. **Es un grabador, no un oráculo.**
`bias` es siempre `NEUTRAL` porque el contrato de `verdict.py` dice que una
condición `UNEVALUABLE` fuerza `NEUTRAL` — y eso es información, no un fallo:
significa «el gate no puede opinar».

Su valor es que **(c) es justo la condición que falla su propio criterio
pre-registrado** (`rules.json` 7.1: ≤10 transiciones MET↔no-MET por día; medido
61–63/día sobre 27,5 h de ventana). Para decidirla hacen falta días completos, y
eso en el PC es imposible.

## Una sola implementación de (c)

No reimplementa nada. `vps_reader.py` **importa `verdict.py`** (el mismo fichero
que usa el gate del PC, copiado en el deploy) y llama a `verdict.dom_vac_eval()`.
El esquema de la tabla `verdicts` es el **mismo** que el del reader, así que
`build_dom_vac_calibration.py` funciona sin cambios: la calibración del VPS se
genera con la herramienta del PC.

```
~/vps-reader/
├── vps_reader.py              # el ciclo (cron lo lanza cada minuto)
├── reader/
│   ├── verdict.py             # copia del gate del repo (una sola (c))
│   ├── build_dom_vac_calibration.py
│   ├── dom_vac_calibration.json   # se genera aquí (no viene en el deploy)
│   ├── dom_gate_state.json        # rachas; lo escribe verdict.py
│   └── reader.db                  # ledgers: tabla `verdicts`
└── atas-bridge/rules.json     # umbrales que verdict.py lee
~/vps-reader-backup/{watchdog.sh,backup.sh,watchdog.log,watchdog.heartbeat,snap-*.*}
```

`verdict.py` resuelve `HERE.parent/atas-bridge/rules.json` y
`HERE/dom_vac_calibration.json`, por eso el árbol replica el del repo: el gate se
usa **sin modificarlo**.

## Cadencia — la diferencia que hay que saber

El reader del PC corre cada ~30 s. En cron el mínimo es 1 minuto, así que
`dom_vac_polls_min = 2` significa **2 minutos de hold** en vez de 1. Las guardas
del propio `verdict.py` siguen satisfechas: `_DOM_GATE_POLL_S` (15 s) → un tick de
cron es siempre una observación nueva; `_DOM_GATE_STALE_S` (120 s) → la racha no
muere entre ticks.

## Despliegue

```bash
# desde el PC: armar el stage, subirlo y ejecutar install.sh en el VPS
scp -r <stage> vps-japan:/tmp/vps-reader-deploy
ssh vps-japan 'bash /tmp/vps-reader-deploy/install.sh'
```

`install.sh` es idempotente y **no** toca `reader.db`, `dom_vac_calibration.json`
ni `dom_gate_state.json` (son estado, no binario).

## Operación

| Cuándo | Qué | Dónde |
|---|---|---|
| cada minuto | un ciclo: lee el scanner, evalúa (c), graba fila y línea de dashboard | `cycle.log` |
| cada hora (:11) | watchdog externo: mtime de `cycle.log`, `/health` del scanner, disco, rotación | `watchdog.log`, `watchdog.heartbeat` |
| diario (06:05) | backup: copia online consistente (`sqlite3 .backup` vía python3) + `quick_check` + sha256, retención 90 días | `snap-YYYY-MM-DD.tar.gz` |

## Calibración — heredada del PC, y por qué es legítimo

`dom_vac_eval` no puede decir nada hasta que exista `dom_vac_calibration.json`
(media/std de `book_imb_5` por venue). Sin banda no hay z, y sin z no hay votos:
graba `UNEVALUABLE` — **correcto**, porque aún no hay con qué comparar.

El VPS arranca con la calibración **heredada del PC** (n=341/venue), con la
procedencia escrita dentro del fichero (`source` + `note`). No es una
sustitución de una fuente caída: `book_imb_5` por venue es una propiedad del
**mercado** para un instrumento y unos venues dados — lo mide el mismo código de
scanner leyendo los mismos libros. La estimación del PC es **la misma cantidad**
con 10× más muestra que las ~30 que tendría el VPS el primer día.

Regenerarla desde datos del VPS cuando el ledger tenga muestra suficiente
(n ≥ 300/venue, ~5 h):

```bash
cd ~/vps-reader/reader && python3 build_dom_vac_calibration.py
```

Al regenerarla, la marca de bootstrap desaparece por sí sola (el generador
reescribe `source`/`note`). **No** se regenera por cron a propósito: una
calibración que se recalcula sobre la muestra que incluye el régimen actual
«aprende» ese régimen y las z dejan de ser extremas. En el PC se congeló
(`generated_at` 2026-10-15); aquí igual: regenerar es una decisión manual y
fechada.


## Advertencias (comparabilidad con el PC)

- **JUP/USD tiene 3 venues aquí, no 4.** Binance está throttled (HTTP 418) desde
  esta IP, así que `dom_vac_raw` trae okx/kraken/bybit. Con
  `dom_vac_venues_min = 3`, MET exige **3 de 3** aquí frente a **3 de 4** en el
  PC: la tasa de MET **no** es directamente comparable. Las reglas no se tocan
  (cambiarlas crearía una divergencia silenciosa); la reducción se declara.
- **Con 3 venues, MET es estructuralmente raro** — y puede ser imposible: `_vac_z`
  *excluye* un venue cuya std de calibración sea ~0 (`if m is None or not sd:
  continue`), así que un venue degenerado deja 2 votantes donde se piden 3.
- Por eso **el entregable para medir el criterio 7.1 es la serie cruda**
  (`dom_vac_raw` = `book_imb_5` por venue, 1/min) **y los `counts` por lado**, no
  el estado derivado. Desde el crudo se recalcula la transición MET↔no-MET con
  cualquier umbral y cualquier conjunto de venues, offline y sobre días completos
  — que es exactamente lo que nunca se pudo hacer en el PC.
- **JUP/USD es un par; la calibración de (c) es por venue sobre la muestra de un
  solo par.** Mezclar pares la corrompería: cada par que se quiera seguir
  necesita su propio directorio (igual que `paper-monitor` usaba `PAPER_HOME`).
  Para añadir otro par: nuevo `VPS_READER_SYMBOL`, nuevo árbol, nueva calibración.
- `JUP/USD` se añadió a `~/scanner-engine/pairs.py` (bloque copiado del
  `pairs.py` local; respaldo `pairs.py.bak-preJUP`). Sin ese bloque el scanner
  no publica el par y el reader graba `UNEVALUABLE` con `venues=0`.

