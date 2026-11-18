# MARKET NARRATIVE — estado de la historia por par (compartido entre sesiones)

**Actualizado:** 2026-11-17 19:40 COT · **Mantenido por:** sesiones de análisis en vivo
(exec briefs) y el cron horario de briefs. REGLA: cada lectura/brief CONTINÚA
esta historia (qué cambió desde la última entrada) y la actualiza al cerrar.
Nunca reiniciar de cero.

---

## RÉGIMEN DE MERCADO (2026-11-17 19:40 COT)

Rotación, no risk-on amplio: BTC 64.8k en rango 63.3-65.4k (+0.5% 24h) y ETH
1.820 (+0.4%, LH/LL) no lideran. La fuerza está en beta alta (JUP +8.2%, SOL
+5.5% tras barrer 71.02 y romper 74.29) y en equity tokenizada (NVDA +3.9% con
volumen 6h 2.5x el previo, SPCX +4.3%). Débiles: PEPE -2.3% (barrió 0.0036485
sin cerrar bajo 0.00369) y XAU -0.8% pegado al mínimo 3.825. ATAS cerrado: sin
footprint/DOM/tape/CVD; lectura de estructura y precio, no de flujo. Gate
scanner: 12/12 neutral.

## RÉGIMEN DE MERCADO (2026-11-15) — previo

Purga acelerando: BTC 64.7k (75.8k→71.5k→68k→63.7k mínimo intradiario con rebote; funding AÚN positivo
+0.007% = los largos NO capitulan — el capítulo final de la purga sigue
pendiente), ETH rompió 2000 y 1800 (1.805), XRP rompió el dollar level (1.02), NVDA
sangrando (211→160). Periferia con suelos: HYPE/SUI/JUP/XRP/SOL llevan días
sin hacer nuevos mínimos mientras el núcleo gotea. Gate de señales RETIRADO
(2026-11-13) — sin sesgo formal; toda lectura direccional es interpretativa.

## 1000PEPE (par objetivo del bridge)

**Arco:** 12-nov 0.00439 → flush 0.00369 → firma de suelo completada (funding
−0.05%) → absorción en POC (15-nov) → **INVALIDACIÓN EJECUTADA (15/16-nov):
la absorción MURIÓ.** La confirmación exigida (CHOCH/BOS alcista 5m/15m sobre
0.00401) nunca llegó: el precio tocó 0.00401-0.00402 dos veces y cerró debajo
del POC 0.003968 → 15m CHOCH bajista @ 0.0039779 → tape 100% ventas.

**Última lectura de flujo (16-nov, ATAS):** barra delta −28.8M, tape neto
negativo con 0% prints compradores, DOM EMA −26.9% (sigue vendido), CVD −550M
(otra ola de −94M tras el flush). Funding −0.047% (cortos aún apiñados).
La estructura micro es BAJISTA: último evento del early-engine 1m CHOCH short.

**Estado:** tesista bajista de continuación medida (deep-dd perde en sección
cruzada — el veredicto §5.7 aplicado a esta situación). Pisos: 0.00394
(micro), 0.00369 (pierna), 0.00344 (episode-stats). El guion alcista se
REINICIA desde cero: exigiría un CHOCH/BOS alcista 15m NUEVO sostenido sobre
0.00401-0.00412 con funding aún más profundo — la vieja absorción no cuenta.

**Lectura 17-nov 19:40 COT (sin ATAS):** el nivel 0.00369 se BARRIÓ (mínimo
24h 0.0036485) pero NO hubo cierre 1h por debajo (mínimo cierre 0.0037069,
17-nov 16:00 UTC): la continuación bajista a 0.00344 no se ejecutó. Desde el
barrido, mínimos crecientes 0.0036485 → 0.0036868 → 0.00370/0.00379; precio
0.0038062 probando 0.003825 (pivote 1h) y 0.003878 (máximo 15m). Arriba:
0.003978 (24h) y 0.00401-0.00402 (CHOCH viejo). Sin flujo no hay confirmación:
spring en prueba, no suelo confirmado.

## SOL / XRP / SUI / ARB / HYPE (débiles, pisos estadísticos)

Suelos episode-stats vigentes: SOL 65.5 · SUI 0.644 · XRP 0.896 · ARB 0.120.
XRP 0.957 (rebotando del dollar level roto, mínimos crecientes), SUI 0.719 (a un 10% de su piso),
SOL 71.4 (a un 8% de su piso 65.5). Todos con mínimos crecientes en 12h — periferia construyendo
suelos mientras el núcleo gotea. HYPE = fuerza relativa recurrente (+2.3%
en tablero rojo).

**17-nov 19:40 COT:** SOL 75.3 rompió 74.29 (barrió 71.02, +5.5% 24h); XRP
0.984 recuperó 0.9766 y el dollar level, mira 0.9911; SUI 0.7356 comprime
0.7135-0.7441 (LH/HL, precio en el vértice); ARB 0.132 en medio del rango
0.1279-0.1351; HYPE 69.65 recuperó 68.82, techo 70.71. Pisos episode-stats
sin tocar: la periferia ya no construye suelo, empieza a resolverlo.

## GATE

Retirado 2026-11-13 (rules 6/7 eliminadas, v3.5.0) — bias NEUTRAL por
construcción. Defecto abierto: el notificador pierde cambios de sesgo <60s.
Las banderas del early-engine (SMC CHOCH/BOS, detectores de flujo) están en
SHADOW — hechos, sin protocolo validado; no tratarlas como señal.

**17-nov:** scanner /rank 12/12 neutral. PEPE/BTC/ETH/HYPE/XAU solo cumplen
la regla 1; XRP la 4; ARB/SPCX/SUI tienen la regla 4 UNEVALUABLE; nadie
cumple 2/3/4. Sin plan (Rule 10).
