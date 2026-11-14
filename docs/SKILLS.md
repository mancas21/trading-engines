# Skills del sistema trading-engines

Inventario de skills que sirven a este repo. Hay tres capas: la **biblioteca
propia** (creada para este sistema, inyectada por ruta), las **skills de
pipeline** creadas dentro de `~/.zcode/skills`, y las **bibliotecas
instaladas** de terceros a nivel usuario.

---

## 1 · Biblioteca propia — `C:\Users\Lenovo\Manuel\skills\` (10 skills)

Skills quant escritas PARA este sistema. Source-only: se inyectan por ruta al
usarlas, no están instaladas como comandos. Referencias verificadas contra el
repo (2026-10-24).

| Skill | Qué hace |
|---|---|
| `binance-raw-data` | Fetch/paginación/razonamiento de datos Binance: klines USDⓈ-M y spot, funding history, exchangeInfo, premiumIndex |
| `crypto-market-microstructure` | Referencia de mecánica de perpétuos y order-flow/SMC — para construir o razonar indicadores y estrategias |
| `grid-backtest` | Backtest de Futures Grid Binance (spot/futuros, aritmético/geométrico, neutral/long/short) contra OHLC o L2 |
| `macro-context-data` | Referencia RETIRADA-como-dependencia (2026-10-15) del pipeline macro de QAE — qué era y por qué pasó a manual |
| `ml-for-crypto-trading` | ML sobre señales de trading y agentes LLM — feature engineering en datos de order-flow/precio |
| `quant-crypto-analysis` | Análisis de mercado en vivo ("analiza / análisis / analyze") — el exec report de trader quant |
| `quant-research` | Protocolo ANTES de tratar cualquier indicador/estrategia como validado — backtest, criterios de aceptación |
| `session-stats-quant` | "¿Cuánto puede rally/pullback en la sesión?" — límites estadísticos de precio (sigmas) intradía |
| `strategy-hypothesis-families` | Familias de edge y el modelo de régimen que elige la estrategia correcta por régimen de mercado |
| `trading-systems-engineering` | Plomería de sistemas live — WebSocket ingestion, integración ccxt REST+WS, supervisión |

## 2 · Skills de pipeline — `~\.zcode\skills\` (creadas para este sistema)

| Skill | Qué hace |
|---|---|
| `episode-stats` | "¿Hasta dónde puede llegar este bajón?" — estadística de episodios gemelos del propio histórico: piso 10d con niveles, timing, gauge de capitulación. Motor: `scripts/episode_stats.py` (782e17f). Constante también en el phase brief (pares débiles) |

## 3 · Bibliotecas instaladas a nivel usuario — `~\.zcode\skills\`

Instaladas de fuentes públicas (no exclusivas de este repo, pero de uso
frecuente en él):

- **day-trader-skill** (11 skills `trader-*`) — US stocks, Yahoo/SEC, journal,
  scanner, entry/exit/risk. Instalada 2026-10-27.
- **claude-trading-skills** (mancas21, 68 skills) — crypto/DeFi/quant:
  on-chain, DeFi, derivados, portfolio. Instalada 2026-10-29.
- **trading-skills** (mancas21 fork de marian2js, 95 skills) — frameworks de
  proceso y decisión de trading. Instalada 2026-11-01.

Total instalado: **174 directorios** de skills a nivel usuario.

## Uso

- Las de la capa 1 se inyectan por ruta (leer su `SKILL.md` cuando el trabajo
  lo pida — p.ej. `quant-crypto-analysis` es el exec report "analiza").
- Las de las capas 2-3 están instaladas y el agente las carga por nombre
  (`/episode-stats`, o disparadores de lenguaje natural según su
  `description`).
- Regla de casa (audit 5.5 / §5.7): ningún análisis se publica como señal sin
  pasar por el protocolo de validación correspondiente — `quant-research` es
  la puerta.
