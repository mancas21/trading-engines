# Ejecución — puente al testnet de Binance

Estado: **bucle completo operativo en testnet**. Señal viva calculada con el
engine auditado → tamaño por riesgo → bracket con stop en el exchange →
reconciliación periódica (recrea el stop si desaparece, detecta cierres).
Validado end-to-end el 2026-10-25 (`evidence_automation_2026-10-25.json`).

Aviso: esto valida el *plumbing*. El testnet cobra 4 pb taker (no 5), su libro
es otro y sus precios no son los de producción. Ninguna cifra de PnL de testnet
es evidencia de edge.

## 1. Claves (lo haces tú, una vez)

1. Entra en <https://testnet.binancefuture.com> con GitHub o email.
2. `API Key` → `Create API` → permisos **Reading + Futures**. Sin retiros.
3. Copia la key y el secret en `execution/.env`:

```
BINANCE_TESTNET_KEY=...
BINANCE_TESTNET_SECRET=...
ALLOW_ORDERS=0
MAX_ORDER_NOTIONAL_USD=650
ALLOW_SYMBOLS=ETHUSDT,SOLUSDT,SUIUSDT,XRPUSDT
```

`ALLOW_ORDERS=0` deja el cliente en seco: construye la orden, la registra en
`logs/orders.jsonl` como `not_sent` y no sale a la red.

No pegues las claves en el chat: quedan en el historial de la sesión. El script
solo muestra `********...ab12 (len 64)`.

## 2. Comprobar

```
python execution/check_testnet.py --symbol XRPUSDT
```

Lee host, hora (offset), filtros del símbolo, libro, cuenta, posiciones y
órdenes abiertas; al final imprime la orden que enviaría, sin enviarla.

Códigos: `0` ok · `2` faltan claves · `3` clave rechazada · `4` símbolo caído.

## 2b. Prueba de plumbing (escribe en la cuenta)

```
python execution/test_order.py --mode limit     # coloca y cancela, sin fill esperado
python execution/test_order.py --mode market    # abre y cierra 20 USDT, mide fills y fees
```

Ambos exigen `ALLOW_ORDERS=1` en el entorno **y** que el cliente se construya con
`dry_run=False`. Abortan si el símbolo ya tiene posición u órdenes abiertas.

Medido el 2026-10-25 en XRPUSDT (ver `evidence_testnet_2026-10-25.json`):

| prueba | resultado |
|---|---|
| LIMIT GTX 15,9 @ 1,2567 | NEW → leída → CANCELED, 0 ejecutado |
| MARKET BUY 15,4 @ 1,2958 | filled, comisión 0,007982 (4,00 pb) |
| MARKET SELL reduceOnly @ 1,2956 | filled, comisión 0,007981 (4,00 pb) |
| coste del roundtrip | 1,54 pb spread + 8 pb fees = **9,54 pb** |

**El testnet no sirve para validar costes**: `commissionRate` y `userTrades`
confirman 4 pb taker / 2 pb maker, mientras producción USDT-M Regular cobra
5 pb / 2 pb. Subestima 2 pb por roundtrip y su libro es distinto. Solo prueba
firma, filtros, órdenes, cancelación y contabilidad de posición.

Otra rareza: las respuestas `MARKET` del testnet devuelven `avgPrice=0`. El
precio medio se reconstruye desde `/fapi/v1/userTrades`.

## 3. Router (señal → posición protegida)

```
# plan, no envía nada
python execution/router.py --symbol XRPUSDT --side LONG --stop 1.2760 --take-profit 1.3350

# ejecutar
$env:ALLOW_ORDERS="1"
python execution/router.py --symbol XRPUSDT --side LONG --stop 1.2760 --take-profit 1.3350 --require-candidate --execute
```

También acepta `--signal senal.json`:

```json
{"symbol": "XRPUSDT", "side": "LONG", "stop": 1.2760,
 "take_profit": 1.3350, "candidate_id": "S10", "run_id": "..."}
```

Decisiones del operador (2026-10-25):

1. **Una posición por símbolo.** Si hay posición u órdenes vivas, aborta. No
   promedia ni reemplaza.
2. **Tamaño por riesgo.** `--risk-pct 0.5` del equity hasta el stop, con tope de
   nocional (`MAX_ORDER_NOTIONAL_USD`) y tope de margen inicial (`--margin-pct 25`).
   Se queda con el mínimo, redondea hacia abajo al step y dice qué restricción
   manda. Con 650 USDT de tope y stops de ~2-2,7%, el tamaño lo fija el riesgo
   (0,25%), no el tope. El tope **no** se aplica a cierres `reduceOnly`: una
   posición grande siempre se puede aplanar.
