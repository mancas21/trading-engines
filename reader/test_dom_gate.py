"""La condición (c) de las reglas AT-6/AT-7 (Fix 2-A) + el ledger.

Contratos que fija este fichero:
  1. JSON <-> codigo: rules.json declara dom_source="scanner_multi_venue" y el
     codigo lee EXACTAMENTE esas claves (el fallo que costo el dia entero de
     arreglos fue una regla que declaraba una cosa y evaluaba otra).
  2. N-de-M: el consenso cuenta venues del lado correcto, mas alla de la
     banda; con menos venues vivos que los pedidos es UNEVALUABLE, no FAIL.
  3. Persistencia file-backed: dos lecturas consecutivas en consenso suben la
     racha; una fuera la resetea; el scanner caido la mata.
  4. UNA observacion por ciclo del reader (fix 2026-10-15): el doble tick del
     supervisor y el observe=False de la puerta.
  5. El grabador y la puerta miden la MISMA regla (frontera del footprint).

AISLAMIENTO: los tests usaban el fichero de estado DEL GATE VIVO
(reader/dom_gate_state.json), asi que cada `pytest` sobreescribia la racha real
del reader con datos inventados (imb5 0.80/0.75/0.90 y simbolo XRPUSDT, que es
justo el que usa la suite) con carrera de escritura contra el lector en
ejecucion. El fixture autouse de abajo lo impide para TODO el fichero.
"""
import json
import pathlib
import sys
import time

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import verdict as V                          # noqa: E402

# Valor REAL del modulo, capturado ANTES de que el fixture lo relaje. Hace
# falta para los tests que verifican la proteccion contra doble tick: sin el,
# una ventana de 0s dejaria pasar justo lo que hay que impedir.
_REAL_POLL_S = V._DOM_GATE_POLL_S
_REAL_STALE_S = V._DOM_GATE_STALE_S
_PRODUCTION_STATE = HERE / "dom_gate_state.json"


@pytest.fixture(autouse=True)
def _isolated_gate_state(tmp_path, monkeypatch):
    """Ningun test toca el gate vivo, y cada llamada cuenta como observacion.

    - Estado en tmp_path: aisla la escritura del fichero del lector real.
    - _DOM_GATE_POLL_S en 0: en la vida real dos llamadas en el MISMO ciclo
      (record + notify) deben contar UNA; en un test, dos llamadas seguidas
      son dos lecturas distintas a proposit. Los dos tests que verifican
      justamente esa proteccion restauran el valor real.
    - _DOM_GATE_STALE_S NO se toca: con 0, toda llamada posterior resetearia
      la racha y la persistencia no seria testeable.
    """
    monkeypatch.setattr(V, "_DOM_GATE_STATE", tmp_path / "dom_gate_state.json")
    monkeypatch.setattr(V, "_DOM_GATE_POLL_S", 0.0)


def test_contract_json_matches_code():
    raw = json.loads((ROOT / "atas-bridge" / "rules.json").read_text(encoding="utf-8"))
    assert raw.get("namespace") == "atas"
    for rid in (6, 7):
        spec = next(r for r in raw["rules"] if r["id"] == rid)
        assert spec["conditions"]["dom_source"] == V.DOM_SOURCE, (
            "rules.json declara una fuente y el codigo implementa otra — "
            "exactamente el fallo de contrato que este test existe para detener")
        # Cada regla con LOS SUYOS: el motor ahora lee R6 para el lado sell y
        # R7 para el lado buy, asi que ambos juegos tienen que seguir iguales
        # a lo que declara CADA regla (no a lo que declara la otra).
        assert spec["conditions"]["dom_vac_thr"] == V.RULE6["vac_thr"]
        assert spec["conditions"]["dom_vac_venues_min"] == V.RULE6["vac_venues"]
        assert spec["conditions"]["dom_vac_polls_min"] == V.RULE6["vac_polls"]
    spec7 = next(r for r in raw["rules"] if r["id"] == 7)
    assert spec7["conditions"]["dom_vac_thr"] == V.RULE7["vac_thr"]
    assert spec7["conditions"]["dom_vac_venues_min"] == V.RULE7["vac_venues"]
    assert spec7["conditions"]["dom_vac_polls_min"] == V.RULE7["vac_polls"]


def test_the_suite_never_writes_the_live_gate_state():
    """Ninguna ruta de test puede coincidir con el gate del reader vivo."""
    assert pathlib.Path(V._DOM_GATE_STATE) != _PRODUCTION_STATE, (
        "un test apunta al estado REAL del gate: cada `pytest` sobreescribiria "
        "la racha del reader en ejecucion")
    assert pathlib.Path(V._DOM_GATE_STATE).parent == pathlib.Path(
        V._DOM_GATE_STATE).parent  # no crashea: la ruta es normalizable
    assert not str(V._DOM_GATE_STATE).startswith(str(HERE)), (
        "el estado de test no puede vivir en reader\\, que es donde esta el "
        "gate vivo")


def test_symbol_mapping():
    assert V._symbol_to_scanner_pair("XRPUSDT") == "XRP/USD"
    assert V._symbol_to_scanner_pair("XRP-USDT-SWAP") == "XRP/USD"
    assert V._symbol_to_scanner_pair("") is None


