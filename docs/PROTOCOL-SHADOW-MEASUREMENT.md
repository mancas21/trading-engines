# PROTOCOLO PRE-REGISTRADO — Medición shadow del gate (Reglas 6/7) y de la capa watch

**Fecha de pre-registro:** 2026-11-12 (commiteado ANTES de mirar cualquier
retorno futuro — requisito del auditor, §5 del resultado de auditoría)
**Origen:** resultado de auditoría `AUDIT-GATE-SIGNAL-ENGINE-RESULTADO.md`
(§5, ítems 1-7). Este fichero ES el pre-registro. Cualquier cambio posterior
a este documento requiere commit separado con motivo.
**Estado al pre-registrar:** regla 7 hard-no-validada, regla 6 disabled,
0 BULLISH/BEARISH vivos bajo validación; la (c) vigente SIN medición de
transiciones/día completa (D2 midió: BTC r6_c 42/día supera la barra N=10;
JUP 0.5/día la cumple — veredicto mixto por par).

---

## 1. Prerrequisito — cobertura del grabador ≥ 90%

El gate solo puede medirse donde el grabador corre (D3: `verdict_recorder.py`
es el ÚNICO escritor de `dom_gate_state.json` y `condition_window_state.json`).
Prerrequisito de todo lo demás:

- Heartbeat: el supervisor registra cada ciclo si el grabador tickó
  (`recorder_tail` ya existe en supervisor_status.json). Alerta si 3 ciclos
  seguidos sin tick con ATAS abierto.
- Cobertura objetivo: ≥ 90% de los ticks de 30s con el símbolo objetivo vivo.
- D1 midió: la cobertura es por símbolo-objetivo (XRP sin tick desde el
  26-oct al dejar de ser target). La medición shadow de un par exige que ese
  par SEA el objetivo o que el grabador multi-par esté activo (decisión
  pendiente, fuera de este protocolo).

## 2. Shadow continuo — registrar valores, no solo estados

En cada tick del grabador, añadir al ledger (columnas nuevas, sin romper el
esquema actual):

- z por venue (los crudos `dom_vac_raw` ya existen — añadir el z por venue),
- nº de votos por lado,
- racha del gate,
- streak up/down de CVD y delta de la barra en curso,
- `buy_h`/`sell_h` tres veces: con niveles thin, sin thin, y con volumen
  mínimo por nivel (umbral a definir en el diseño, pre-registrado aquí como
  "≥ 1000 contratos o el p25 del volumen por nivel de la sesión"),
- precio y ts.

Retornos futuros (15m/1h/4h) calculados OFFLINE (backfill), netos de ~11 bps.
Nunca en el tick.

## 3. Atribución por condición

Tres variantes pre-registradas del gate, medidas sobre el MISMO shadow:

- **T0** = (a) ∧ (b)  — sin consenso multi-venue
- **T1** = T0 ∧ (c)   — el gate vigente
- **T2** = (c) sola   — consenso sin flujo

Para cada variante y horizonte: placebo (signo aleatorio estratificado por
día), n_eff por episodio (dedupe por gap ≥ 1h), bootstrap por bloques de día
(1,000 remuestreos, IC95), walk-forward (mitad inicial calibra, mitad final
evalúa). Criterio de promoción: batir el placebo out-of-sample con
significancia Y estabilidad entre mitades. Criterio de retiro: ver §6.

## 4. Rediseño de (c) evaluado SIN retornos primero

Métricas de calidad de la señal (nunca retornos en esta fase):

- Calibración móvil por (venue, par) vs la agrupada vigente (D4),
- imbalance agregado en profundidad por bps del mid (no solo top-5),
- Kraken sin voto (medido: 42% de muestras por encima de +0.25 con std 0.499).

Barras de aceptación pre-registradas (del propio rules.json): churn/día ≤ 10
transiciones MET↔no-MET (D2 midió BTC r6_c a 42/día — la (c) vigente FALLA
esta barra en BTC), persistencia ≥ 2 lecturas, acuerdo entre venues ≥ 60% de
ciclos sin SPLIT. Solo si una variante pasa todas las barras de calidad se le
corren retornos, una vez, en held-out.

## 5. Presupuesto de ruido de publicación

- Tope duro: ≤ 1 notificación por símbolo-día (el notificador ya lo cumple por
  CAMBIO DE SESGO — se mantiene).
- Cualquier gradiente de confianza o tier informativo es SOLO informativo
  hasta pasar el protocolo §3 y calibrarse con curva de fiabilidad antes de
  mostrarse (lección del scorecard 0-100: mostrar un gradiente sin calibrar
  fue exactamente su defecto).

## 6. Regla de parada pre-registrada

Si tras N eventos efectivos (contados por EPISODIO, no por fila) el shadow no
muestra relación monótona entre profundidad de evidencia y retorno, la regla
se retira (mismo estándar que retiró las reglas 1/2/4 del scanner y el
scorecard). N inicial: 30 episodios independientes para el gate, 50 para la
capa watch.

## 7. Capa watch (watch_early v3)

- Estado: HIPÓTESIS, no edge (auditoría §2.1: 56.2% n=32 depende de 2
  regímenes, p=0.21, sin meseta de umbral, 0 banderas 2024/2026).
- Test pre-registrado del mecanismo (funding extremo → retorno forward):
  sección cruzada de ≥ 50 perps de Binance con datos públicos de funding,
  inferencia agrupada por fecha y por episodio, control de régimen (drawdown
  y volatilidad de BTC), reporte SIEMPRE con y sin las 4 mayores banderas y
  sin el par dominante (SOL).
- Cada bandera v3 en vivo se registra con su resultado
  (`watch_flags.jsonl` + `--measure`), y se reporta con y sin las 4 mayores.
- Prohibido ajustar umbrales de `watch_early` antes de este test (§6).

---

**Firma del pre-registro:** este fichero se commitea antes de ejecutar
cualquier medición de retornos derivada de él. El primer commit que lo
incluye es la marca temporal.


---

## RESULTADO (2026-11-13) — test §7 ejecutado: NO VALIDADO

60 perps por volumen, n=846 banderas LONG funding <= -0.001, 355 episodios.
Criterios: (1) PASS 355>=50 | (2) FAIL med -0.052% | (3) FAIL hit 49.5% |
(4) FAIL IC95 [-0.516,+0.322] | (5) gradiente no evaluable (todas las
banderas >10x por diseño del test) | (6) FAIL robustez (sin top-4 -0.067;
sin par dominante +0.032) | (7) FAIL régimen INVERTIDO: deep-dd -0.225% vs
shallow-dd +0.191%. Acción: capa watch RETIRADA (§6). Top-8 flags =
micro-caps con recorridos no operables (ARIA +667% 24h). Detalle: PLAN.md
§10.3.