3. **Stop en el exchange.** `STOP_MARKET` con `closePosition=true` y
   `workingType=MARK_PRICE`. Si el motor se cae, el stop sigue vivo.

Si el stop no se puede colocar, el router **cierra la posición a mercado** y
registra el fallo. Si el stop no se puede representar (fuera de la banda
`PERCENT_PRICE`, p. ej. ±5% en XRP), **no entra**.

`--require-candidate` verifica contra `scanner-engine/backtest_link.py` que el
símbolo tenga candidato admitido (PF>1.2, n≥30, excess>0, costes 11 pb).

## 3c. Auto-router (señal viva → posición, sin manos)

```
python execution/live_signal.py --all          # estado que ve el engine
python execution/auto_router.py                # plan: no envía
$env:ALLOW_ORDERS="1"
python execution/auto_router.py --execute      # un ciclo real
python execution/auto_router.py --loop --execute --interval 300
```

Cada ciclo, por símbolo con candidato admitido:

1. calcula la dirección con `live_signal.compute()` — el **mismo** engine del
   backtest (`cbpack_funding_engine`, hash en el artefacto), no una
   reimplementación;
2. lee la posición real y decide `open` / `close` / `reverse` / `keep` / `wait`
   / `skip`;
3. si hay que operar, delega en `router.py` (riesgo + bracket) o `flatten.py`
   (cierre). Una posición por símbolo.

Política de entrada tardía: si el engine lleva más de `--max-entry-lag-bars`
(24 = 1 día) en la posición, **no se persigue**. Un motor que arranca a mitad
de una tendencia de 186 horas no replica el backtest, copia su resultado. Si la
posición viva es la contraria y la señal es vieja, se cierra pero no se abre la
pata nueva.

El stop de una entrada no fresca es el **nivel que fijó el engine** (`sig.stop`),
no un stop recalculado desde el precio actual: reanclar cambia la invalidación
(ETH 2026-10-25: 2394,85 del engine vs 2400,22 desde el mark). Si el precio ya
cruzó ese nivel, no se abre: entraría directo al stop.

Concentración: `conc_passes=false` (los 5 candidatos) → riesgo **x0.5**, es
decir 0,25% del equity. No se veta: el engine advierte que recortar la cola
derecha penaliza a S02/S10 por construcción. `--require-conc` en el gate del
scanner queda disponible para quien quiera el veto estricto.

El estado vive en `state/positions.json` (escritura atómica) y se escribe la
**intención antes** de enviar la entrada: si el proceso muere entre el fill y
el stop, el reconciliador sabe qué stop corresponde. Lock por PID en
`state/auto_router.lock`.

## 3d. Reconciliador (cuenta real ↔ estado)

```
python execution/reconcile.py                     # plan
$env:ALLOW_ORDERS="1"
python execution/reconcile.py --execute           # un ciclo
python execution/reconcile.py --loop --execute --interval 60
```

Qué hace, por símbolo con estado:

| caso | acción |
|---|---|
| estado abierto y cuenta plana | reconstruye el cierre con `userTrades` (precio, PnL, fees de entrada y salida), infiere stop/TP/otro y **borra el estado** |
| posición viva sin `STOP_MARKET` | lo recrea al nivel guardado (dry-run dice qué haría) |
| posición viva y precio ya cruzó el stop | cierra a mercado: es lo que el stop habría hecho |
| stop con trigger distinto al estado | avisa, no lo mueve |
| cantidad del estado ≠ cuenta | avisa (parcial o manual) |
| posición sin estado (huérfana) | avisa; solo cierra con `--close-orphans` |
| `entering` de hace <120 s | espera: el router puede estar entrando |

La causa de la salida es **inferida** (el exchange no dice “saltó el stop”): se
compara el precio de salida con los niveles guardados y una salida que no encaja
se reporta como “otro”, no como stop.

## 3e. Prueba de plumbing re-ejecutable

```
$env:ALLOW_ORDERS="1"
python execution/selftest_plumbing.py
```

Sustituye la señal por una sintética fresca en XRP (el engine está plano), abre
con 25 USDT, borra el stop, reconcilia, cierra a mercado sin tocar el estado,
comprueba la detección de cierre, la huérfana, el margen de `entering` y deja la
cuenta plana. No valida estrategia. Tests offline:

```
python -m pytest execution/tests/test_automation_logic.py -q     # 17 passed
python -m pytest scanner-engine/tests/test_backtest_link.py -q   # 10 passed
```

