# FIXES — el gate del DOM (condición (c) de las reglas 6 y 7)

**Fecha:** 2026-10-11
**Estado:** EJECUTADO 2026-10-11 noche. Ver apéndice A al final del fichero.
**Autor:** auditoría de esta sesión, con los números medidos contra el sistema vivo.

---

## 0. Lo que está verificado (y lo que me equivoqué)

Antes de los fixes, el estado real. Todo lo de esta tabla está medido sobre el
sistema corriendo, no leído del código.

### 0.1 El gate de hoy y de dónde sale

`atas-bridge/rules.json` v3.4.1, reglas 6 y 7, condición (c):

```json
"dom_imbalance_max": 3,          // regla 6
"dom_imbalance_min": 8,          // regla 7
"dom_ema_field": "imbalance_pct_ema",
"dom_persistence_polls_min": 2
```

Y en el payload de `/indicators`, `dom` lleva DOS libros distintos pegados:

| Campo | Origen | Comportamiento medido |
|---|---|---|
| `imbalance_pct` | libro de **ATAS** | estable: 14–16 todo el día |
| `imbalance_pct_ema` | libro de **Binance** (WS top-15) | **−12.0 a +18.7 en un día** |

**El gate usa `imbalance_pct_ema` → o sea, el libro de Binance, no el de ATAS.**
El número que se muestra al lado (`imbalance_pct`) es de otro libro.

### 0.2 Por qué el EMA no sirve como gate

Sí suaviza — eso funciona:

```
libro Binance instantáneo : std 8.03   rango −11.3 a +12.2   (muestreado en 12s)
libro Binance EMA         : std 3.82   rango  −3.3 a  +7.8
```

Pero la entrada es tan ruidosa que incluso suavizada cruza los umbrales:

```
HOY, 67 veredictos:
  ema: min −12.0  max +18.7
  cruces del umbral +8 (regla 7):  12 veces
  cruces del umbral +3 (regla 6):  20 veces
  --> la condición (c) cambió de estado 32 veces
```

Secuencia real, un punto por minuto:

```
18:24  ema= 11.0   r7_c=MET    NEUTRAL
18:26  ema= 13.1   r7_c=MET    BULLISH
18:28  ema=  4.4   r7_c=FAIL   NEUTRAL
18:30  ema=  8.3   r7_c=FAIL
18:32  ema= 15.8   r7_c=MET
18:34  ema=  7.6   r7_c=FAIL
18:44  ema= −9.8   r7_c=FAIL
```

**Un gate "hard" que se invierte 32 veces al día no es un gate.**

### 0.3 Retractación de un diagnóstico anterior

En una pasada previa sostuve que un informe de cierre citaba "el campo
equivocado" al decir `DOM EMA +16.8%`. **Era falso y lo retiro.** La base de
datos lo desmiente:

```
18:32:05   imbalance_pct=16.702   imbalance_pct_ema=17.8   n=44934
```

El EMA **era** +17.8 con 44.934 muestras en ese momento. El informe acertó. Lo
llamé error porque lo medí cuatro minutos después, cuando ya valía +3.90.
**Asumí que un valor llamado "EMA" sería estable. No lo es.**

Consecuencia para todo lo demás: cualquier lectura de este gate —mía, del motor,
o de cualquier otro agente— es la foto de un instante inestable. Dos informes
correctos pueden contradecirse con dos minutos de diferencia.

---

## 1. Fix 1 — NO mover el gate al libro de ATAS (descartado)

Se propuso leer la condición (c) del libro de ATAS, porque su valor es estable
(14–16 todo el día). **Se descarta.** Motivos:

1. **ATAS es una aplicación de escritorio de Windows.** Cuando está cerrada no
   emite nada. Hoy mismo el grabador reportó
   `SKIP: ATAS no disponible: TimeoutError`. Un gate que depende de que alguien
   abra una ventana no es un gate.
2. **No es desplegable en VPS.** ATAS X necesita GUI. Lo mismo TradingView.
   Cualquier diseño que dependa de ellas está limitado a una máquina con
   escritorio y a alguien que la mantenga abierta.
3. **Cubre un solo símbolo.** Solo XRP tiene profundidad real. El resto son
   quotes REST sin CVD, footprint ni DOM. Un gate que solo juzga un símbolo no
   escala al resto del universo.

**Acción: ninguna.** Este fix se documenta como descartado para que no se
vuelva a proponer.

---

