# Quant Alert Engine — AISLADO (2026-10-11)

Este motor ya no esta ligado a nada en `trading-engines/`. Es una app de
escritorio que arranca cuando el usuario la abre, y punto.

## Estado

- **Arranque automatico:** eliminado. El acceso directo de Startup se renombro a
  `_disabled_QuantAlertEngine.lnk` (reversible: borralo y vuelve).
- **Puertos que expone cuando corre:** `:8081` (health/diagnostico).
- **Procesos que lo lanzan al encender el PC:** ninguno. No hay scheduled task,
  no hay entrada en Startup activa, no lo invoca ningun supervisor hermano.

## Que se Desacoplo (2026-10-11)

Cuatro cosas dependian de esta carpeta. Todas resueltas:

| Dependencia | Como estaba | Ahora |
|---|---|---|
| `scanner-engine` | usaba `quant-alert-engine/venv/Scripts/python.exe` como interprete | **Python312 del sistema.** Verificado que el scanner solo necesita stdlib + `certifi` (opcional, con fallback en `http_io.py`) |
| `reader/verdict_notify.py` | leia las credenciales de Telegram de `quant-alert-engine/.env` | **lee `atas-bridge/.env`** (`TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`), que ya existe. Verificado: token y chat_id presentes |
| `reader/live_reader.py` | `src_macro()` consultaba `:8081/health` para el bloque macro | **CoinGecko REST directo.** QAE era la unica via del pipeline macro, yCoinGecko es la pieza que si importa |
| `quant-crypto-analysis/SKILL.md` | fuentes 10/11/12/14 del analisis dependian del pipeline macro de QAE | **Polymarket y CoinGecko por REST directo.** Fuente 6 del formato (multi-TF consensus) eliminada |

### Nota sobre `scanner-engine`

El riesgo real de borrar esta carpeta era el scanner: **esta vivo y sano**
en `:8090`, 8 pares x 4 venues (Binance/OKX/Kraken/Bybit), ~3.352 filas
grabadas y creciendo. Depender del venv de QAE lo ataba. Ahora corre con
Python312 y su `/health` responde identico — verificado tras reiniciarlo.

Si borras esta carpeta, el scanner **sigue funcionando**. Ese era el punto.

### Nota sobre Polymarket

`reader/polymarket_direct.py` reproduce la lectura sin heredar nada de aqui.
Un hallazgo que cambia el metodo: `/public-search?q=XRP` devuelve mercados
**ya cerrados de meses atras** (probado el 2026-10-11: 10 eventos, todos
`closed=true`, `volume24hr=None`). Es un indice de volumen historico, no un
mercado vivo. La ruta correcta es:

```
GET gamma-api.polymarket.com/events?closed=false&active=true&tag_slug=<tag>
    &order=volume24hr&ascending=false
```

Uso: `python reader\polymarket_direct.py XRP`

## Como abrirla a mano

```powershell
cd C:\Users\Lenovo\trading-engines\quant-alert-engine
.\start-qae.ps1
curl.exe -s http://127.0.0.1:8081/health
```

## Como pararla

```powershell
.\stop-qae.ps1                    # UI + supervisor
.\stop-qae.ps1 -IncludeHeadless   # ademas el engine headless
```

El orden importa: primero muere el supervisor, o resucita la UI a los 3 s.

## Advertencia sobre su propio estado interno

`GET /calls` en el puente devuelve **404**. Este motor sigue intentando
POSTear ahi (`orderflow_engine.py:328`) y cada intento falla en silencio con
`! {sym} call log failed`. Sus alertas al puente (`POST /quant-alert`) si
funcionan, pero **no pasan por `verdict.py` ni por `rules.json`**.

Si abres QAE esperando que sus señales respeten las Reglas 6/7/2, eso no
existe. QAE tiene su propio motor de reglas, independiente.