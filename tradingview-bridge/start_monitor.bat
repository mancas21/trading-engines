@echo off
REM 2026-10-07: esta linea hacia `cd C:\Users\Lenovo\claudeverstradingview`, una
REM COPIA VIEJA de este bridge. monitor.log de ahi showed:
REM   [2:40:16 p.m.] BINANCE:XRPUSDT.P $1.42 | watching...
REM   >> [TG] Analisis completo enviado
REM Es decir: la tarjeta que llegaba a Telegram la producia el codigo de julio
REM (RSI sin Wilder, POC sintetico, sin order-flow de ATAS), no este repositorio.
REM Por eso los datos de la tarjeta no coincidian con el analisis.
REM
REM La copia vieja esta archivada en _archive_claudeverstradingview-*. La ruta
REM se deja fija y sin variable de entorno a proposito: un %CD% cambiaria el
REM comportamiento silenciosamente, que es justo el fallo que acabamos de fixar.
cd /d C:\Users\Lenovo\trading-engines\tradingview-bridge
if not exist monitor.mjs (
  echo ERROR: monitor.mjs no esta en %CD% - no se arranca nada.
  exit /b 1
)
node monitor.mjs 8 > monitor.log 2>&1
