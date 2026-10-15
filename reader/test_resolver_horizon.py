"""El horizonte del resolutor tiene que ser el que la fila DECLARA.

Medido 2026-10-15. `do_resolve` pedia klines de **15 minutos** mientras indexaba
con `horizon_bars` (12) para calcular `exit_i = sig_i + h`. Como el indexado es en
unidades de la serie descargada, 12 barras de 15m son **180 minutos**: la fila
declaraba 12 barras (60 minutos si la grafica es de 5m) y la medicion cubria el
TRIPLE.

Importa porque `forward_return` es la tabla sobre la que se midio si las reglas 6
y 7 tienen edge — y la que sostiene su cuarentena. Un horizonte 3x mas largo mide
otra cosa: una deriva de tres horas en vez del movimiento posterior a la senal.

Y era invisible: la fila guarda `horizon_bars` pero NO el tamano de barra con el
que se resolvio, asi que no habia forma de notar el desajuste mirando la base. El
propio comentario del resolutor ya avisaba de la trampa gemela: emparejar
`bar_time` de ATAS contra aperturas de 15m daba CERO coincidencias, porque la
rejilla del chart es de 5 minutos.

Contratos que fija este fichero:
  1. La descarga usa EXACTAMENTE el tamano de barra del horizonte (una sola
     fuente: BAR_MINUTES). Re-hardcodear "15m" vuelve a romperlo.
  2. El horizonte declarado son 60 minutos.
  3. La ventana realizada son DEFAULT_HORIZON_BARS barras contadas desde el
     fill: fill en el OPEN de la barra siguiente a la de senal, salida en el
     CLOSE de la barra sig_i + h. (No hay off-by-one: son 12 barras enteras.)
"""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import verdict_recorder as VR       # noqa: E402

