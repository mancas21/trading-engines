# Notas de la plataforma ATAS X

Todo lo que hay que saber antes de tocar estos indicadores. Verificado contra
`ATAS.Indicators v8.0.16.678` (instalación del 12/11/2026).

---

## 1. Índices: unos fallan y otros no

Este es el fallo más caro de la API, porque uno de los dos **no se queja**.

| Operación | Índice inválido |
|---|---|
| `ValueDataSeries[i]` | **Silencioso.** Negativo → 0. Mayor que el último → **extiende la serie con ceros**. Ni excepción ni aviso: el gráfico muestra una línea plana por el suelo. |
| `GetCandle(i)` | **Lanza excepción.** |

Regla: usa siempre el `bar` que te pasa `OnCalculate`, y **guarda** el índice
antes de llamar a `GetCandle`:

```csharp
if (bar - k < 0) break;          // no confiar en la excepción
var c = GetCandle(bar - k);
```

En el exportador esto estaba en el cálculo del volumen medio a 20 barras: en
las primeras 20 barras de un chart la excepción saltaba y el contexto de
volumen salía `null` sin explicación. El `try/catch` lo tapaba; el bug era
silencioso.

## 2. `OnCalculate` se llama por toda la historia

`OnCalculate(bar, value)` se invoca **una vez por cada barra histórica** al
cargar el chart, y después en **cada tick** de la barra viva. Cuando llegan
ticks más rápido de lo que el indicador calcula, la plataforma **se salta**
llamadas de la misma barra (la última de cada barra sí se calcula siempre).

Consecuencias:

- Un indicador que escriba a disco en `OnCalculate` **replays la historia**.
  El LiveExporter lo evita con `if (bar != CurrentBar - 1) return;`.
- `CurrentBar` es el número total de barras; la última barra es
  `CurrentBar - 1`. `CurrentBar` crece cuando aparece una barra nueva.
- `value` es el valor de la **fuente** que elige el usuario (open/high/low/
  close/volume/HL2/HLC3/OHLC4/HLCC4). Por defecto es el cierre.

## 3. Un recálculo borra las series

Cuando cambia un ajuste y se llama a `RecalculateValues()`, la plataforma
**vacía las series** y vuelve a llamar a `OnCalculate` desde la barra 0. Tus
arrays propios no se vacían: si guardas caché por barra, límpialos tú en
`OnRecalculate` / `OnFinishRecalculate`.

## 4. Tipos que se confunden fácil

| Lo que escribes | Lo que es |
|---|---|
| `GetCandle(bar)` | **`ATAS.Indicators.IndicatorCandle`**, no `ATAS.Types.Candle` |
| `candle.GetAllPriceLevels()` | `IEnumerable<ATAS.Indicators.PriceVolumeInfo>` |
| `PriceVolumeInfo` | `Price`, `Volume`, `Bid`, `Ask`, `Ticks`. **No tiene `Delta`** → `delta = Ask - Bid` |
| `ValueArea` | **Solo** `ValueAreaHigh` y `ValueAreaLow`. **No tiene POC.** |
| `Indicator.ValueAreaPercent` | **Obsoleto** en esta build → `PlatformSettings.ValueAreaPercent`. El exportador usa su propio ajuste. |
| `ValueDataSeries.Color` | **`System.Drawing.Color`**, no el WPF `System.Windows.Media.Color` que dice la documentación clásica |

> La documentación de `docs.atas.net` que describe `System.Windows.Media.Color`
> es del ATAS clásico. En ATAS X el color es `System.Drawing.Color` y el
> proyecto **no** necesita `UseWPF`. `System.Drawing.Common` sí hace falta,
> porque `RenderStringFormat.Alignment` y `RenderPen` exponen tipos de ahí.

## 5. Dibujar en el gráfico