### Aplastar un símbolo

```
python execution/flatten.py --symbol XRPUSDT --execute
```

Cancela órdenes normales y algo, y cierra la posición a mercado. Solo ese
símbolo. Es la herramienta de emergencia.

### Órdenes condicionales: endpoint nuevo

Desde 2026 el endpoint clásico rechaza `STOP_MARKET` y `TAKE_PROFIT_MARKET`:

```
HTTP 400 code=-4120 "Order type not supported for this endpoint.
Please use the Algo Order API endpoints instead."
```

Hay que usar `POST /fapi/v1/algoOrder`:

| parámetro | valor |
|---|---|
| `algoType` | `CONDITIONAL` |
| `type` | `STOP_MARKET` o `TAKE_PROFIT_MARKET` |
| `triggerPrice` | precio de disparo (no `stopPrice`) |
| `closePosition` | `true`, sin `quantity` ni `reduceOnly` |
| `workingType` | `MARK_PRICE` |

`closePosition=true` exige **posición abierta** (-4509 si estás plano). Las
órdenes no aparecen en `/fapi/v1/openOrders` sino en `/fapi/v1/openAlgoOrders`,
y el campo de tipo se llama `orderType`, no `type`. Se cancelan con
`DELETE /fapi/v1/algoOrder?algoid=...`.

Medido el 2026-10-25: entrada 76,9 XRP @ 1,2998, stop `algoId 1000000257290766`
(1,276), TP `algoId 1000000257290782` (1,335), ambos `NEW`; después cancelados y
posición cerrada. Cuenta plana.

## 3b. Enviar a mano (sin router)

Dos llaves a la vez, a propósito:

* `ALLOW_ORDERS=1` en el entorno
* `place_order(..., dry_run=False)`

```python
from binance_testnet import TestnetClient
c = TestnetClient(dry_run=False)
c.set_leverage("XRPUSDT", 2)
c.place_order(symbol="XRPUSDT", side="BUY", qty=10, order_type="MARKET")
```

Topes locales que no se pueden saltar por parámetro: lista blanca de símbolos,
`MAX_ORDER_NOTIONAL_USD`, `leverage <= 20`, y `check_order()` contra tick/step/
minNotional del exchange antes de firmar.

## 4. Lo que el testnet NO prueba

* **Precios**: el libro del testnet es otro (y más fino). Las señales deben
  seguir calculándose con datos de producción; el testnet solo prueba el
  *plumbing* (firma, filtros, órdenes, cancelaciones, listenKey).
* **Fills**: la cola y el impacto no son realistas.
* **Persistencia**: Binance resetea balances del testnet el día 1 de cada mes.

## 5. Límites conocidos

* **Semántica del stop**: el bracket representa un stop fijo y, si hay, un TP
  fijo. Si el candidato saliera por trailing, tiempo o señal contraria dentro de
  la barra, el stop del exchange no lo replica: el auto-router solo actúa al
  cierre de cada barra de 1h (o cuando corre el ciclo). El stop fijo acota la
  pérdida; no reproduce la salida del backtest.
* **Funding**: el engine cobra funding histórico; aquí lo cobra el exchange. El
  PnL del reconciliador no reconstruye funding (`userTrades` no lo trae); para
  eso está `/fapi/v1/income`.
* **Supervisión**: los bucles `--loop` no se auto-reinician si el proceso muere.
  Mientras corre el stop, la posición sigue protegida; al volver, el
  reconciliador recrea lo que falte. Para operación continua hace falta un
  supervisor (no incluido).
* **Símbolos**: `ALLOW_SYMBOLS` filtra; hoy ETHUSDT, SOLUSDT, SUIUSDT y XRPUSDT
  (los 4 con candidato admitido). El router aborta si el stop no es
  representable en la banda `PERCENT_PRICE` de cada uno.
* **Testnet ≠ producción**: 4 pb taker, libro distinto, reseteo de balances.
  El modelo de costes es 11 pb (5+0,5 por lado) y vive en el backtest, no aquí.

## 6. Alpaca (acciones US) — segunda pata de ejecución

Las secciones 1-5 son la pata crypto (Binance). Esta es la pata de acciones US,
contra el **broker de verdad** (cuenta paper de Alpaca), orquestando su CLI
oficial (`alpaca.exe`) en lugar de una API firmada: **el .env no lleva claves**,
la autenticación vive en el perfil del CLI (`alpaca profile login`, una vez).

Piezas (espejo de la pata crypto):

