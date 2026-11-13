# Enlace: trading-engines ↔ backtest-system

**Fecha del enlace:** 2026-10-20
**Capa LIVE:** `C:\Users\Lenovo\trading-engines` (scanner, reader, gate, alertas, ATAS/TV bridges)
**Capa VALIDACIÓN:** `C:\Users\Lenovo\Documents\backtest-system` (harness `vrun.py`, artefactos con `run_id`, kill list)

---

## 1. Por qué existe este enlace

`trading-engines` decide **en vivo** (qué par, qué dirección, qué nivel). `backtest-system`
decide **si una estrategia merece existir**, con una regla dura: *ningún número sin artefacto*.

Hasta ahora eran dos repos aislados: el engine podía señalar un par cuya estrategia nunca
se validó, y el backtest-system validaba estrategias que el engine nunca miraba. Este
enlace hace que **el engine sepa qué pares tienen una estrategia que pasa los gates del
repo**, con su `run_id` a la vista.

## 2. Qué se enlaza (y qué no)

| | |
|---|---|
| **Dirección** | engine → backtest-system (**solo LECTURA**) |
| **Código** | `scanner-engine/backtest_link.py` (nuevo) |
| **Tests** | `scanner-engine/tests/test_backtest_link.py` (6 tests) |
| **Endpoints** | `GET /backtest` · `GET /backtest?pair=XRP%2FUSD` |
| **Health** | sección `backtest_link` en `GET /health` |
| **Datos** | NO se copian (1.3 GB). Se referencian por ruta. |
| **Escritura** | NUNCA en el backtest-system: `code/`, `data/`, `results/`, `backups/` son append-only de ese repo |

## 3. Los gates que se aplican (heredados del repo, no inventados)

De `backtest-system/AGENTS.md` → *"Validation criteria (not negotiable)"*:

- **PF ex-cost > 1.2**
- **n ≥ 30 trades cerrados**
- **net > 0**
- **costes del repo:** direccional 5 bps fee + 0.5 bps slip por lado → **11 bps roundtrip**
  (medido 2026-10-25 en libro vivo de Binance/OKX/Bybit/Kraken; el preset viejo
  6+5 = 22 bps queda solo como sensibilidad)
- **excess vs buy & hold** se reporta siempre (un PF > 1 que solo gana *perdiendo menos
  que un mercado que se hunde* no es alpha)

Un artefacto con **otra asuncion de costes** (14 bps, zero-cost control, o los modelos
propios de `cvrmd`/`sax`) **no admite candidatos**: se contabiliza en
`other_cost_artifacts` y no se mezcla. En el estado actual: **8 artefactos** con costes
del repo frente a **289** con otros costes.

## 4. Estado del enlace (2026-10-20, artefactos de 17-oct 20:21Z)

7 pares/símbolos evaluados con costes del repo. **Candidatos admitidos: tres.**

| Par | Estrategia | TF | PF | Net | Excess vs B&H | n | DD | run_id |
|---|---|---|---|---|---|---|---|---|
| XRP/USD | S10 Vol-Targeted TSM | 1h | 1.75 | +171.7% | +28.9% | 169 | −20.7% | `e383f966e0b38516` |
| XRP/USD | S02 Donchian + Flow Breakout | 1h | 1.47 | +143.1% | +0.3% | 228 | −28.9% | `e383f966e0b38516` |
| SOL/USD | S10 Vol-Targeted TSM | 1h | 1.56 | +66.6% | +100.3% | 173 | −22.1% | `b54c2bef8a8c4489` |

**Evaluados y sin candidato (ninguno pasa los gates):** BTCUSDT@1h, ETHUSDT@1h,
HYPEUSDT@1h, HYPEUSDT@15m, JUPUSDT@1h, SUIUSDT@1h.
**Sin cobertura en el backtest-system:** XAU, SPCX, NVDA.

**Note sobre el run_id:** el `AGENTS.md` del backtest-system cita `a30083333490d945`
(XRP) y `814c80675d570aae` (SOL). **Desde 2026-10-25 el gate exige 11 bps**: los
artefactos viejos a 14/22 bps caen en `other_cost_artifacts` y dejan de admitir
candidatos hasta re-correr el grid con el modelo nuevo. Esta tabla es un snapshot
historico del 2026-10-20 y no refleja el estado posterior al cambio de costes.

## 5. Cómo se usa

```powershell
# resumen por par
curl http://127.0.0.1:8090/backtest

# candidatos + run_id + métricas de un par
curl "http://127.0.0.1:8090/backtest?pair=XRP%2FUSD"

# sección del link dentro del health del engine
curl http://127.0.0.1:8090/health | findstr backtest_link

# el módulo standalone (no necesita el scanner)
python scanner-engine\backtest_link.py
```

## 6. Reglas de uso (heredadas, no negociables)

1. **Un número del backtest-system SIEMPRE viaja con su `run_id` y su ruta de artefacto.**
   Si no hay artefacto, no hay número: se declara la ausencia.
