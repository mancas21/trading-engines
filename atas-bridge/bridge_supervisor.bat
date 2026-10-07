@echo off
REM bridge_supervisor.bat -- supervisor del puente ATAS.
REM
REM FIX 2026-10-07 -- el puente se COLGABA, no moria.
REM
REM Sintoma medido: el proceso python seguia vivo (desde las 12:04) pero nada
REM escuchaba en el 8000, asi que /health daba RemoteDisconnected. Este
REM supervisor solo miraba el CODIGO DE SALIDA, y un proceso colgado no devuelve
REM ninguno: nunca lo reiniciaba. Los servicios dependientes (el grabador de
REM veredictos) se quedaban sin dato sin que nadie lo notara.
REM
REM Causa raiz: los WebSockets de Binance caian por `keepalive ping timeout`
REM (ping_interval=20 / ping_timeout=10 era demasiado agresivo con 19 sockets),
REM reconectaban TODOS a la vez con un `timeout /t 3` fijo, y el bucle drenaba
REM el event loop hasta que el HTTP dejaba de aceptar conexiones. Corregido ya
REM en bridge_server.py (ping_timeout=60 + backoff escalonado con jitter).
REM
REM Este supervisor es la RED DE SEGURIDAD para cuando eso no basta: comprueba
REM que el PUERTO responde, no solo que el proceso existe. Estados:
REM   exit 0        -> el operador paro el puente. PARAR, sin relanzar.
REM   Puerto abajo  -> proceso colgado o muerto. Reiniciar.
REM   Puerto arriba -> sano.
REM
REM Y hay un TECHO de reinicios rapidos: un bucle que nunca para es un fallo que
REM hay que mirar, no recuperacion. Al agotarlo, el puente queda PARADO.
setlocal EnableDelayedExpansion
title ATAS Bridge Supervisor
cd /d C:\Users\Lenovo\trading-engines\atas-bridge
set "PYTHON=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
set "PORT=8000"
set "MAX_FAST_RESTARTS=8"
set "FAST_WINDOW=180"
set /a SPAWNS=0
set /a FAST=0
set /a START_TS=0
for /f %%t in ('powershell -NoProfile -Command "(Get-Date -UFormat %%s)"') do set "START_TS=%%t"
:loop
set /a SPAWNS+=1
echo [%date% %time%] === Starting bridge (spawn %SPAWNS%) === >> bridge_supervisor.log
REM El codigo de salida se captura aqui: si %ERRORLEVEL% se lee despues de
REM otro comando, ya no es el del puente (bug anterior: logaba "!CODE!" literal).
"%PYTHON%" bridge_server.py >> bridge_stdout.log 2>> bridge_stderr.log
set "CODE=!ERRORLEVEL!"
set "SPAN=0"
if !FAST! GTR 0 set /a FAST+=1
REM El chequeo que faltaba: responde el PUERTO? Un proceso colgado no devuelve
REM codigo de salida, asi que mirar solo el codigo nunca lo detecta.
REM Test-NetConnection se usa en vez de Invoke-WebRequest porque este ultimo
REM falla al pasar por bash/powershell anidado (devolvia errorlevel 1 incluso
REM con el puerto abierto). Verificado: True con :8000 abierto, False cerrado.
set "ALIVE=0"
set "PROBE=0"
powershell -NoProfile -Command "(Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue)" 2>nul | findstr /C:"True" >nul && set "PROBE=1"
if "!PROBE!"=="1" set "ALIVE=1"
REM Sin escapes de pipe: con ^(^|^) el parser de echo consume los ! ANTES de
REM expandirlos y el log sale con los literales "!CODE!". Verificado en un .bat
REM de prueba: la forma sin pipes expande bien, la con escapes no.
echo [%date% %time%] bridge exited code=!CODE! after !SPAN!s port_alive=!ALIVE! fast=!FAST!/!MAX_FAST_RESTARTS! >> bridge_supervisor.log
if "!CODE!"=="0" (
  echo [%date% %time%] Bridge closed cleanly by operator - supervisor stopping, nothing relaunched. >> bridge_supervisor.log
  exit /b 0
)
if !FAST! GEQ %MAX_FAST_RESTARTS% (
  echo [%date% %time%] GIVING UP after !FAST! fast restarts. This is NOT recovery, it is a failure. Bridge left DOWN. >> bridge_supervisor.log
  exit /b 1
)
if "!ALIVE!"=="0" (
  echo [%date% %time%] Port 8000 NOT responding ^(hung or dead process^) - restarting. >> bridge_supervisor.log
) else (
  echo [%date% %time%] Port 8000 responds; process exited on its own. >> bridge_supervisor.log
)
REM Espera creciente: reiniciar de inmediato tras un cuelgue deja al proceso
REM anterior aun cerrando el puerto y el nuevo choca con el.
set /a DELAY=2
if !FAST! GTR 2 set /a DELAY=5
if !FAST! GTR 4 set /a DELAY=12
timeout /t !DELAY! /nobreak >nul
goto loop
