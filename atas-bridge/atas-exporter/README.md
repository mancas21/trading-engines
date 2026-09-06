# LiveExporter — Indicador C# para ATAS X

## ¿Qué hace?

Exporta datos **en vivo** de la vela actual desde ATAS X a un archivo JSON cada 300ms, permitiendo que el bridge (`bridge_server.py`) consuma Order Flow en tiempo real sin depender del webhook de alerts.

## Diferencia clave vs Webhook

| Webhook de ATAS | LiveExporter (este indicador) |
|---|---|
| Solo manda BigTrades y DOM alerts (>X lots) | Envía toda la vela: OHLC, volumen, delta |
| Entra en backoff si el bridge no responde | Escribe a archivo local — sin dependencia de red |
| Latencia variable (según umbral de lots) | Streaming continuo cada 300ms |
| Reactivo (solo cuando ocurre el evento) | Proactivo (siempre tiene el estado actual) |

## Archivos

- **Fuente**: `LiveExporter.cs`
- **Proyecto**: `AtasExporter.csproj` (apunta a DLLs de ATAS X en `C:\Program Files\ATAS X\`)
- **DLL compilado**: `bin\Release\net10.0\AtasExporter.dll`
- **Salida**: `C:\atas-bridge\_live_indicators.json`
- **Endpoint bridge**: `GET /indicators`

## Instalación en ATAS X

1. Copia `AtasExporter.dll` a la carpeta de indicadores:
   ```
   C:\Users\Lenovo\Documents\ATAS\Indicators\
   ```
2. Reinicia ATAS X
3. Agrega el indicador: *Indicators → Add → Export → LiveExporter*
4. El indicador comienza a escribir a `_live_indicators.json` automáticamente

## Datos que exporta

```json
{
  "timestamp": 1710000000000,
  "ticker": "SOLUSDT",
  "price": {
    "last": 75.88,
    "open": 75.91,
    "high": 76.00,
    "low": 75.88
  },
  "volume": {
    "total": 74776,
    "ask": 38000,
    "bid": 36776
  },
  "delta": -1224,
  "time": "14:30:00"
}
```

## Cómo lo usa el bridge

El endpoint `GET /indicators` en `bridge_server.py`:
```python
@app.get("/indicators")
async def get_indicators():
    try:
        with open(r"C:\atas-bridge\_live_indicators.json") as f:
            data = json.load(f)
        return data
    except FileNotFoundError:
        return {"error": "No data from LiveExporter yet"}
```

## Notas

- El throttle de 300ms evita escritura excesiva en disco
- Si ATAS X no está corriendo, el archivo no se actualiza (el bridge responde con error)
- El indicador se ejecuta sobre la vela activa — los valores cambian en cada tick