## 2. Fix 2 — Rediseñar (c) sobre el consenso multi-venue del scanner

### 2.1 Por qué el scanner y no otro suavizado

`scanner-engine/` ya mide imbalance de una forma que sí puede ser un gate. Su
`rules_eval.py` **no promedia: exige coincidencia entre venues**:

```python
usable = {v: x for v, x in f.touch_imbalance.items() if x is not None}
side_buy  = sum(1 for x in usable.values() if x > 0.5 + band)
side_sell = sum(1 for x in usable.values() if x < 0.5 - band)
hit = max(side_buy, side_sell)
sub_a = MET if hit >= need_n else UNMET     # N de M venues
```

Y cuando no hay datos, lo dice:

```python
return RuleResult("3", ..., UNEVALUABLE,
                  reason="no touch imbalance available (books carry no size)")
```

Características que ATAS no puede dar:

| | ATAS | scanner-engine |
|---|---|---|
| Disponibilidad | ventana abierta | **continuo** (poller) |
| Venues | 1 (el de ATAS) | **4** (binance, bybit, okx, kraken) |
| Símbolos | 1 (XRP) | **8 pares** |
| Construcción | 1 snapshot | **N de M venues** |
| Sin datos | silencio | **UNEVALUABLE explícito** |

El propio código del scanner lo dice: *"A single-venue engine (the ATAS one)
structurally cannot see any of this."*

### 2.2 Matices que hay que asumir (no es gratis)

1. **Los venues están arbitrados entre sí.** Binance, Bybit, OKX y Kraken no son
   cuatro medidas independientes de XRP: están pegados por arbitraje. "3 de 4
   coinciden" es mejor que uno, pero **no es 4× mejor**.
2. **`touch_imbalance` es solo el mejor bid/ask** — más nervioso que un top-15,
   no menos. La robustez viene de la construcción N-de-M, no del dato base.
3. **NVDA / SPCX / XAU no son cripto** y su conjunto de venues puede diferir. El
   denominador del "N de M" cambia por par.

### 2.3 Decisión pendiente: ¿libro o flujo?

Antes de escribir código hay que elegir qué mide (c):

**Opción A — libro multi-venue (N de M):** "N venues con imbalance más allá de
la banda, sostenido P ciclos". Usa lo que el scanner ya calcula. Cambio pequeño.

**Opción B — flujo multi-venue en ventana:** (a) y (b) ya miden flujo (racha de
CVD) y absorción por niveles (footprint). Una (c) de libro es una foto de un
instante; una (c) basada en **delta de taker agregado multi-venue sobre una
ventana** sería coherente con el resto de la regla y estable por construcción.
El scanner tiene su capa CVD corregida (`flow.py`) precisamente para esto.

**Recomendación: B, y si no hay tiempo, A.** El motivo: (c) existe para
responder "¿está el lado pasivo absorbiendo?". Una foto de libro de 15 niveles
es la peor forma de responder eso; una ventana de flujo lo responde mejor y no
oscila.

**Esto no lo decide el agente. Lo decide el usuario.**

---

## 3. Fix 3 — rules.json y código tienen que aterrizar juntos

Si (c) pasa a medir consenso multi-venue, **la regla tiene que decirlo**. Hoy
declara:

```json
"dom_imbalance_max": 3,
"dom_ema_field": "imbalance_pct_ema"
```

Si el código pasa a leer del scanner y el JSON sigue diciendo
`imbalance_pct_ema`, se reproduce exactamente el fallo que costó el día entero
de arreglos: **una regla que declara una cosa y evalúa otra.**

Regla operativa: **un cambio de fuente de datos es un cambio de contrato.** Van
en el mismo commit, o no van.

Forma sugerida del nuevo bloque (a validar):

```json
{
  "id": 7,
  "title": "Accumulation Threshold",
  "enforcement": "hard",
  "conditions": {
    "cvd_consecutive_up_min": 2,
    "footprint_levels_min": 3,
    "footprint_buy_pct_min": 65,
    "dom_source": "scanner_multi_venue",
    "dom_vac_band": 0.15,
    "dom_vac_venues_min": 3,
    "dom_vac_polls_min": 2
  }
}
```

Los tres campos `dom_vac_*` son **inventados en esta propuesta** y hay que
calibrarlos contra la distribución real de `touch_imbalance` antes de fijarlos.
No copiar estos números.

---

## 4. Fix 4 — Desambiguar los dos `rules.json` (barato, hacer primero)