| fichero | rol |
|---|---|
| `alpaca_broker.py` | cliente: locate del binario, envoltura JSON del CLI, lista blanca, tope de nocional, dry-run por defecto, log en `logs/alpaca_orders.jsonl` |
| `alpaca_check.py` | chequeo SOLO LECTURA (códigos 0/2/3/4 como `check_testnet.py`) |
| `alpaca_router.py` | señal → bracket protegido, mismas decisiones del operador que `router.py` |
| `tests/test_alpaca_broker.py` | 22 tests offline de la política de decisión |

```
python execution/alpaca_check.py --symbol AAPL
python execution/alpaca_router.py --symbol AAPL --side LONG --stop 335.50 --take-profit 370.0
$env:ALLOW_ALPACA_ORDERS="1"
python execution/alpaca_router.py --symbol AAPL --side LONG --stop 335.50 --execute
python execution/alpaca_router.py --symbol AAPL --close --execute    # emergencia
```

Variables del `.env` (`ALLOW_ALPACA_ORDERS`, `MAX_ALPACA_NOTIONAL_USD`,
`ALLOW_ALPACA_SYMBOLS`) — el binario se localiza con `ALPACA_CLI` si no está en
PATH ni en `~/go/bin/alpaca.exe`.

Decisiones específicas de esta pata (2026-10-29):

1. **Bracket atómico.** Alpaca acepta entrada + stop + TP en una sola
   submission (`--order-class bracket`): si la entrada llena, el stop ya vive
   en el exchange aunque el proceso muera. Se verifica al final igualmente:
   posición viva sin orden protectora = exit 6.
2. **Shares enteras.** El sizing redondea a shares completas (los brackets
   fraccionales no se pisan). Con tope de 1000 USD y acciones caras el
   redondeo puede dejar el riesgo muy por debajo del objetivo: el router avisa.
3. **Caja sin apalancamiento.** El tope de caja usa `cash`, no `buying_power`,
   aunque la cuenta sea margin 4x. El router no usa leverage.
4. **Mercado cerrado no entra.** Las órdenes `day` en cola llenarían en la
   apertura siguiente con un stop pensado para el precio de hoy. El plan (sin
   `--execute`) sí se prepara fuera de horario; con `--execute` bloquea.
5. **Sin gate de candidato ni state store.** No hay backtest de acciones US en
   el repo (quien ejecuta con `--execute` responde de la señal), y el estado
   crypto de `state/positions.json` no se toca: mezclar AAPL ahí confundiría
   al reconciliador de Binance. Trazabilidad en `logs/alpaca_router.jsonl`.

Comisión 0 en acciones: el coste real es el spread impreso en el plan. La
cuenta paper no prueba costes ni fills reales, igual que el testnet — solo
prueba el plumbing (submission, bracket, verificación, cierre).

## 6b. MCP server de Alpaca (acceso del agente en vivo)

El `alpaca-mcp-server` oficial (65 herramientas: cuenta, órdenes, posiciones,
datos de acciones, watchlists, news) da al **agente de ZCode** acceso directo
al broker en conversación — complementa, no sustituye, al cliente por CLI de
arriba (que es el camino programático del engine).

Configurado en `~/.zcode/cli/config.json` → `mcp.servers.alpaca`:

* `command`: `uvx alpaca-mcp-server --env-file execution/.env` — **las claves
  no viven en la config de ZCode**, se leen del `.env` (que está gitignoreado).
* `ALPACA_PAPER_TRADE=true` (explícito) y `ALPACA_TOOLSETS` recortado a
  `account,trading,watchlists,stock-data,news` (65 herramientas enteras son
  mucho contexto; amplíalo en la misma entrada si haces falta opciones, crypto
  o acciones corporativas).
* `enabled: false` hasta que existan claves.

Puesta en marcha (una vez, lo hace el operador):

1. Dashboard <https://app.alpaca.markets> → cuenta **paper** → API keys
   (empiezan por `PK`). **Las claves del login OAuth del CLI no sirven**: la
   API REST necesita key/secret, y el CLI no las exporta.
2. `python execution/set_alpaca_mcp.py` — pide key y secret ocultas, las
   escribe en `execution/.env` y pone `enabled=true` en la entrada del server.
3. Reiniciar ZCode; comprobar en Settings → MCP que `alpaca` está conectado.

Seguridad: el server habla con la Trading API directamente (las órdenes que el
agente proponga se ejecutan al confirmarse), paper por defecto, y quedan los
mismos topes del repo solo si se usa el router por CLI — el MCP no pasa por
`alpaca_broker.py`, así que sus topes locales no le aplican. Trátalo como
consola del broker, no como parte del bucle de decisión del engine.
