"""La identidad de barra del ledger no puede mezclar instrumentos.

Medido 2026-10-16. reader.db guardaba UNA clave compartida por DOS simbolos:
`20740|21:30:00|0`, 30 filas — las ultimas 25 de XRPUSDT (ids 2911-2935) y las
5 primeras de JUPUSDT (ids 2936-2940). El cambio de grafica (XRP -> JUP) cayo en
la MISMA etiqueta de vela y el mismo dia, y `bar_key()` solo miraba etiqueta y
dia: sin retroceso de etiqueta no abria segmento nuevo.

Importa porque `bar_key` es la identidad que sustituyo a `bar_index` (que
colisionaba igual: barra 950 en los dos instrumentos) y porque en la serie de
precios del ledger ese punto se lee como un salto de x4,17 (1,3328 -> 0,3198):
un artefacto de identidad, no un movimiento.

Contratos que fija este fichero:
  1. Cambiar de instrumento con la MISMA (dia, etiqueta) abre segmento nuevo:
     es el caso medido, y se fija la clave exacta de cada lado.
  2. La misma barra releida conserva su clave.
  3. Un retroceso de etiqueta y un rollover de dia siguen abriendo segmento.
  4. El estado se siembra desde el ledger: el grabador corre como proceso corto
     (`record` por ciclo), asi que sin seed el contador renacia en cada arranque.
  5. El ledger vivo no contiene ninguna clave compartida por 2+ instrumentos.
"""
import pathlib
import sqlite3
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import verdict_recorder as VR       # noqa: E402


def _ts(day, hh, mm, ss=0):
    return day * 86400 + hh * 3600 + mm * 60 + ss


DIA_XRP_JUP = 20740                          # 2026-10-15, dia del cambio XRP->JUP


@pytest.fixture(autouse=True)
def _proceso_recien_arrancado():
    """Cada test empieza como un proceso nuevo; el seed lo prueba su propio test."""
    VR._BAR_SEGMENT = 0
    VR._last_bar_key = None
    VR._last_tag = None
    VR._last_day = None
    VR._last_symbol = None
    yield


def test_cambio_de_instrumento_con_la_misma_etiqueta_no_comparte_clave():
    k_xrp = VR.bar_key("21:30:00", _ts(DIA_XRP_JUP, 21, 30), "XRPUSDT")
    k_jup = VR.bar_key("21:30:00", _ts(DIA_XRP_JUP, 21, 30, 30), "JUPUSDT")
    assert VR.bar_key_str(k_xrp) == "20740|21:30:00|0"
    assert VR.bar_key_str(k_jup) == "20740|21:30:00|1"
    assert k_xrp != k_jup


def test_la_misma_barra_releida_conserva_su_clave():
    a = VR.bar_key("21:30:00", _ts(DIA_XRP_JUP, 21, 30), "JUPUSDT")
    b = VR.bar_key("21:30:00", _ts(DIA_XRP_JUP, 21, 32), "JUPUSDT")
    c = VR.bar_key("21:45:00", _ts(DIA_XRP_JUP, 21, 45), "JUPUSDT")
    assert a == b, "la misma vela releida no puede abrir segmento"
    assert c[2] == a[2], "avanzar de vela dentro de la sesion no es un reinicio"
    assert c[1] == "21:45:00"


def test_retroceso_y_rollover_de_dia_siguen_abriendo_segmento():
    VR.bar_key("21:30:00", _ts(DIA_XRP_JUP, 21, 30), "JUPUSDT")
    k_back = VR.bar_key("20:00:00", _ts(DIA_XRP_JUP, 22, 0), "JUPUSDT")
    assert k_back[2] == 1, "un retroceso de etiqueta tiene que abrir segmento"
    k_day = VR.bar_key("00:05:00", _ts(DIA_XRP_JUP + 1, 0, 5), "JUPUSDT")
    assert k_day[2] == 2, "el rollover de dia tiene que abrir segmento"


def _db_con_ultima_fila(tmp_path, symbol, key):
    c = sqlite3.connect(tmp_path / "t.db")
    c.executescript(VR.SCHEMA)
    VR._migrate(c)
    c.execute("INSERT INTO verdicts (ts, symbol, bias, score, horizon_bars, "
              "bar_key, bar_seg) VALUES (?,?,?,?,?,?,?)",
              (_ts(DIA_XRP_JUP, 23, 0), symbol, "NEUTRAL", 0, 12, key,
               int(str(key).split("|")[2])))
    c.commit()
    return c


def test_el_seed_continua_la_identidad_desde_el_ledger(tmp_path):
    c = _db_con_ultima_fila(tmp_path, "JUPUSDT", "20740|23:00:00|1")
    VR._seed_bar_key_state(c)
    assert VR._last_symbol == "JUPUSDT"
    assert VR._BAR_SEGMENT == 1
    k_sig = VR.bar_key("23:15:00", _ts(DIA_XRP_JUP, 23, 15), "JUPUSDT")
    assert VR.bar_key_str(k_sig) == "20740|23:15:00|1"
    k_otro = VR.bar_key("23:15:00", _ts(DIA_XRP_JUP, 23, 15, 20), "SUIUSDT")
    assert VR.bar_key_str(k_otro) == "20740|23:15:00|2", (
        "reiniciar en otro instrumento tiene que abrir segmento")


def test_el_ledger_vivo_no_comparte_claves_entre_instrumentos():
    db = HERE / "reader.db"
    if not db.exists():
        pytest.skip("reader.db no esta en esta maquina (no va en git)")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        filas = con.execute(
            "SELECT bar_key, COUNT(DISTINCT symbol) FROM verdicts "
            "WHERE bar_key IS NOT NULL GROUP BY bar_key "
            "HAVING COUNT(DISTINCT symbol) > 1").fetchall()
    finally:
        con.close()
    assert filas == [], f"claves compartidas por 2+ instrumentos: {filas[:5]}"