Existen dos ficheros de reglas con **los mismos números de ID y significados
distintos**:

```
atas-bridge/rules.json       reglas 1–8   (CVD, footprint, DOM, dist/acum)
scanner-engine/rules.json    reglas 1–4   (absorción, vacuum, ...)
```

El "regla 3" del scanner (liquidity vacuum) **no es** el "regla 3" de
`atas-bridge` (no inventar probabilidades). El "regla 2" del scanner
(absorción esfuerzo-vs-resultado) **no es** el "regla 2" de `atas-bridge`
(agotamiento de CVD).

**Esto es una trampa activa**: cualquiera que lea "la regla 3 hace X" sin decir
de qué fichero está hablando leerá el número equivocado. Ya casi me pasó en esta
sesión al citar el N-de-M como "regla 3".

**Acción (barata, sin riesgo, sin tocar lógica):**
- prefijar los IDs: `AT-1..AT-8` en `atas-bridge`, `SC-1..SC-4` en el scanner, o
- añadir un campo `"namespace": "atas"` / `"namespace": "scanner"` a cada
  fichero, y
- hacer que todo informe que cite una regla diga el fichero.

**Hacer esto ANTES del Fix 2**, para que la próxima auditoría no confunda los
números mientras se cambia la lógica.

---

## 5. Lo que NO hay que hacer

- **No subir la τ del EMA** para que deje de cruzar. Disimula el síntoma: un
  valor que necesita 10 minutos de suavizado para ser estable no sirve para
  confirmar en 2 barras. Y seguiría viniendo de un solo venue.
- **No mover el gate a ATAS** (Fix 1, descartado).
- **No cambiar el código sin cambiar el JSON** (Fix 3).
- **No tocar el significado de la condición (c) sin re-auditar** las reglas 6/7:
  los t-stats del audit v4 (−14.12 / −15.73 / −9.32) miden las reglas con la
  condición (c) ACTUAL. Si (c) cambia, esas cifras dejan de describir la regla
  nueva y hay que volver a medir.

---

## 6. Orden recomendado

| # | Fix | Coste | Riesgo | Depende de |
|---|---|---|---|---|
| 1 | Desambiguar namespaces (Fix 4) | bajo | ninguno | — |
| 2 | Decidir libro vs flujo (Fix 2.3) | decisión | — | — |
| 3 | Calibrar `dom_vac_*` contra datos reales | medio | medio | (2) |
| 4 | Rediseñar (c) + `rules.json` en un commit (Fix 2 + 3) | alto | alto | (2),(3) |
| 5 | Re-auditar reglas 6/7 con la (c) nueva | medio | — | (4) |

---

## 7. Cómo verificar que el fix funcionó

Criterio objetivo, medible, sobre el resultado del Fix 2:

1. **La condición (c) no puede cambiar de estado más de N veces al día.** Hoy
   son 32. Un gate usable debería estar en el orden de unidades, no decenas.
   *(El umbral N hay que fijarlo antes de medir, no después.)*
2. **El gate es evaluable con ATAS cerrado.** Probar con ATAS cerrado y
   confirmar que (c) sale MET/FAIL — no UNEVALUABLE y no silencio.
3. **El gate cubre más de un símbolo.** Hoy solo puede juzgar XRP.
4. **`rules.json` y el código dicen lo mismo.** El test: leer el JSON y el
   código por separado y comprobar que la condición (c) usa el campo que el JSON
   nombra. Esto ya falló dos veces en esta sesión por no comprobarlo.

---

## 8. Estado del sistema en el momento de escribir esto

```
ATAS :8000        vivo (XRPUSDT, age 0.2s, timestamp avanza)
TradingView :9222 TVDesktop/3.4.1
CoinGecko         CAÍDO (HTTPError, rate limit) — 3 intentos
QAE :8081         healthy (estaba caído, reiniciado)
reader            grabando, 0 fallos, bias NEUTRAL
scanner-engine    corriendo, 8 pares, 4 venues, scanner.db al día
```

Datos del último veredicto (18:47):

```
rule6  a=FAIL | b=FAIL | c=MET    → no
rule7  a=FAIL | b=FAIL | c=FAIL   → no
BIAS: NEUTRAL
dom ema=+1.5% (n=53359)
```

---

## 9. Pendientes que NO son este fix

- **CoinGecko caído** — el macro de XRP queda solo con Yahoo.
- **`missing_bars=960` de 328** en la serie CVD. El gate de integridad que se
  añadió hoy da `ok` (86 barras contiguas desde el último hueco) y los huecos
  son antiguos, pero el total sigue siendo alto.
