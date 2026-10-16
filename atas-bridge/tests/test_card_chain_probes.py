"""Contrato de la cadena de la tarjeta (quant-alert-engine/card_live): sin sondas muertas.

MEDIDO 2026-10-16, en vivo. `fetch_live.py` sondeaba en CADA pull tres endpoints
que ya no podian contestar nada util, nunca mas:

  - `calls` y `calls/stats`: el subsistema /calls se borro en la Fase 6
    (orderflow_calls.py, la ruta perdedora: 25 calls, winrate 36%). El bridge
    contesta 404 y `live_read.py` metia una linea por cada uno en `degraded`.
  - `quant`: el buffer que solo llena el QAE con POST /quant-alert. Con el QAE
    cancelado no puede recibir nada: `out["quant"]` era {} para siempre y la
    tarjeta imprimia "freshest alerts: none" en cada lectura como si fuera una
    medida.

Y ademas: el QAE (:8081) se sondeaba, se listaba en `degraded`, y publicaba
`health.qae` / `qae_processes` / `uptime_qae_h` / `qae_gates` / `ml_filter`.

Efecto medido: de las TRES entradas que la tarjeta imprimia en
"FEED DEGRADED — 3 source(s) down", DOS eran subsistemas retirados que nadie
habia desconectado. Un banner de degradado que siempre trae dos lineas fijas deja
de significar "algo se ha roto": enseña a ignorarlo, que es justo lo contrario de
lo que un aviso de salud debe hacer. Es el mismo defecto que watchdog.py corrigio
el 2026-10-15 para su propio checker de /calls (ver test_watchdog_health.py), y la
misma regla: un control que no puede moverse no es un control — o se cablea o se
quita.

Esta guardia lee el CODIGO (AST: constantes de cadena y identificadores), NO los
comentarios ni los docstrings: el porque de cada retirada esta escrito en prosa en
los propios ficheros y esa prosa no debe hacer fallar la prueba. Nombra los tokens
retirados una sola vez aqui, que es donde toca.

Uso:  pytest atas-bridge/tests/
"""
import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # atas-bridge/tests -> atas-bridge -> raiz del repo
CARD = ROOT / "quant-alert-engine" / "card_live"
FILES = ["fetch_live.py", "live_read.py", "live_card.py"]

# Tokens prohibidos en el CODIGO de la cadena. Prohibidos como sonda, como clave
# de diccionario y como nombre de variable: cualquiera de las tres formas vuelve a
# encender el banner de degradado o a publicar un campo que nadie escribe.
#
# Se bloquea el token DESNUDO, no la ruta: el codigo original sondeaba los
# endpoints por nombre (`"calls"`, `"quant"`) y la URL se componia despues, asi que
# vigilar solo "/calls" no habria visto la regresion. Medido: con la guardia
# estrecha, reinyectar `"calls"` en la lista de sondas pasaba el test en verde.
MUERTOS = ("calls", "8081", "quant", "qae")

# Sondas vivas que tienen que seguir existiendo: si desaparecen, la guardia se
# volveria vacua en silencio (el defecto opuesto).
VIVAS = ("health/detailed", "blackout/status", "indicators")


def _tree(name):
    return ast.parse((CARD / name).read_text(encoding="utf-8"))


def _docstrings(tree):
    """ids de los nodos que son docstring (modulo, funcion, clase)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _code_strings(tree):
    docs = _docstrings(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def _identifiers(tree):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
    return names


def test_la_cadena_existe_y_las_sondas_vivas_siguen_ahi():
    """Si el descubrimiento o el parseo se rompen, la guardia seria vacua."""
    missing = [f for f in FILES if not (CARD / f).is_file()]
    assert not missing, f"faltan ficheros de la cadena de la tarjeta: {missing}"
    strings = _code_strings(_tree("fetch_live.py"))
    for sonda in VIVAS:
        assert sonda in strings, (
            f"fetch_live.py ya no sondea {sonda!r}: la guardia estaria vigilando "
            f"una lista de sondas que no existe. Sondas actuales: {sorted(strings)[:8]}...")


def test_ningun_fichero_de_la_cadena_usa_los_tokens_retirados():
    ofensores = {}
    for f in FILES:
        tree = _tree(f)
        malos = sorted({s for s in _code_strings(tree)
                        if any(t in s.lower() for t in MUERTOS)})
        malos += sorted({n for n in _identifiers(tree)
                         if any(t in n.lower() for t in MUERTOS)})
        if malos:
            ofensores[f] = sorted(set(malos))
    assert not ofensores, (
        "la cadena de la tarjeta vuelve a tocar subsistemas retirados. MEDIDO: el "
        "subsistema /calls se borro en la Fase 6 (404 en cada pull) y el QAE esta "
        "cancelado, asi que sondearlos mete lineas fijas en `degraded` y el banner "
        "FEED DEGRADED salta con cero feeds caidos de verdad. Un control que no "
        f"puede moverse no es un control: o se cablea o se quita. Ofensores: {ofensores}")


def test_la_tarjeta_sigue_publicando_la_salud_del_puente():
    """Retirar el QAE no puede llevarse por delante la salud que SI es real."""
    strings = _code_strings(_tree("live_card.py"))
    assert any("ENGINE" in s for s in strings), "la tarjeta ya no imprime la linea ENGINE"
    assert any("blackout" in s for s in strings), "la tarjeta ya no imprime el blackout"
    assert any("bridge" in s.lower() for s in strings), "la tarjeta ya no imprime el estado del bridge"
