# PLAN DE REDISEÑO — motor Quant Alert Engine

Fecha: 2026-10-06 · Estado: plan aprobado para ejecutar

---

## 1. Qué está medido y por qué esto es un rediseño, no una optimización

He auditado el motor entero. Los números no son opiniones:

| Medición | Resultado | Significado |
|---|---|---|
| Corpus del motor | 2.808 filas / 28 días / 1.016 dirigidos | Edge neto **−0,1341%** por señal |
| Familias con edge positivo | **0 de 29** | Ninguna paga tras costes |
| Flujo de takers (Binance, 5 perps, 120d) | **0 de 75 celdas** | El delta real tampoco tiene edge |
| Reversión a la media (walk-forward) | OOS **−0,000033R, t=−0,05, PF 0,994** | Hipótesis muerta |
| Ruta de llamadas / reglas | **n=24, winrate 37,5%, −0,4196R, CI [−0,80; −0,02]** | Perdedor demostrado |
| Reglas 6/7 sobre datos ATAS reales | Emitió veredicto correcto a las 21:48 | **Esto sí funciona** |

Restricción tuya, firme: **ATAS da L2 real (footprint, DOM, Smart Tape) para UN símbolo**. TradingView igual: un gráfico. No hay más cobertura.

**Conclusión:** no existe un predictor posible con estos datos. Lo que sí existe es un **lector disciplinado**. El rediseño convierte el motor de "generador de señales" a "lector de decisión".

Lo que se conserva es lo que funcionó. Lo que se elimina es lo que no.

---

## 2. Qué se elimina (y por qué)

| Se elimina | Motivo medido |
|---|---|
| Generación de señales (volume, cvd, rsi, macd, stochastic, volatility) | 0 de 29 familias con edge. Ya están en `enabled: false`. |
| Filtro ML | AUC 0,552 vs 0,510 permutado = ruido. Ya desactivado. |
| `orderflow_calls.py` (ruta de llamadas) | −0,4196R neto, IC 95% excluye cero. Monitor ya está parado. |
| Puertas de expectativa como entrada de decisión | Con el pool negativo no hay nada que "des-silenciar". Un alert bajo **no** significa que la puerta funcione. |
| Backtester `orderflow_backtester.py` | Verifica reglas sobre datosTickPb; se rehace en la Fase 5. |

---

## 3. Arquitectura nueva

```
   ATAS L2 (1 símbolo) ─┐
   TradingView (estructura) ─┤
   Binance (precio/taker/ libro) ─┼──►  LECTOR ÚNICO  ──►  VEREDICTO
   Macro / breadth ─────┘         │      (live-only)        (reglas 6/7)
                                   │
                                   └──►  registro de evidencia
```

El motor pasa a ser **una sola función**: `leer() -> veredicto`. Sin generación,
sin gates que“自调”, sin auto-confianza.

---

## 4. Fases, con entregable y criterio de aceptación

### Fase 1 — Selector de símbolo ATAS
**Problema actual:** el gráfico abierto es XRP por casualidad, no por decisión. Con cobertura = 1 símbolo, elegirlo ES el diseño.

- **Entregable:** `atas-bridge/atas_target.json` (`{symbol, reason, set_at}`) + `atas-bridge/set_atas_target.py`
- El script escribe el objetivo y lee el log de ATAS para detectar el gráfico real.
- `/health` añade `atas_chart_symbol`, `atas_target`, `atas_target_match`.
- **Aceptación:** cambiar el objetivo a SOL y ver el aviso coincide en ≤ 2 min.

### Fase 2 — Lector multi-fuente
- **Entregable:** `reader/live_reader.py` — un solo punto de entrada que:
  1. Lee las 4 fuentes (ATAS, TV, Binance, macro).
  2. Aplica live-only: descarta cualquier fuente con edad > 300 s.
  3. Si falta una fuente, lo dice explícitamente en el veredicto. Nunca inventa.
- Reutiliza `tradingview-bridge/tv_live.mjs` (ya existe, salida exacta).
- **Aceptación:** un `leer()` devuelve JSON con las 4 fuentes y su edad; con el puente caído devuelve `null` en esa fuente y lo declara.

