"""El bloque `gates` de rules.json: que deje de poder divergir en silencio.

MEDIDO 2026-10-15. `scanner-engine/rules.json` declara un bloque `gates` con
valores OPERATIVOS:

    "max_age_s": 300,
    "min_history_bars": 60,
    "default_min_venues": 3,
    "min_venues_is_per_pair": true,
    "gate_signals": false

y NINGUN modulo del scanner lo lee (`grep gates scanner-engine/*.py` no
encuentra nada; el unico acierto es una frase del docstring de features.py).

Que los valores coincidan HOY no basta: hay TRES copias de `max_age_s` (el JSON,
poller.DEFAULT_MAX_AGE_S y scanner.DEFAULT_MAX_AGE_S) y nada que las mantenga
juntas. Quien cambie el JSON esperando que surta efecto no vera nada pasar — que
es la clase de trampa que este repositorio ya pago varias veces (un umbral
declarado que el motor no aplica).

No se hace que el codigo LEA el JSON: `poller` es un modulo de bajo nivel y
meterle una dependencia de fichero de configuracion para un valor por defecto que
hoy nadie cambia es peor remedio que enfermedad. Lo que SI se hace es lo que
faltaba: un test que falle en cuanto las copias diverjan, y la nota en el propio
JSON diciendo que esta guardado.

Uso:  pytest scanner-engine/tests/test_rules_gates.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                      # scanner-engine/
sys.path.insert(0, str(ROOT))

import poller as P                      # noqa: E402
import scanner as S                     # noqa: E402
from pairs import PAIRS                 # noqa: E402


def _gates() -> dict:
    raw = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))
    assert "gates" in raw, "rules.json perdio el bloque gates"
    return raw["gates"]


def test_max_age_s_has_one_meaning_across_all_three_copies():
    """300 esta escrito en el JSON, en poller y en scanner. Si divergen, el
    JSON manda en la documentacion y el codigo en la realidad — y nadie se
    entera."""
    g = _gates()
    assert g["max_age_s"] == P.DEFAULT_MAX_AGE_S, (
        f"rules.json declara max_age_s={g['max_age_s']} y poller usa "
        f"{P.DEFAULT_MAX_AGE_S}")
    assert g["max_age_s"] == S.DEFAULT_MAX_AGE_S, (
        f"rules.json declara max_age_s={g['max_age_s']} y scanner usa "
        f"{S.DEFAULT_MAX_AGE_S}")


def test_the_venue_floor_is_per_pair_and_never_below_the_declared_default():
    """El JSON declara `default_min_venues: 3` + `min_venues_is_per_pair: true`.
    El registro real vive en pairs.py; esto comprueba que la politica declarada
    y los numeros no se han separado.

    Un par por DEBAJO del default seria un par que la puerta evalua con menos
    venues de los que el fichero dice tolerar."""
    g = _gates()
    assert g["min_venues_is_per_pair"] is True
    floors = {name: spec.min_venues for name, spec in PAIRS.items()}
    assert floors, "el registro de pares esta vacio"
    lowest = min(floors.values())
    assert lowest == g["default_min_venues"], (
        f"el suelo mas bajo del registro es {lowest} y el JSON declara "
        f"default_min_venues={g['default_min_venues']}: {floors}")
    for name, n in floors.items():
        assert n >= g["default_min_venues"], (
            f"{name} tiene suelo {n}, por debajo del default declarado "
            f"{g['default_min_venues']}")


def test_audited_rules_match_the_declared_statuses():
    """`gate_signals: false` con su nota: "No rule may suppress a signal until
    the Phase 4 auditor has measured it... Four rules that have never been
    audited must not be allowed to gate anything."

    Ojo con la lectura: `audited_rules` son las MEDIDAS, no las buenas. Hoy
    devuelve ['1', '2'] porque esas dos estan REFUTADAS (DROP, t=-11.45 y
    -14.53) — una medida a DROP es una medida, y por eso queda RETIRADA en vez
    de gatear. Reglas 3 y 4 siguen UNVALIDATED.

    Lo que este test fija es que el fichero y el motor digan lo MISMO sobre
    quien ha sido medido: es la clase de contrato que en este repositorio ha
    divergido en silencio mas de una vez."""
    raw = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))
    g = raw["gates"]
    assert g["gate_signals"] is False

    measured = {rid for rid, s in raw["rules"].items()
                if not str(s.get("audit_status", "UNVALIDATED"))
                .upper().startswith("UNVALIDATED")}
    sc = S.Scanner(pairs=["ETH/USD"])
    assert set(sc.health()["audited_rules"]) == measured, (
        "lo que el motor llama 'auditada' no cuadra con los audit_status del "
        "fichero: alguien cambio uno de los dos")

    # Y lo medido a DROP tiene que estar retirado, no gateando.
    dropped = {rid for rid, s in raw["rules"].items()
               if str(s.get("audit_status", "")).upper().startswith("DROP")}
    for rid in dropped:
        assert str(raw["rules"][rid]["audit_status"]).upper().startswith("DROP"), rid
    if dropped:
        assert set(sc.health()["retired_rules"]) >= dropped, (
            "hay reglas medidas a DROP que el motor no da por retiradas: "
            "seguirian gastando celdas del auditor y pudiendo producir "
            "direccion")


def test_min_history_bars_is_declared_and_reachable():
    """El JSON declara cuanta historia minima hace falta. No se exige que el
    codigo lo lea (igual que max_age_s), pero SI que exista un camino de
    features que acumule historia: si no, el umbral describe algo que nunca
    ocurre."""
    g = _gates()
    assert isinstance(g["min_history_bars"], int) and g["min_history_bars"] > 0
    import features as F
    assert hasattr(F, "DEFAULT_MIN_COVERAGE_S"), (
        "features.py perdio su constante de cobertura minima: el umbral de "
        "historia del JSON ya no describe nada comprobable")
