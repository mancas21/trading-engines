# Quant Alert Engine — MANUAL ONLY (since 2026-10-11)

> Ver tambien `README-DESACOPLADO.md` — lista de las 4 dependencias que se
> eliminaron y por que el scanner ahora corre con Python312.

**Este motor ya NO se autoarranca.** Vive como programa manual: lo abres
cuando lo quieres y lo cierras cuando terminas.

## Por que se desconecto

1. **La dependencia va en un solo sentido.** El puente ATAS (`:8000`) no
   llama nunca a QAE (`:8081`). Lo unico que hace QAE es POSTear alertas a
   `POST /quant-alert`, que el puente guarda en `quant_buffers` y sirve en
   `GET /quant/{SYM}`.
2. **Ese buffer estaba vacio.** En los analisis en vivo del 09 y 11 de
   octubre, `GET /quant/XRP` devolvio `{"window_size": 0, "alerts": []}`.
   La seccion "Quant Engine Signals" no estaba aportando nada a ningun
   veredicto: ya se analizaba sin ella, solo que la plantilla aun la
   mostraba como si fuera una fuente viva.
3. **`verdict.py` no lo usa.** El motor de reglas solo lee `/indicators` y
   `/cvd`. Cero referencias a QAE.
4. **Se caia solo y habia que relanzarlo a mano.** Como proceso de arranque
   era un generador de incidentes: caia, el watchdog lo reiniciaba, y a
   veces habia que intervenir.

Lo que NO se pierde nada de esto: el veredicto (Reglas 6/7/2), el CVD, el
footprint, el DOM, la lectura macro de CoinGecko y las derivadas siguen
viniendo del puente, no de aqui.

## Como abrirlo a mano

```powershell
cd C:\Users\Lenovo\trading-engines\quant-alert-engine
.\start-qae.ps1
```

Abre el supervisor detached (auto-reinicio si crasea). Comprobacion:

```powershell
curl.exe -s http://127.0.0.1:8081/health
```

## Como pararlo de verdad

```powershell
.\stop-qae.ps1                    # UI + supervisor
.\stop-qae.ps1 -IncludeHeadless   # ademas el engine headless
```

El orden importa: primero muere el supervisor, o resucita la UI a los 3 s.

## Aviso sobre la carpeta

**NO borres esta carpeta.** `scanner-engine/` usa
`quant-alert-engine\venv\Scripts\python.exe` como interprete. Borrarla mata
el scanner multi-venue, que esta vivo y sano en `:8090` con 8 pares x 4
venues. El desacople correcto (mover el venv y actualizar los scripts) es un
trabajo aparte, todavia no hecho.

## Cuando lo abras: recalls que no es fuente de verdad

QAE tiene su propio motor de reglas y sus propias senales. Si lo abres
mientras usas el otro sistema, remember:

- Sus senales NO pasan por `verdict.py` ni por `rules.json` (las reglas
  6/7/2 que gobiernan el veredicto).
- Las alertas que envie al puente salen por `POST /quant-alert`, sin pasar
  por las reglas del veredicto. Si quieres que una señal de QAE respete las
  reglas, eso hay que construirlo; hoy no existe.
- El gating por order-flow de nivel 2 (`level2.*` en `config.json`) consulta
  al puente, no al revés.