2. **No se reinterpreta ni se recalcula** una métrica del artefacto. Se copia.
3. **Un par sin candidato no es "débil"**: es "no medido". Se declara así.
4. **Los artefactos con otros costes no admiten candidatos** y no se mezclan.
5. El backtest-system es la **fuente de verdad sobre si una estrategia tiene edge**.
   El engine no puede promover una estrategia que el backtest-system haya matado
   (kill list en su `STATUS.md`).
6. **El engine no se modifica para "hacer que algo pase"** en el backtest-system. Si un
   par del engine no tiene candidato, se dice.

## 7. Límites conocidos

- El enlace es **de solo lectura y de una dirección**: el engine no escribe artefactos
  nuevos ni lanza corridas de `vrun.py`. Para eso hay que ir al backtest-system.
- `results/runs/` crece: 297 artefactos ahora. La lectura está cacheada 300 s
  (`cached_scan`) porque `/health` se llama a menudo.
- El puente **no aplica los gates de periodo/walk-forward** del repo (period split,
  neighbourhood, OOS): esos viven en `_cbpack_gates.py`. El enlace es una **c criba de
  admisión**, no una revalidación completa. Para revalidar, `verify`/`guard` del harness.
- Si el backtest-system se mueve, el enlace **degrada sin romper**: `root_exists=false`,
  `artifacts=0`, y el scanner sigue escaneando (test
  `test_missing_root_declares_absence_instead_of_raising`).

## 8. La capa rápida (2026-11-06): cortes de datos, `bt.py`, tiempos objetivo

El cuello de horas del pipeline nunca fue cómputo: la criba de cien
estrategias x dos costes corre en minutos. Las horas se iban en la capa de
validación, y el 2026-11-06 se restructuró (decisión: operar en ambos repos,
objetivo *idea → veredicto validado* en unos treinta minutos, motores y gates
intactos):

| | |
|---|---|
| **Cortes de datos (gen 2)** | Todo artefacto nuevo lleva `core.cut_ts` (UTC, por defecto el día de creación) y hashea solo los bytes ANTERIORES: prefijo byte-exacto del CSV de klines (`open_ts < cut_ts`), registros de funding con `t < cut_ts` (serialización canónica). El motor RECIBE ese mismo corte (directorio temporal, basename original). **Un append diario ya no stalea nada**: STALE significa edición DENTRO del corte, motor cambiado o params cambiados. Artefactos legacy (sin `cut_ts` en core) siguen la comparación de fichero completo — el museo no se migra. |
| **Reproducir una ventana antigua** | `python code/vrun.py run ... --cut-ts <ISO>` — para reproducir un artefacto legacy: cut = max(finn de inputs) + 1s (`code/_cut_regression_prompts100.py` lo hace y exige métricas idénticas). `--cut-ts full` = hashing legacy puntuual. |
| **`bt.py` (backtest-system/code)** | Pipeline por etapas sobre la maquinaria existente (ningún gate re-implementado): screen (gates baratos full+OOS) → walk-forward → neighborhood → scorecard → reporte + `guard --strict`. Cada etapa corre solo para supervivientes y para temprano si una etapa vacía. Planes en `results/bt_plans/`; timings en `results/bt_timings/` (nunca en el reporte: guard no los respalda). |
| **Plantilla de adaptador** | `code/adapter_template.py`: checklist completa (port → known answers ANTES de datos reales → registro → run → gates → reporte con guard) y control zero-cost medido dentro del artefacto. |
| **Paralelismo** | `matrix`/`neighborhood` aceptan `--workers N` (mismos artefactos y orden); `_cbpack_gates.py` lanza los ocho gate runs por celda en pool. `verify` de un artefacto sigue siendo secuencial (una corrida de motor = un trabajo); el paralelo por lotes es `pverify`. |
| **Criba (este repo)** | `backtest_prompts100/run_all.py`: señal construida UNA vez y puntuada a 11 y 22 bps; caché feather de CSVs (`data/_csv_cache`, clave nombre+mtime+tamaño; backtest-system sigue read-only); pool de procesos (`--workers`, por defecto secuencial; `--eq oos` recorta las curvas al tramo OOS, `--eq full` para `make_charts10.py`). Pantalla completa de doscientas corridas: unos treinta y nueve segundos (antes cinco minutos ocho). Indicadores vectorizados (aroon, percentrank, connors streak/percentrank) verificados bit-identical contra las versiones de bucle, incluida la regla NaN de pandas. |

**Tiempos medidos (2026-11-06):** criba completa 200 corridas ≈ 39 s
(antes 308 s); matriz cbpack 1h de trece celdas ≈ 13 s; campaña demo completa
(screen+WF+neighborhood+stats+reporte con guard) ≈ 10 s de cómputo de etapas.
Un candidato pesado sigue costando lo que cuesta SU motor — lo que ya no
existe es la re-corrida entera tras cada append de datos.
