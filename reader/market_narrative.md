# MARKET NARRATIVE — estado de la historia por par (compartido entre sesiones)

**Actualizado:** 2026-11-26 09:20 COT (barrido live + OpenMarket metric_get, chart tab cerrado) · **Mantenido por:** sesiones de análisis en vivo
(exec briefs) y el cron horario de briefs. REGLA: cada lectura/brief CONTINÚA
esta historia (qué cambió desde la última entrada) y la actualiza al cerrar.
Nunca reiniciar de cero.

---

## RÉGIMEN DE MERCADO (2026-11-20, live)

**CAPÍTULO FINAL EN CURSO — las dos condiciones del 15-nov se alinearon:**
(1) los pisos estadísticos se están probando SIMULTÁNEAMENTE (PEPE +0.6%
sobre el suyo, SUI +1.4%, XRP +1.8%, SOL +5.3%), y (2) **el funding de BTC y
ETH giró NEGATIVO por primera vez en toda la purga** (BTC −0.004%, ETH
−0.019%) — los largos capitularon y ahora SON LOS CORTOS los que pagan.
BTC 59,020 (rompió 60k), ETH 1,619, NVDA −11.9% en un día (129, de 211),
XAU −2.7% cayendo también. Todo lh=True, nada construye suelo AÚN.

## 1000PEPE (par objetivo del bridge)

**Arco:** 0.00439 → flush 0.00369 → firma de suelo (funding −0.056%) →
absorción INVALIDADA → segunda pierna 0.00394 rota → **20-nov: tocando su
piso estadístico 0.00344 (a +0.6%)**. Funding −0.0024% (ya no extremo — los
cortos cubrieron en la caída). El guion bajista se cumplió entero; ahora el
mercado está EN el piso que episode-stats marcó. Primer test en vivo del
piso bajo condiciones de capitulación — observación, no señal (el mecanismo
funding→rebote murió en §5.7; este episodio es la prueba en vivo que faltaba).

**23-nov 20:30 COT — chart OpenMarket 7kOL6Nwh (PEPE-USDT-SWAP, OKEX, 5m):**
impulso alcista intradía limpio: desde el mínimo 0.0034014 (18:55 COT) sube
por HH/HL hasta 0.0035032 (20:10), cierra 0.003492 con volumen creciendo en
el tramo final (214B/188B/198B vs 46-100B al inicio). Precio POR ENCIMA de la
banda superior de VWAP (0.0034748) y del VWAP (0.0034501): sobre-extensión de
banda en 5m. En 1h sigue LH/LL (0.0034487 Binance, -1,8% en 6h) — el impulso
5m aun no repara la estructura mayor. Soporte 0.0034501 (VWAP) / 0.0034254
(banda baja); invalidacion del impulso = cierre 5m bajo 0.0034748.
Footprint/CVD del chart NO exponen valores por chart_read: no se imputa delta.
Gate scanner PEPE 0 (neutral), artefactos STALE (0/2 verificado): sin plan.

**26-nov 09:20 COT — REBOTE GENERALIZADO, la periferia lidera.** Todas las
pares verdes 24h (BTC +4,4% 60.833, ETH +4,2% 1.704, SOL +6,5% 71,4,
SUI +7,5%, HYPE +8,1%, JUP +10,1%, XRP +5,9%, PEPE +5,6%). El movimiento
es un short squeeze: funding PEPE +0,0033%/8h (los cortos pagan), OI 25,9M.
PERO CVD 5m de PEPE en OKX = −927.875 en 50 barras con taker_buy 0,695:
precio arriba, CVD abajo = distribución/absorbencia, no compra agresiva
sostenida. RSI 5m 48,8 (neutral), precio 0,0000035232 en la parte media de
BB (0,0000035108-0,0000035293), EMA20≈precio<EMA50. Chart tab de OpenMarket
cerrado: leído via metric_get sin tab. Sin ATAS: sin footprint por barra.
Gate: BTC/HYPE/XAU score 20, PEPE/ARB/ETH/SOL 10, todos neutral → sin plan.

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

**18-nov 14:54 COT:** SUI 0.7178 (−0.3% 24h, RS vs BTC −1.8%/ETH −2.0%)
resolvió la compresión 0.7135-0.7441 POR ABAJO: barrido 0.7024 (nuevo mínimo
bajo 0.7056) y reclamo hasta 0.7239; ahora de vuelta dentro del valor 3d
(POC 0.7216, VAL 0.7149, VAH 0.7310) pero bajo el POC. Estructura 15m sigue
LH 0.7340 → 0.7286 → 0.7239: spring en prueba, sin CHOCH. Funding −0.008%/8h,
LS 2.18, OI 101M SUI; DOM Binance +20% (muro bid 0.7174/361k, ask 0.7202/374k);
tape 10s comprador 6.4k vs 1.5k pero cierra con prints de venta. ATAS cerrado:
sin footprint/CVD por barra, gate UNAVAILABLE → sin plan (Rule 10).

## GATE

Retirado 2026-11-13 (rules 6/7 eliminadas, v3.5.0) — bias NEUTRAL por
construcción. Defecto abierto: el notificador pierde cambios de sesgo <60s.
Las banderas del early-engine (SMC CHOCH/BOS, detectores de flujo) están en
SHADOW — hechos, sin protocolo validado; no tratarlas como señal.

**17-nov:** scanner /rank 12/12 neutral. PEPE/BTC/ETH/HYPE/XAU solo cumplen
la regla 1; XRP la 4; ARB/SPCX/SUI tienen la regla 4 UNEVALUABLE; nadie
cumple 2/3/4. Sin plan (Rule 10).
