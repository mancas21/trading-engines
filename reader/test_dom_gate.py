"""La condición (c) nueva de las reglas AT-6/AT-7 (Fix 2-A).

Tres contratos que este fichero fija:
  1. JSON ↔ código: rules.json declara dom_source="scanner_multi_venue" y el
     código lee EXACTAMENTE esas claves (el fallo que costó el día entero de
     arreglos fue una regla que declaraba una cosa y evaluaba otra).
  2. N-de-M: el consenso cuenta venues del lado correcto, más allá de la
     banda; con menos venues vivos que los pedidos es UNEVALUABLE, no FAIL.
  3. Persistencia file-backed: dos lecturas consecutivas en consenso la racha
     sube; una lectura fuera la resetea; el scanner caído la mata.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import verdict as V                          # noqa: E402


def test_contract_json_matches_code():
    raw = json.loads((ROOT / "atas-bridge" / "rules.json").read_text(encoding="utf-8"))
    assert raw.get("namespace") == "atas"
    for rid in (6, 7):
        spec = next(r for r in raw["rules"] if r["id"] == rid)
        assert spec["conditions"]["dom_source"] == V.DOM_SOURCE, (
            "rules.json declara una fuente y el código implementa otra — "
            "exactamente el fallo de contrato que este test existe para detener")
        assert spec["conditions"]["dom_vac_thr"] == V.RULE7["vac_thr"] == V.RULE6["vac_thr"]
        assert spec["conditions"]["dom_vac_venues_min"] == V.RULE7["vac_venues"]
        assert spec["conditions"]["dom_vac_polls_min"] == V.RULE7["vac_polls"]


def test_symbol_mapping():
    assert V._symbol_to_scanner_pair("XRPUSDT") == "XRP/USD"
    assert V._symbol_to_scanner_pair("XRP-USDT-SWAP") == "XRP/USD"
    assert V._symbol_to_scanner_pair("") is None


def test_n_of_m_counting(tmp_path, monkeypatch):
    # 4 venues: 3 con bids pesados (imb5 > thr), 1 neutro (escala ±1)
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.80, "okx": 0.75, "kraken": 0.90, "bybit": 0.52})
    gate = V.dom_vac_eval("XRPUSDT")
    assert gate["buy"]["state"] in ("MET", "UNEVALUABLE"), gate["buy"]
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
    state = tmp_path / "dom_gate_state.json"
    monkeypatch.setattr(V, "_DOM_GATE_STATE", state)
    # racha previa
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0:
                        {"binance": 0.9, "okx": 0.9, "kraken": 0.9})
    V.dom_vac_eval("XRPUSDT"); V.dom_vac_eval("XRPUSDT")
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] >= 2
    # el scanner cae
    monkeypatch.setattr(V, "_scanner_book_imb5", lambda s, timeout=4.0: None)
    gate = V.dom_vac_eval("XRPUSDT")
    assert gate["buy"]["state"] == "UNEVALUABLE"
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["XRPUSDT"]["buy"] == 0, "la racha muere cuando muere la fuente"


def test_persistence_two_consecutive_reads(tmp_path, monkeypatch):
    state = tmp_path / "dom_gate_state.json"
    monkeypatch.setattr(V, "_DOM_GATE_STATE", state)
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
