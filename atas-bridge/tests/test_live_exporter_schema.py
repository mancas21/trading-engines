"""Contrato del payload del LiveExporter (schema 1 vs schema 2).

MEDIDO 2026-11-12. El exportador subio a `schema_version: 2` y anadio campos
nuevos. El contrato que importa NO es "los campos nuevos existen", es:

  1. Un payload v1 (un archivo viejo en disco, o un snapshot del backtester)
     SIGUE construyendo un Snapshot valido. Si no, el lector se rompe el dia
     que se cambia de maquina o se lee historico.
  2. Ningun campo existente cambio de SIGNIFICADO. En concreto `tape.buy_vol`
     / `tape.net_vol` / `tape.buy_count_pct` describen la ventana de 30
     prints y deben seguir describiendola; los totales reales de la barra van
     en `tape.bar_*`. Repurposar un campo en produccion rompe el track record
     en silencio: las filas viejas y las nuevas dejan de ser comparables.

  3. Los totales de la barra son la direccion; la ventana es una muestra.
     MEDIDO en 1000PEPEUSDT 2026-11-12: la ventana de 30 prints decia
     "net +1.1M BUY" mientras la barra era "net -6.6M SELL" sobre 457 prints.
     La ventana puede apuntar al reves que la barra; por eso existen los dos.

Uso:  pytest atas-bridge/tests/
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                      # atas-bridge/
sys.path.insert(0, str(ROOT))

from signal_engine import build_snapshot_from_live   # noqa: E402


# ── Payload v1: lo que escribia el exportador antes del 2026-11-12 ─────────
V1_PAYLOAD = {
    "timestamp": 1794491294040,
    "ticker": "1000PEPEUSDT",
    "bar_index": 1029,
    "price": {"last": 0.004365, "open": 0.004371, "high": 0.00438, "low": 0.004365},
    "volume": {"total": 157455468.0, "ask": 86923013.0, "bid": 70532455.0},
    "delta": 16390558.0,
    "time": "13:45:00",
    "footprint": {
        "poc": 0.004372,
        "value_area_high": 0.004376,
        "value_area_low": 0.00437,
        "top_levels": [
            {"price": 0.004372, "volume": 23393400.0, "bid": 9770514.0,
             "ask": 13622886.0, "delta": 3852372.0, "ticks": 761},
        ],
    },
    "dom": {"imbalance_pct": 5.16, "cumulative_bid_vol": 1.0, "cumulative_ask_vol": 2.0},
    "tape": {
        "trades": [{"p": 0.00437, "q": 100.0, "s": "buy"}],
        "buy_vol": 1152426.0, "sell_vol": 36491.0,
        "buy_count": 20, "sell_count": 10,
        "total_vol": 1188917.0, "net_vol": 1115935.0,
        "avg_trade_size": 39630.0,
        "largest_trade": 50000.0, "largest_trade_side": "buy",
        "largest_trade_price": 0.00437,
        "buy_count_pct": 67, "sell_count_pct": 33,
    },
}


# ── Payload v2: lo que escribe el exportador hoy ───────────────────────────
V2_PAYLOAD = {
    **V1_PAYLOAD,
    "schema_version": 2,
    "open_interest": 20899992956.0,
    "vwap": 0.00436915342434014,
    "max_delta": 4357551.0,
    "min_delta": -6090297.0,
    "max_volume_level": {"price": 0.004369, "volume": 19980681.0},
    "max_tick_level": {"price": 0.004369, "ticks": 848},
    "closed_bars": [
        {
            "bar_index": 1030, "time": "13:50:00",
            "volume": 224315282.0, "delta": -9025200.0,
            "poc": 0.004365,
            "value_area_high": 0.004373, "value_area_low": 0.004363,
            "level_count": 20, "imbalance_pct": 4.023,
            "stacked_buy_levels": 0, "stacked_sell_levels": 0,
            "levels": None,
        },
    ],
    "session_value_area": {
        "poc": 0.004376, "value_area_high": 0.004418, "value_area_low": 0.004345,
        "bars_used": 20, "total_volume": 8981058557.0,
        "covered_pct": 70.4, "target_pct": 70, "levels": 155,
    },
    "tape": {
        **V1_PAYLOAD["tape"],
        "prints_sampled": 30,
        "sample_window": "last_30_trades_of_current_bar",
        "bar_buy_vol": 3934206.0,
        "bar_sell_vol": 10537647.0,
        "bar_net_vol": -6603441.0,
        "bar_trade_count": 457,
        "bar_buy_count": 150,
        "bar_sell_count": 307,
    },
}


def test_a_v1_payload_still_builds_a_snapshot():
    """Un payload viejo no puede romper el lector."""
    snap = build_snapshot_from_live(V1_PAYLOAD)

    assert snap is not None
    assert snap.ticker == "1000PEPEUSDT"
    assert snap.price == 0.004365
    assert snap.delta == 16390558.0
    assert snap.poc == 0.004372
    assert len(snap.footprint_levels) == 1


def test_a_v1_payload_leaves_the_new_fields_at_their_defaults():
    """Ausencia = 0 / vacio, nunca una excepcion."""
    snap = build_snapshot_from_live(V1_PAYLOAD)

    assert snap.session_poc == 0.0
    assert snap.session_value_area_high == 0.0
    assert snap.session_value_area_low == 0.0
    assert snap.session_value_area_bars == 0
    assert snap.closed_bars == []
    assert snap.open_interest == 0.0
    assert snap.vwap == 0.0
    assert snap.tape_bar_buy_vol == 0.0
    assert snap.tape_bar_sell_vol == 0.0
    assert snap.tape_bar_trade_count == 0
    assert snap.tape_prints_sampled == 0


def test_a_v2_payload_maps_every_new_field():
    snap = build_snapshot_from_live(V2_PAYLOAD)

    assert snap.session_poc == 0.004376
    assert snap.session_value_area_high == 0.004418
    assert snap.session_value_area_low == 0.004345
    assert snap.session_value_area_bars == 20
    assert len(snap.closed_bars) == 1
    assert snap.closed_bars[0]["bar_index"] == 1030
    assert snap.open_interest == 20899992956.0
    assert snap.vwap == 0.00436915342434014
    assert snap.max_delta == 4357551.0
    assert snap.min_delta == -6090297.0


def test_the_tape_window_keeps_its_meaning_and_the_bar_totals_are_separate():
    """El campo viejo NO cambia de significado; los nuevos van aparte.

    Este es el test que impediria el fallo silencioso: repurposar
    `tape.net_vol` para que valga el total de la barra haria comparables dos
    filas que no lo son.
    """
    snap = build_snapshot_from_live(V2_PAYLOAD)

    # Ventana (v1) — intacta.
    assert snap.tape_buy_vol == 1152426.0
    assert snap.tape_sell_vol == 36491.0
    assert snap.tape_buy_count == 20
    assert snap.tape_sell_count == 10
    assert snap.tape_buy_count_pct == 67

    # Bar totals (v2) — aparte.
    assert snap.tape_bar_buy_vol == 3934206.0
    assert snap.tape_bar_sell_vol == 10537647.0
    assert snap.tape_bar_trade_count == 457
    assert snap.tape_prints_sampled == 30


def test_the_window_and_the_bar_can_disagree_without_being_a_bug():
    """La ventana puede apuntar al reves que la barra. Por eso existen las dos.

    Fixture medida: la ventana de 30 prints deia +1.1M net BUY y la barra era
    -6.6M net SELL. El Snapshot debe poder representar las dos cosas a la vez.
    """
    snap = build_snapshot_from_live(V2_PAYLOAD)

    assert snap.tape_buy_vol > snap.tape_sell_vol          # ventana: compradora
    assert snap.tape_bar_sell_vol > snap.tape_bar_buy_vol  # barra: vendedora
    assert snap.tape_bar_trade_count > snap.tape_prints_sampled


def test_an_empty_or_broken_payload_does_not_raise():
    """El lector no puede caer por un archivo a medias.

    Un payload VACIO devuelve None a proposito (no hay snapshot que evaluar).
    Un payload a medias devuelve un Snapshot con los campos que falten a 0.
    """
    assert build_snapshot_from_live({}) is None

    partial = build_snapshot_from_live({"ticker": "X", "tape": None})
    assert partial is not None
    assert partial.tape_buy_vol == 0
    assert partial.closed_bars == []

    partial2 = build_snapshot_from_live({"ticker": "X", "closed_bars": None,
                                         "session_value_area": None})
    assert partial2 is not None
    assert partial2.closed_bars == []
    assert partial2.session_poc == 0.0
