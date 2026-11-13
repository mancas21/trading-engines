# LiveExporter + Live Order Flow — indicadores C# para ATAS X

Dos indicadores en **un solo DLL** (`AtasExporter.dll`) que viven en
`%APPDATA%\ATAS X\Indicators\`:

| Indicador | Qué hace | Escribe |
|---|---|---|
| **LiveExporter** | Exporta la vela activa a JSON cada ~300 ms y empuja alertas BigTrade / murallas DOM al puente | Sí — ficheros + HTTP |
| **Live Order Flow** | Dibuja el área de valor, el POC y el delta sobre el gráfico de precio | No — es solo vista |

El `LiveExporter` es la fuente de datos del `bridge_server.py` (FastAPI, `:8000`).
El `Live Order Flow` es puramente visual: no escribe ficheros, no habla con la
red y no toca el feed del exportador.

---

## 1. Construir

```powershell
cd C:\Users\Lenovo\trading-engines\atas-bridge\atas-exporter
dotnet build -c Release --nologo
```

Sale en `bin\Release\net10.0-windows\AtasExporter.dll`. **Debe dar 0 errores y 0
avisos** — el proyecto tiene `Nullable=enable` y el compilador trata los avisos
como errores en la práctica.

El proyecto referencia las DLL de la instalación de ATAS X
(`C:\Program Files\ATAS X\`) con `<Private>False</Private>`: ATAS ya las tiene
cargadas en memoria y no debe llevar copias propias. Si actualizas ATAS X,
**reconstruye y reinstala** — las referencias apuntan a la versión instalada.

## 2. Instalar

```powershell
Copy-Item bin\Release\net10.0-windows\AtasExporter.dll "$env:APPDATA\ATAS X\Indicators\AtasExporter.dll" -Force
```

- **ATAS X recarga solo**: detecta el cambio en el fichero y recarga la carpeta
  de indicadores (verás `Changed library '…'` en el log de ATAS). No hace falta
  reiniciar.
- **ATAS clásico** (no X) usa `%APPDATA%\ATAS\Indicators\` y sí necesita el
  botón de recarga de la barra de estado.
- Alternativa: botón **Add custom indicator** (abajo a la izquierda en la
  ventana de indicadores) — copia el DLL y lo carga al momento.

Los indicadores aparecen en el grupo **Custom** de la ventana de indicadores,
con los nombres `LiveExporter` y `Live Order Flow`.

## 3. Verificar que funciona

```powershell
# El fichero del símbolo que esté chartado debe avanzar cada ~300 ms
Get-Content C:\Users\Lenovo\trading-engines\atas-bridge\_live_indicators_1000PEPEUSDT.json -Raw | ConvertFrom-Json | Select-Object schema_version, ticker, bar_index
```

Si `schema_version` es `2` y el `timestamp` avanza, el exportador vivo es la
versión nueva. El log del exportador está en
`atas-bridge\_exporter_debug.log`.

> **Un símbolo a la vez.** El exportador escribe un fichero por instrumento
> chartado, pero solo el que está en pantalla produce datos frescos. El puente
> aplica su propia puerta de "solo live" sobre esto.

---

## 4. Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `ATAS_BRIDGE_DIR` | `C:\Users\Lenovo\trading-engines\atas-bridge` | Dónde escribe el exportador. El puente lee el mismo sitio. |
| `ATAS_LIVE_MAX_AGE_SECONDS` | `300` | Antigüedad máxima para considerar un snapshot "live" (lo usa el puente). |

---

## 5. Esquema del JSON (schema_version 2)

Fichero: `_live_indicators_<SÍMBOLO>.json`. Todo es **aditivo**: ninguna clave
existente cambió de significado y ninguna se eliminó. Un lector de schema 1
sigue funcionando.

```jsonc
{
  "schema_version": 2,
  "timestamp": 1794492220544,          // epoch ms del tick que originó el snapshot
  "ticker": "1000PEPEUSDT",
  "bar_index": 1032,                   // barra viva (la última, en formación)

  "price":  { "last":…, "open":…, "high":…, "low":… },
  "volume": { "total":…, "ask":…, "bid":…,       // ask = volumen negociado en el ask
              "prev_bar_volume":…, "avg_volume_20":…, "volume_ratio":…, "lookback_bars": 20,
              "delta":… },
  "delta": 16214197.0,                 // delta de la barra viva
  "time": "14:00:00",

  // ── schema 2 ──
  "open_interest": 20882761300.0,
  "vwap": 0.00437803425614996,
  "max_delta": 46463412.0,
  "min_delta": -2525850.0,
  "max_volume_level": { "price":…, "volume":… },
  "max_tick_level":   { "price":…, "ticks":… },

  // Footprint de la barra VIVA (la que se está formando)
  "footprint": {
    "poc":…, "value_area_high":…, "value_area_low":…,
    "top_levels": [ { "price":…, "volume":…, "bid":…, "ask":…, "delta":…,
                      "ticks":…, "imbalance_ratio":…, "stacked_buy":…, "stacked_sell":… } ],
    "all_level_count": 18,
    "imbalance_ratio": -5.93            // % del conjunto de la barra
  },

  // Footprint de las últimas BARRAS CERRADAS (antes esto no existía:
  // el puente solo veía la barra en formación)
  "closed_bars": [
    { "bar_index":1031, "time":"13:55:00", "volume":…, "delta":…,
      "poc":…, "value_area_high":…, "value_area_low":…,
      "level_count":15, "imbalance_pct":-5.306,
      "stacked_buy_levels":0, "stacked_sell_levels":0,
      "levels": null }                  // solo si "Publish closed-bar levels" está ON
  ],

  // Área de valor de la SESIÓN, calculada dentro de ATAS a partir de la unión
  // de las barras cerradas
  "session_value_area": { "poc":…, "value_area_high":…, "value_area_low":…,
                          "bars_used":20, "total_volume":…,
                          "covered_pct":70.6, "target_pct":70, "levels":151 },

  // Smart DOM
  "dom": { "best_bid":…, "best_ask":…, "spread":…, "cumulative_bid_vol":…,
           "cumulative_ask_vol":…, "imbalance_pct":…, "bid_ask_ratio":…,
           "bid_stack":[…], "ask_stack":[…] },

  // Smart Tape
  "tape": {
    "trades": [ { "p":…, "q":…, "s":"buy"|"sell" } ],   // los últimos 30 prints
    "buy_vol":…, "sell_vol":…, "net_vol":…,              // VENTANA de 30 prints
    "buy_count":…, "sell_count":…, "buy_count_pct":…, "sell_count_pct":…,
    "total_vol":…, "avg_trade_size":…,
    "largest_trade":…, "largest_trade_side":…, "largest_trade_price":…,
    "prints_sampled": 30,
    "sample_window": "last_30_trades_of_current_bar",

    // ── schema 2: TOTALES REALES DE LA BARRA ──
    "bar_buy_vol":…, "bar_sell_vol":…, "bar_net_vol":…,
    "bar_trade_count":…, "bar_buy_count":…, "bar_sell_count":…
  }
}
```

### ⚠️ La ventana del tape miente sobre la dirección

`tape.buy_vol` / `net_vol` / `buy_count_pct` describen **los últimos 30 prints**
de la barra, no la barra. Medido en 1000PEPEUSDT el 2026-11-12:

| | Ventana (30 prints) | Barra real |
|---|---|---|
| Compras | 1.152.426 | 3.934.206 |
| Ventas | 36.491 | 10.537.647 |
| Neto | **+1.115.935 (compra)** | **−6.603.441 (venta)** |

En otro momento la ventana llegó a marcar 30 ventas seguidas (`buy_count: 0`)
mientras la barra iba 3.792 compras / 3.549 ventas.

**Para la dirección, lee siempre `tape.bar_*`.** Los campos viejos conservan su
significado a propósito: las filas viejas del track record tienen que seguir
siendo comparables con las nuevas.

### Tamaño del snapshot

El fichero se reescribe entero cada ~300 ms. Por eso `closed_bars` va **en
resumen**: los 20 Levels por barra eran 119 KB y el snapshot entero 223 KB
(≈750 KB/s de escrituras, y el puente lo parsea a 5 Hz). Con el resumen son
**~15 KB**. El detalle por nivel queda a un ajuste de distancia
(`Publish closed-bar levels`), y solo cambia una vez por cierre de barra.

---

## 6. Ajustes del LiveExporter

| Grupo | Ajuste | Por defecto |
|---|---|---|
| Alerts | Enable bridge alerts | ON |
| | Bridge URL | `http://127.0.0.1:8000` |
| | BigTrade min lots | 2000 |
| | DOM wall min lots | 40000 |
| | Alert cooldown (s) | 300 |
| Display | Show price line on chart | OFF |
| Export | Publish DOM snapshot | ON |
| | Publish tape snapshot | ON |
| | Publish closed-bar footprint | ON |
| | Closed bars to publish | 20 |
| | Publish closed-bar levels | OFF |
| | Stacked imbalance levels | 3 |
| | Stacked imbalance ratio | 2.0 |
| | Session value area % | 70 |

**Show price line on chart** está OFF a propósito: el exportador es un feed de
datos, no un visual. Con ON pinta la línea de cierres en el gráfico.

## 7. Ajustes del Live Order Flow

Todo en el gráfico de precio: banda de área de valor, línea de POC, etiquetas
de delta por barra, marcadores de imbalance apilado en la barra viva y un
cuadro de información. Todo se puede apagar por separado; los colores y
grosores son ajustables.

---

## 8. Notas del plataforma

Las trampas de la API de ATAS (índices que no lanzan, `OnCalculate` que
reproduce la historia, `RecalculateValues` que borra las series, reglas de
carga) están en **[ATAS_NOTES.md](ATAS_NOTES.md)**. Léelo antes de tocar el
código.
