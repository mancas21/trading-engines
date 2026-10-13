# _archive — scripts de exploración one-off

Movidos aquí el 2026-10-11 (plan post-auditoría). Son los `_*.py` con los que
se exploró la API de ATAS y se depuró el exportador durante la construcción del
puente. NO forman parte del runtime: `bridge_server.py` solo importa
`_shared_read.py`, que sigue en la raíz. `_emit_alert.py` referencia el
`bias_score` que signal_engine ya no publica — es un fósil de la era de scoring.

Se conservan como registro de cómo se midió cada endpoint, no como código vivo.