def test_n_of_m_counting(tmp_path, monkeypatch):
    # 4 venues: 3 con bids pesados (imb5 > thr), 1 neutro (escala +/-1)
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.80, "okx": 0.75, "kraken": 0.90, "bybit": 0.52})
    gate = V.dom_vac_eval("XRPUSDT")
    # Era `in ("MET", "UNEVALUABLE")`, que aceptaba justo el estado que este
    # test deberia excluir: con polls_min=2 la PRIMERA lectura es racha 1 de 2,
    # o sea UNEVALUABLE. Si la persistencia desapareciera y la primera
    # devolviera MET, la asercion vieja pasaba igual.
    assert gate["buy"]["state"] == "UNEVALUABLE", gate["buy"]
    assert gate["buy"]["state"] != "FAIL"       # 3 venues >= venues_min=3
    assert gate["sell"]["state"] == "FAIL"      # 0 venues del lado vendedor


def test_insufficient_venues_is_unevaluable(tmp_path, monkeypatch):
    # 2 venues vivos: el consenso pide 3 — no se puede preguntar, no es un "no"
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.90, "okx": 0.90})
    gate = V.dom_vac_eval("XRPUSDT")
    assert gate["buy"]["state"] == "UNEVALUABLE"
    assert gate["sell"]["state"] == "UNEVALUABLE"


def test_scanner_down_kills_streak_and_is_unevaluable(tmp_path, monkeypatch):
    state = V._DOM_GATE_STATE
    # racha previa
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    V.dom_vac_eval("XRPUSDT")
    V.dom_vac_eval("XRPUSDT")
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] >= 2
    # el scanner cae
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0: None)
    gate = V.dom_vac_eval("XRPUSDT")
    assert gate["buy"]["state"] == "UNEVALUABLE"
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] == 0, "la racha muere cuando muere la fuente"


def test_persistence_two_consecutive_reads(tmp_path, monkeypatch):
    state = V._DOM_GATE_STATE
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    g1 = V.dom_vac_eval("XRPUSDT")
    assert g1["buy"]["state"] == "UNEVALUABLE"   # racha 1 de 2
    g2 = V.dom_vac_eval("XRPUSDT")
    assert g2["buy"]["state"] == "MET"           # racha 2 >= polls_min
    # una lectura fuera de consenso la resetea
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.05, "okx": -0.03, "kraken": 0.10})
    g3 = V.dom_vac_eval("XRPUSDT")
    assert g3["buy"]["state"] == "FAIL"          # 0 venues del lado: un NO medido
    g4 = V.dom_vac_eval("XRPUSDT")
    assert g4["buy"]["state"] == "FAIL"          # y sigue siendo NO, no UNEVALUABLE


# ── 2026-10-15: UNA observacion por ciclo ────────────────────────────────────

def test_same_cycle_second_read_does_not_advance_the_streak(tmp_path, monkeypatch):
    """EL BUG: rules.json pide `dom_vac_polls_min: 2` = "lecturas del reader
    (~30s)", pero UN ciclo del supervisor llamaba al gate DOS veces sobre el
    MISMO snapshot del scanner — `verdict_recorder.record` (supervisor:261) y,
    si registro, el notificador via `build_verdict` (supervisor:306 -> :878).
    Las dos separadas por milisegundos y el scanner refresca cada ~12s, asi que
    la persistencia se cumplia con UNA observacion real: la condicion (c)
    disparaba con media evidencia.

    Aqui se restaura la ventana real para que el test verifique la proteccion
    y no la relajacion del fixture.
    """
    state = V._DOM_GATE_STATE
    monkeypatch.setattr(V, "_DOM_GATE_POLL_S", _REAL_POLL_S)
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    g1 = V.dom_vac_eval("XRPUSDT")     # el record del ciclo
    g2 = V.dom_vac_eval("XRPUSDT")     # el notify del MISMO ciclo, sin flag
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] == 1, (
        "la segunda llamada del mismo ciclo avanzo la racha: el doble tick "
        "esta otra vez vivo y polls_min=2 se cumple con una sola lectura")
    assert g1["buy"]["state"] == "UNEVALUABLE"
    assert g2["buy"]["state"] == "UNEVALUABLE", (
        "una sola observacion no puede satisfacer la persistencia")


def test_observe_false_never_advances_the_streak(tmp_path, monkeypatch):
    """La puerta (build_verdict) y el CLI `live_reader --text` LEEN: no son
    observaciones. Sin esto, un humano pulsando el CLI podia llevar la racha a
    MET sin que el grabador la observara nunca."""
    state = V._DOM_GATE_STATE
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    gate = V.dom_vac_eval("XRPUSDT", observe=False)
    assert gate["buy"]["state"] == "UNEVALUABLE", (
        "sin observacion previa la racha es 0 de 2, no puede ser MET")
    if state.exists():
        st = json.loads(state.read_text(encoding="utf-8"))
        assert "XRPUSDT" not in st, "observe=False escribio estado del gate"