### Fase 3 — Motor de veredictos (reglas.json como salida principal)
- **Entregable:** `reader/verdict.py`
  - Evalúa Reglas 6 y 7 primero (son la puerta).
  - ~~Scorecard 0–100 separado de la puerta~~ — **retirado 2026-10-13**: medido sin
    poder predictivo (pearson +0,0175; el tercil alto perdía más que el bajo). El sesgo
    sale únicamente de la puerta.
  - Sesgo = puerta. Si la puerta no dispara → NEUTRAL.
  - `probability: unvalidated` siempre que no exista hit rate validado.
  - Tabla de footprint con `bid_pct` calculado (regla 6 cuenta >70%, regla 7 <30%).
- **Aceptación:** reproduce el veredicto de las 21:48 (Rule 6 = MET) desde el log, sin intervención manual.

### Fase 4 — Notificación por cambio de estado
- Telegram entrega el **veredicto**, no señales crudas.
- Solo notifica cuando el sesgo **cambia** (NEUTRAL→BEARISH etc.). Esto elimina el spam que fue el defecto original.
- **Aceptación:** 24 h de operación sin más de 2–3 mensajes, y cada uno con motivo.

### Fase 5 — Registro de evidencia (el camino a validar en el futuro)
- **Entregable:** tabla `verdicts` en SQLite: `ts, symbol, score, sesgo, r6, r7, ema, imbalance, footprint_buy, footprint_sell, tape_buy_pct, volume_ratio, forward_return_12`.
- Se escribe en **cada** veredicto, incluso NEUTRAL. Sin esto no habrá dataset nunca.
- **Objetivo:** n = 5.000 veredictos (~4–5 meses) antes de volver a preguntar por edge.
- **Aceptación:** `audit_verdicts.py` calcula expectancy direccional sobre los veredictos y aplica Bonferroni.

### Fase 6 — Limpieza — HECHA

- Borrado: `orderflow_calls.py`, `orderflow_calls_report.py`, `plan_gate.py`,
  `monitor_supervisor.bat`, `orderflow-calls.ps1`,
  `quant_engine/orderflow_backtester.py`. Los seis eran la ruta perdedora
  (−0,4196R neto, IC 95% excluía cero). `monitor_supervisor.bat` era el
  peligroso: un bucle de reinicio podía resucitar esa ruta.
- **NO se borró `signal_engine.py`**: sigue vivo. `bridge_server.py` lo usa en
  `_build_fast_analysis()`, que alimenta el comando de chat `/analizar` de
  Telegram. No es fuente de spam — es a demanda, no automático.
- Verificado: `bridge_server.py` parsea, cero referencias colgantes a los seis
  borrados, y tras reiniciar el puente `/health` responde `ok` sin errores de
  import.
- **Aceptación:** `grep -ri "create_order\|set_leverage"` no devuelve nada en
  la ruta de decisión — verificado antes y sigue siendo cierto.

---

## 5. Lo que este rediseño NO entrega

Debo decirlo claro: **no entrega un bot que gana dinero.** Entrega:

- ✅ Un lector disciplinado que dice “neutral” cuando la evidencia no alcanza, y por qué.
- ✅ Un único símbolo con L2 real, elegido a propósito.
- ✅ Un dataset que en 4–5 meses puede responder si existe edge.
- ❌ No hay edge hoy. Medido, no supuesto.

La honestidad de “no puedo recomendar entrada” **es** el producto. Un motor que dice “compra” sin evidencia es peor que uno que calla.

---

## 6. Orden de ejecución

1. Fase 1 (selector) — 1 h, quita la aleatoriedad del símbolo.
2. Fase 3 (veredictos) — 2 h, el núcleo.
3. Fase 2 (lector) — 1 h, une todo.
4. Fase 4 (notificar) — 1 h, quita spam.
5. Fase 5 (registro) — 30 min, empieza a contar desde el primer minuto.
6. Fase 6 (limpieza) — 1 h, al final.

Empieza por Fase 5 aunque esté en último lugar: **si no empiezas a registrar desde hoy, en 4 meses repetitionás esta misma auditoría sin datos nuevos.**