BAR_MS = 300_000                    # 5 minutos


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _klines(n=40):
    """Klines sinteticas con precios que identifican su indice.

    open[i]  = 100 + i
    close[i] = 100 + i + 0.5

    Asi la ventana realizada se puede recalcular a mano y el test no depende de
    ningun numero magico.
    """
    base = 1_700_000_000_000
    rows = []
    for i in range(n):
        o = base + i * BAR_MS
        rows.append([o, 100 + i, 100 + i + 1, 100 + i - 1, 100 + i + 0.5,
                     1.0, o + BAR_MS - 1, 0, 0, 0, 0, 0])
    return rows


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Nunca tocar reader/reader.db (el del servicio vivo)."""
    monkeypatch.setattr(VR, "DB", tmp_path / "reader.db")


def _capture_urlopen(monkeypatch, rows):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return _FakeResp(json.dumps(rows).encode())

    monkeypatch.setattr(VR.urllib.request, "urlopen", fake_urlopen)
    return seen


def _insert(c, ts, hz):
    c.execute(
        "INSERT INTO verdicts (ts, symbol, bar_index, bar_time, bar_key, bias,"
        " score, horizon_bars, resolved) VALUES (?,?,?,?,?,?,?,?,0)",
        (ts, "JUP/USDT", 1, "00:05:00", "JUP/USDT|seg0", "BULLISH", 0, hz))
    c.commit()


def test_the_downloaded_bar_size_is_the_one_the_horizon_is_measured_in(tmp_path, monkeypatch):
    """Contrato 1: la descarga usa BAR_MINUTES. Estuvo pidiendo 15m."""
    rows = _klines()
    seen = _capture_urlopen(monkeypatch, rows)
    c = VR.db()
    _insert(c, rows[5][0] / 1000.0, VR.DEFAULT_HORIZON_BARS)

    VR.do_resolve(c)

    assert "url" in seen, "el resolutor no descargo nada"
    assert f"interval={VR.BAR_MINUTES}m" in seen["url"], (
        f"la descarga no usa el tamano de barra del horizonte: {seen['url']}")
    assert f"interval={VR._RESOLVE_INTERVAL}" in seen["url"], (
        "el intervalo de la URL dejo de salir de la unica fuente (BAR_MINUTES)")


def test_the_declared_horizon_is_sixty_minutes():
    """Contrato 2: si alguien cambia el numero, tiene que ser a proposito."""
    assert VR.BAR_MINUTES == 5, "el chart de ATAS es de 5 minutos"
    assert VR.DEFAULT_HORIZON_BARS * VR.BAR_MINUTES == 60, (
        "el horizonte declarado dejo de ser 60 minutos")


def test_realized_window_is_exactly_horizon_bars_from_the_fill(tmp_path, monkeypatch):
    """Contrato 3: fill en el open de la barra siguiente, salida en el close de
    la barra sig_i + h. Son DEFAULT_HORIZON_BARS barras enteras."""
    rows = _klines()
    _capture_urlopen(monkeypatch, rows)
    k = 5                                  # barra de senal
    h = VR.DEFAULT_HORIZON_BARS
    c = VR.db()
    _insert(c, rows[k][0] / 1000.0, h)

    assert VR.do_resolve(c) == 1

    fill_open = 100 + (k + 1)
    exit_close = 100 + (k + h) + 0.5
    expected = (exit_close - fill_open) / fill_open

    got = c.execute("SELECT fill_price, forward_return, resolved FROM verdicts"
                    ).fetchone()
    assert got[2] == 1, "la fila sigue sin resolver"
    assert abs(got[0] - fill_open) < 1e-9, (
        f"el fill deberia ser el OPEN de la barra siguiente ({fill_open}), "
        f"no {got[0]}")
    assert abs(got[1] - expected) < 1e-8, (
        f"la ventana realizada no son {h} barras: esperado {expected:.8f}, "
        f"obtenido {got[1]:.8f}")


def test_the_fill_is_never_the_signal_bars_close(tmp_path, monkeypatch):
    """El close de la barra de senal solo se conoce DESPUES de que cierre:
    llenarse ahi inventa edge hacia atras. El fill tiene que ser un OPEN
    posterior al instante del veredicto."""
    rows = _klines()
    _capture_urlopen(monkeypatch, rows)
    k = 7
    c = VR.db()
    _insert(c, rows[k][0] / 1000.0, VR.DEFAULT_HORIZON_BARS)
    c.execute("UPDATE verdicts SET price_last=? WHERE id=1", (100 + k,))
    c.commit()

    VR.do_resolve(c)

    fill = c.execute("SELECT fill_price FROM verdicts WHERE id=1").fetchone()[0]
    signal_close = 100 + k + 0.5
    assert fill != signal_close, "el fill es el close de la barra de senal"
    assert fill >= 100 + (k + 1), "el fill no es posterior al instante de la senal"


def test_a_resolved_row_declares_the_bar_size_it_was_resolved_with(tmp_path, monkeypatch):
    """La OTRA mitad del defecto era que fuera invisible: la fila guardaba
    `horizon_bars` pero no la unidad con la que se resolvio, asi que mirando la
    base no habia forma de detectar el desajuste ni de saber que filas son
    comparables entre si.

    NULL = resuelta ANTES del arreglo (unidad desconocida, no mezclar).
    El valor = la unidad correcta."""
    rows = _klines()
    _capture_urlopen(monkeypatch, rows)
    c = VR.db()
    _insert(c, rows[5][0] / 1000.0, VR.DEFAULT_HORIZON_BARS)

    VR.do_resolve(c)

    got = c.execute("SELECT resolved_bar_min FROM verdicts WHERE id=1").fetchone()[0]
    assert got == VR.BAR_MINUTES == 5, (
        "la fila no declara con que unidad se resolvio: el desajuste vuelve a "
        "ser invisible mirando la base")


def test_the_migration_is_idempotent_on_an_existing_db(tmp_path, monkeypatch):
    """reader.db ya existe con su historico y CREATE TABLE IF NOT EXISTS no
    altera una tabla viva: la columna entra por migracion y abrir dos veces no
    puede romper ni perder filas."""
    c1 = VR.db()
    _insert(c1, 1_700_000_000.0, VR.DEFAULT_HORIZON_BARS)
    c2 = VR.db()                      # segunda apertura sobre la misma base
    cols = {r[1] for r in c2.execute("PRAGMA table_info(verdicts)")}
    assert "resolved_bar_min" in cols
    assert c2.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1
