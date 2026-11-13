# ARCHIVADO — quant-alert-engine (2026-10-11)

Este motor está CONGELADO en modo manual. No se desarrolla, no se despliega, no
se arranca como servicio. La auditoría de 2026-10-11 lo encontró duplicando las
features y la contabilidad de expectancy que `scanner-engine/` ya hace mejor, y
su scorecard de pesos v3.4.1 está medido como anti-predictivo (spread −4.0 sobre
1,205 registros).

## Qué sigue vivo aquí

- **`ml_train.py`** — la plantilla de meta-labeling con la disciplina correcta
  (walk-forward temporal con purge+embargo por ventana de label, umbrales
  elegidos solo en train, métricas netas de costo a 12bps). Es el código que se
  reutilizará cuando el ledger de `scanner-engine` acumule la muestra para un
  filtro secundario.
- Los datos históricos en `data/` (no borrar: son la pista de auditoría del
  scorecard v3.4.1 y de los 1,205 registros citados arriba).
- `config.json` y credenciales de solo lectura si algún día se necesita el
  conector ccxt.

## Qué está muerto

- El bucle de alertas (`quant_alert_engine.py` como servicio), el UI, y el
  supervisor `qae_supervisor.py` — no arrancarlos.
- `strategy_backtester.py` sigue usable como herramienta CLI manual (con el fix
  de gap-through en stops), pero no es parte de ningún pipeline.
- Todo lo que refiera al subsistema `/calls` (borrado en el bridge el
  2026-10-07): expectancy_r de esa era es de una ruta perdedora medida (winrate
  36%, PF 0.398) y se conserva solo como evidencia en STATUS.md y git history.

## Dónde vive el trabajo activo

`scanner-engine/` (scanner multi-venue + ledger de expectancy + auditor) y
`atas-bridge/` + `reader/` (la pata ATAS L2). Ver `../PLAN.md` para el orden.
