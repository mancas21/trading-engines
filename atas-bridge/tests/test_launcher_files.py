"""Contrato de los ficheros de arranque (.bat/.cmd): solo ASCII.

MEDIDO 2026-10-16, en esta maquina. `cmd.exe` lee los .bat en la codepage OEM, no
en UTF-8, y su parser lleva un desplazamiento de BYTE dentro del fichero. Si una
linea posterior contiene cualquier byte no-ASCII y el final de linea que la
precede es LF (y no CRLF), cmd pierde 2 bytes de ese desplazamiento y ejecuta la
linea SIN sus dos primeros caracteres. Matriz medida (final de linea x raya):

    A  linea previa CRLF, linea con raya CRLF -> limpio
    B  linea previa LF,   linea con raya LF   -> 'M' ...  (el REM se vuelve 'M')
    C  linea previa CRLF, linea con raya LF   -> limpio
    D  linea previa LF,   linea con raya CRLF -> 'M' ...
    G  linea minima con raya, CRLF            -> limpio
    H  linea minima con raya, LF              -> 'M' ...
    E/F  la misma matriz SIN raya (solo ASCII) -> limpio en los dos casos

O sea: el disparador es el NO-ASCII; el LF es lo que lo descoloca. Y ASCII con LF
es seguro (E/F), asi que la guardia NO exige CRLF: exige ASCII. Es la condicion
minima que reproduce el fallo y la unica que hace falta vigilar.

Coste real medido en este arbol: TRES launchers imprimian

    cmd : "M" no se reconoce como un comando interno o externo,

en CADA arranque -- reader/reader_supervisor.bat, quant-alert-engine/
qae_supervisor.bat y scanner-engine/run_scanner.bat -- porque cada uno llevaba una
raya (U+2014, bytes E2 80 94) en un REM. Es el mismo defecto que
bridge_supervisor.bat documenta para un parentesis abierto o un porcentaje suelto,
solo que los caracteres no-ASCII no estaban en esa lista. Sobre una linea que NO
sea REM, ese desfase ejecutaria un comando EQUIVOCADO sin avisar.

Contexto: .gitattributes declara *.bat/*.ps1/*.vbs con eol=crlf, pero el arbol de
trabajo tenia LF sueltos en 49 launchers (escritos por herramientas, no por git;
git normaliza al comparar y por eso `git status` no lo veia). Se normalizaron los
13 .bat/.cmd y se quito el no-ASCII. Esta prueba cubre la mitad que rompe de
verdad; el CRLF queda como convencion, no como requisito de correccion.

Uso:  pytest atas-bridge/tests/
"""
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]              # atas-bridge/tests -> atas-bridge -> raiz del repo

# El descubrimiento no debe entrar en arboles de terceros ni en copias.
SKIP = ("node_modules", "\\venv\\", "/venv/", "site-packages", "_backup")


def launchers():
    """Todos los .bat/.cmd del repo, fuera de venv/node_modules/copias."""
    found = []
    for pattern in ("*.bat", "*.cmd"):
        for p in ROOT.rglob(pattern):
            s = str(p)
            if any(k in s for k in SKIP):
                continue
            found.append(p)
    return sorted(found)


def test_the_discovery_is_not_vacuous():
    """Si el descubrimiento se rompe, la guardia se volveria vacua en silencio."""
    files = launchers()
    assert len(files) >= 8, f"solo {len(files)} .bat/.cmd encontrados bajo {ROOT}"


def test_no_launcher_contains_non_ascii_bytes():
    bad = {}
    for p in launchers():
        raw = p.read_bytes()
        if any(b > 127 for b in raw):
            lines = [i + 1 for i, ln in enumerate(raw.split(b"\n"))
                     if any(c > 127 for c in ln)]
            bad[str(p.relative_to(ROOT))] = {"bytes": sorted({b for b in raw if b > 127}),
                                             "lineas": lines}
    assert not bad, (
        "bytes no-ASCII dentro de un .bat/.cmd. MEDIDO: cmd lee estos ficheros en "
        "la codepage OEM y, con un final de linea LF delante, se come los dos "
        "primeros caracteres de la linea (un REM pasa a ser el comando 'M', que "
        "no existe). Ponlo en ASCII en el propio fichero. Afectados: "
        f"{bad}")