def test_streak_does_not_survive_a_stop(tmp_path, monkeypatch):
    """El docstring del gate prometia "la racha muere" ante cualquier parada,
    pero el registro no llevaba `ts`: tras una caida de ATAS o una pausa del
    supervisor, la siguiente lectura CONTINUABA la racha y bastaba una lectura
    mas para darla por buena."""
    state = V._DOM_GATE_STATE
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    V.dom_vac_eval("XRPUSDT")
    V.dom_vac_eval("XRPUSDT")
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] == 2
    assert "ts" in st["XRPUSDT"], "la racha no lleva timestamp: no puede caducar"

    st["XRPUSDT"]["ts"] = time.time() - (_REAL_STALE_S * 5)
    state.write_text(json.dumps(st), encoding="utf-8")

    gate = V.dom_vac_eval("XRPUSDT")
    st2 = json.loads(state.read_text(encoding="utf-8"))
    assert st2["XRPUSDT"]["buy"] == 1, (
        "la racha sobrevivio a una parada de %ds: dos lecturas separadas por "
        "una caida cuentan como consecutivas" % int(_REAL_STALE_S * 5))
    assert gate["buy"]["state"] == "UNEVALUABLE"   # 1 de 2 tras el reset


# ── El grabador y la puerta miden la MISMA regla ─────────────────────────────

def test_gate_and_ledger_count_the_exact_threshold_boundary_the_same():
    """Dos clasificadores distintos: la puerta usaba `bp >= UMBRAL`, el ledger
    `bp > UMBRAL`. Un nivel en EXACTAMENTE el umbral contaba para el veredicto
    publicado y no para la fila de reader.db — y sobre esa muestra se midio y
    se puso en cuarentena la regla 6. Ahora hay UNA sola funcion."""
    import verdict_recorder as VR

    # bid=UMBral, ask=100-UMBral -> bp es EXACTAMENTE el umbral.
    lvl = [{"price": 1.0, "bid": V.FOOTPRINT_PCT, "ask": 100.0 - V.FOOTPRINT_PCT}]
    rows, sell_h, buy_h, thin = V.classify_footprint(lvl, V.FOOTPRINT_PCT)
    s2, b2, t2, _ = VR.classify_footprint(lvl)

    assert sell_h == 1, "el nivel exactamente en el umbral debe contar para la puerta"
    assert (sell_h, buy_h, thin) == (s2, b2, t2), (
        f"puerta y ledger discrepan: gate={(sell_h, buy_h, thin)} "
        f"ledger={(s2, b2, t2)}")


def test_footprint_buy_side_is_the_complement_not_a_second_threshold():
    """`buy_pct` existia en la firma, se guardaba en `_bp` y NUNCA se usaba.
    Quien lo 'arreglara' para que se usara pasaria a aceptar bid<=65 como
    compra dominante (casi todo el libro) y romperia la regla 7. El lado ask
    dominante es la IDENTIDAD 100-umbral, no una configuracion."""
    # ask dominante exactamente: bid 35 / ask 65 -> bp = 35 = 100 - 65
    lvl = [{"price": 1.0, "bid": 100.0 - V.FOOTPRINT_PCT, "ask": V.FOOTPRINT_PCT}]
    rows, sell_h, buy_h, thin = V.classify_footprint(lvl, V.FOOTPRINT_PCT)
    assert sell_h == 0
    assert buy_h == 1, "ask>=UMBral es compra dominante y no estaba contando"


# ── Los CRUDOS de (c): la puerta falla su propio criterio, hay que poder medirlo ──

def test_dom_vac_publishes_its_inputs_not_only_its_verdict(tmp_path, monkeypatch):
    """La condicion (c) FALLA el criterio pre-registrado en rules.json
    ("N=10 transiciones MET<->no-MET por dia"): medido 61-63/dia sobre 27,5 h
    reales, contra 19-23/dia del gate viejo que sustituyo. Y la causa esta
    medida: los venues no son comparables (kraken 60% de las muestras por encima
    de +0,25 y 16,9% por debajo; okx con std 0,34 y 10,6% en el lado vendedor),
    asi que un umbral ABSOLUTO mide "que venues viven fuera de la banda".

    Redisenar eso exige HORAS de datos, no una muestra de minutos. Con los crudos
    guardados en el ledger, la decision se toma offline sobre el historico real
    en vez de volver a muestrear a mano."""
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.52, "okx": -0.31, "kraken": 0.40, "bybit": 0.02})
    gate = V.dom_vac_eval("XRPUSDT")

    assert "raw" in gate, "la puerta no publica el libro por venue"
    assert gate["raw"] == {"binance": 0.52, "okx": -0.31, "kraken": 0.40, "bybit": 0.02}
    assert "counts" in gate, "la puerta no publica el recuento por lado"
    assert gate["counts"] == {"buy": 2, "sell": 1}, (
        "el recuento por lado no cuadra con los valores: 0.52 y 0.40 pasan +0.25; "
        "-0.31 pasa -0.25 (okx) y bybit 0.02 no pasa ninguna")

    # Los crudos no son el veredicto: buy tiene 2 venues y el gate pide 3.
    assert gate["buy"]["state"] == "FAIL"