El patrón que usan los indicadores oficiales (`_indicator_examples\Technical\`):

```csharp
EnableCustomDrawing = true;
SubscribeToDrawingEvents(DrawingLayouts.Final);   // Flags: None|Historical|LatestBar|Final
…
protected override void OnRender(RenderContext context, DrawingLayouts layout)
{
    var x = ChartInfo.PriceChartContainer.GetXByBar(bar, false);
    var y = ChartInfo.PriceChartContainer.GetYByPrice(price, false);
    context.DrawString(text, font, color, x, y, format);
}
```

- `ChartInfo` es `IChart`. `GetXByBar` / `GetYByPrice` / `BarsWidth` /
  `FirstVisibleBarNumber` están en **`ChartInfo.PriceChartContainer`**
  (`IChartContainer`), no en `IChart` directamente.
- `ChartInfo.ChartContainer.Region` → `Rectangle` del chart.
- `ChartInfo.ColorsStore.AxisTextColor` → color de texto del tema.
- `Container` (el del panel del indicador) es `IIndicatorContainer`: tiene
  `RelativeRegion`, `Minimum`, `Maximum` y `GetYByValue(decimal)` — el mapa
  valor→y **de su propio panel**, no del gráfico de precio.
- `SubscribeToDrawingEvents` es `protected` en `ExtendedIndicator`.
- `RenderContext` es `OFT.Rendering.Context.RenderContext`;
  `RenderFont`, `RenderPen` y `RenderStringFormat` son de `OFT.Rendering.Tools`.

### Una serie, una escala

Todas las series de un panel comparten escala vertical. Un precio (POC, área de
valor) y un volumen (delta) **no pueden vivir en el mismo panel**. Por eso el
*Live Order Flow* dibuja con `OnRender` en vez de con series: así la geometría
de precios es exacta y el delta va como texto.

## 6. Cuándo se recarga el DLL

- **ATAS X** vigila `%APPDATA%\ATAS X\Indicators\` y recarga solo (~1 s de
  debounce). Log: `Changed library '…'` → `Libraries reloaded: N loaded, …`.
- **ATAS clásico** usa `%APPDATA%\ATAS\Indicators\` y necesita el botón de
  recarga de la barra de estado.
- Al recargar se **recrea** el indicador del chart, así que el código nuevo
  entra en vigor sin reiniciar la plataforma. El log también dice
  `Applied N patches to: …` (parcheo de compatibilidad interna de ATAS).
- El `AssemblyPatcher` reescribe los DLL: no compares byte a byte el fichero
  instalado con el que compila `dotnet build`.

## 7. Performance: `OnNewTrade` es el callback caro

`OnNewTrade` se llama **por cada print**. `MarketDepthsChanged` /
`MarketDepthChanged` / `OnBestBidAskChanged` también son de alta frecuencia.

- En esos callbacks: **solo cachear**. Nada de ficheros, JSON, HTTP ni locks
  largos.
- `OnCalculate` ya está limitado por el throttling del propio exportador
  (300 ms), así que leer el DOM ahí sale a ~3 Hz. Por eso el exportador **no**
  usa el push de profundidad: el poll ya está acotado y el push no mejora nada
  medible.

## 8. Multinstrumento

`InstrumentInfo?.Instrument` da el símbolo del chart. El exportador escribe
`_live_indicators_<SÍMBOLO>.json`, uno por chart. Solo el chart visible
produce datos frescos; los demás quedan congelados y el puente los descarta
por antigüedad (`ATAS_LIVE_MAX_AGE_SECONDS`, 300 s por defecto).

## 9. Referencia rápida de la API (esta build)

```
BaseIndicator   CurrentBar, DataSeries, OnCalculate (abstract), OnInitialize,
                OnRecalculate, OnFinishRecalculate, RecalculateValues,
                Panel, Name, FirstVisibleBarNumber, LastVisibleBarNumber,
                ChartInfo (IChart), ChartArea (Rectangle), Container
ExtendedIndicator OnRender, OnNewTrade, OnNewTrades, OnCumulativeTrade,
                MarketDepthChanged, MarketDepthsChanged, OnBestBidAskChanged,
                SubscribeToTimer, EnableCustomDrawing, DataProvider
IndicatorCandle  Open/High/Low/Close, Volume, Delta, OI, VWAP, Ticks,
                MaxDelta, MinDelta, ValueArea, MaxVolumePriceInfo,
                MaxTickPriceInfo, GetAllPriceLevels()
MarketDataArg   Price, Volume, Direction, Time, OpenInterest, IsBid, IsAsk
PriceVolumeInfo Price, Volume, Bid, Ask, Ticks
ValueArea       ValueAreaHigh, ValueAreaLow   (sin POC)
VisualMode      Line, Histogram, Hash, Block, Cross, Square, Dots,
                UpArrow, DownArrow, OnlyValueOnAxis, Hide
```

## 10. Cómo inspeccionar la API sin adivinar

Windows PowerShell 5.1 **no puede** reflejar sobre estos ensamblados (son
net10.0 y `System.Runtime v10` no carga). El camino que funciona es un
proyecto .NET 10 que resuelve las dependencias con un `AssemblyLoadContext`:

```csharp
AssemblyLoadContext.Default.Resolving += (ctx, name) =>
{
    var p = Path.Combine(@"C:\Program Files\ATAS X", name.Name + ".dll");
    return File.Exists(p) ? ctx.LoadFromAssemblyPath(p) : null;
};
var asm = Assembly.LoadFrom(@"C:\Program Files\ATAS X\ATAS.Indicators.dll");
```

Un dumper que funciona está en
`%TEMP%\opencode\atas-api-dump\` (`dotnet run`).
