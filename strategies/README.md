# Estrategias para el harness de backtesting

Dos estrategias nacidas de la lógica de esta sesión (capitulación funding/OI +
estructura SMC), adaptadas al mandato del harness: **cross-sectional,
market-neutral, hipótesis explícita, parámetros pre-registrados**.

Formato: self-contained, mismo convenio de carga que `mp18_v2.py` (data de
`Documents\backtest-system\data\`). Para correrlas en tu pipeline: copia el
`.py` a `backtest-system\code\` y ejecuta con tu agente — las gates
G1-G11, IS/OOS 70/30 y block bootstrap se aplican en tu lado.

## Smoke full-sample (2026-11-15, PRE-registrado, NO es el veredicto)

| Estrategia | Total | Sharpe | MaxDD | Lectura honesta |
|---|---|---|---|---|
| `capfloor_rotation_v1.py` | **−95.1%** | −1.15 | −95.1% | **Muerta tal como está** — confirma §5.7: el factor funding-extremo ES momentum en este histórico (long knives siguen cayendo). Hipótesis v2 obvia pero NO probada aquí: dirección invertida (long funding positivo / short negativo) pre-registrada para TU harness con IS/OOS. No la ejecutes sin ese pase. |
| `smc_structure_rotation_v1.py` | +2889% | 0.98 | −87.6% | Headline fuerte pero DD monstruoso — las gates de concentración/DD de tu harness deciden. Es momentum condicionado a estructura fresca (CHOCH/BOS) con gate ROC de MP-18. |

**Regla de oro (§5.7):** el resultado de arriba es full-sample sobre los datos
que las estrategias nunca debieron ver para tuneo. El veredicto lo da tu
harness con IS/OOS — si OOS no sostiene, se archiva.

## VEREDICTO FORMAL DEL HARNESS (2026-11-15, run_id verificados bit-for-bit)

| Estrategia | run_id | FULL | IS | OOS | Veredicto |
|---|---|---|---|---|---|
| capfloor | `9f9082d68b284a2f` | −95.1% / PF 0.85 | −88.3% (PF 0.87) | −52.9% (PF 1.15, Sharpe −2.24) | **ARCHIVADA** — muerta en ambas ventanas; confirma §5.7 en datos formales |
| smcrot | `21804cd69bf72555` | +2889% / PF 1.00 | **+12,054%** (Sharpe 1.42) | **−78.7%** (Sharpe −0.82) | **ARCHIVADA** — la firma clásica de overfit: IS explota, OOS colapsa, PF full = 1.00 (sin edge), DD −87.6% |

Registradas en `vrun.py` como `--strategy capfloor` y `--strategy smcrot`
(adaptador `code/strat151_ports.py`). Cualquier iteración futura (v2 de
capfloor invertida, variaciones de decay del smcrot) es una hipótesis NUEVA
que pasa de nuevo por este loop: pre-registrar → run → verify → gates.

## Hipótesis

- **capfloor**: posicionamiento extremo revierte relativo — long shorts
  apiñados (funding muy negativo) vs short longs apiñados (funding positivo),
  SOLO con dispersión de panel alta (filtro de régimen anti-§5.7). Falló en
  full-sample; su inversa es la v2.
- **smc_structure**: un CHOCH/BOS recién confirmado marca persistencia de
  régimen de swing — el recién girado bull rinde relativo mejor que el recién
  girado bear en las siguientes 48 barras, con gate de momento absoluto MP-18.