- **Regla 5** sigue marcada NO implementada en `_rule_coverage()` — es una regla
  de conducta, no comprobable por código. Correcto, pero conviene que el JSON no
  la presente como `hard` sin más.
- **1178 veredictos y 0 resueltos** en `reader.db` cuando se escribió el fix del
  gate de integridad. La regla 8 no puede dispararse hasta que haya
  `forward_return` resuelto.


---

## Apéndice A — ejecución (2026-10-11 noche, auditoría de ZCode)

**Orden seguido:** Fix 4 → decisión 2.3 → calibración → un commit lógico (c) +
JSON → re-auditoría con retracción. QAE re-congelado (la otra sesión lo había
revivido a las 18:16; stop-qae.ps1 + lanzador ya neutralizado).

**Fix 4 — hecho, con una variante:** namespace como CAMPO (`"namespace": "atas"` /
`"scanner"` en cada rules.json) y no como prefijo de IDs — renombrar los IDs
rompería a todos los consumidores que buscan por "1"-"4" (el fallo de contrato
que el Fix 3 prohíbe). Los informes citan su fichero: auditor del scanner,
promotion, audit_atas, y el render de la puerta etiqueta `AT-6`/`AT-7`. Test:
`scanner-engine/tests/test_rules_namespace.py`.

**Fix 2 — hecho, Opción A, con un descubrimiento que cambia la construcción:**
la calibración DESCARTÓ el touch de mejor nivel como base del consenso. Medido
sobre 60 ciclos vivos: el touch es estructuralmente unilateral (mediana
|x−0.5| = 0.275 — el mejor nivel casi siempre está cargado de un lado) y
desacoplado entre venues — consenso 3-de-4 del mismo lado: **0 de 480 ciclos**.
La condición (c) mide ahora `book_imb_5` (libro top-5 por venue, escala ±1,
misma construcción que los números DOM de ATAS/Binance), expuesto por el nuevo
endpoint `GET :8090/features`. Sobre esa base SÍ hay consenso (19.1% de
ciclos-pair a thr 0.25) y la simulación sobre la serie real de XRP mostró
**cero transiciones MET↔no-MET de la puerta** en todo el rango de umbrales.

**Fix 3 — hecho:** rules.json y código en el mismo cambio. Contrato nuevo en
AT-6/AT-7: `dom_source: "scanner_multi_venue"`, `dom_vac_thr: 0.25`,
`dom_vac_venues_min: 3`, `dom_vac_polls_min: 2` (con la provenance de
calibración dentro del propio JSON). El gate viejo (`dom_imbalance_max/min`,
`dom_ema_field`) eliminado. Un solo evaluador: `verdict.dom_vac_eval`, usado
por la puerta Y por el grabador (que además tenía umbrales divergentes vivos:
dom 5/10 y pct 70 contra el 3/8 y 65 de rules.json — heredado de la
transcripción manual, ahora muerto). signal_engine ya no evalúa AT-6/AT-7: un
módulo puro no puede juzgar una regla que necesita multi-venue + persistencia.
Test de contrato: `reader/test_dom_gate.py` (JSON↔código, N-de-M, UNEVALUABLE
con venues insuficientes, racha file-backed que muere con la fuente).

**Re-auditoría — mecanismo activado:** `audit_atas.py` MEASUREMENT_VERSION 1→2
con retracción; baseline v2 ya asentado. Barra de aceptación PRE-REGISTRADA
dentro del propio rules.json: **N=10 transiciones MET↔no-MET/día** en r7_c/r6_c
de reader.db (§7.1 — el umbral se fijó antes de medir). Verificar mañana.

**Lo que la propuesta no vio y la ejecución corrigió:**
1. El grabador (verdict_recorder) duplicaba la puerta con OTROS umbrales —
   alinear la (c) sin alinear el grabador habría creado dos verdades.
2. El touch como base del consenso (punto 2.2 del propio doc) no era "más
   nervioso": era inutilizable por construcción.
3. `dom_vac_*` inventados → sustituidos por calibración empírica con muestra,
   método y barra de aceptación escritos en el JSON.

**Pendiente de mañana:** medir N contra la barra pre-registrada; re-calibrar a
7 días cuando el ledger acumule; la puerta quedó 3/4 fuentes (TradingView
intermitente por CDP, no del gate).
