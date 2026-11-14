"""Tests del tracker SMC: BOS/CHOCH sobre velas cerradas, sin lookahead.

Los eventos de estructura son HECHOS (no predicciones), así que lo que se
prueba es la mecánica: pivotes confirmados con retardo, tendencia, prioridad
anti-tendencia (CHOCH), y niveles consumidos que no re-disparan.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import smc as SM  # noqa: E402


def bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def feed(tr, bars):
    return [e for e in (tr.update(b) for b in bars) if e is not None]


def test_bos_bull_continuation():
    tr = SM.StructureTracker("X", "15m", strength=2)
    bars = [
        bar(1, 100, 101, 99, 100),        # base
        bar(2, 100, 102, 100, 101),
        bar(3, 101, 103, 101, 102),       # SH candidato (103)
        bar(4, 102, 102.5, 100.5, 102),
        bar(5, 102, 102.4, 101, 101.5),   # confirma SH 103 (2 velas a la derecha)
        bar(6, 101.5, 103.5, 101.4, 103.2),  # cierra > 103 con trend None -> BOS bull
    ]
    evs = feed(tr, bars)
    assert [(e.kind, e.side) for e in evs] == [("BOS", "bull")]
    assert evs[0].level == 103.0 and tr.trend == "bull"


def test_pivot_has_no_lookahead():
    """El SH 103 no puede estar disponible antes de que cierren 2 velas más."""
    tr = SM.StructureTracker("X", "15m", strength=2)
    bars = [bar(1, 100, 101, 99, 100), bar(2, 100, 102, 100, 101),
            bar(3, 101, 103, 101, 102)]
    feed(tr, bars)
    assert tr.last_sh is None, "pivot confirmado sin lado derecho: lookahead"
    feed(tr, [bar(4, 102, 102.5, 100.5, 102)])
    assert tr.last_sh is None, "faltan 2 velas derechas"
    feed(tr, [bar(5, 102, 102.4, 101, 101.5)])
    assert tr.last_sh == 103.0


def test_choch_bear_reversal():
    """Tendencia bull, cierre bajo el ultimo swing low -> CHOCH bear y flip."""
    tr = SM.StructureTracker("X", "15m", strength=2)
    bars = [
        bar(1, 100, 101, 99, 100),
        bar(2, 100, 102, 100, 101),
        bar(3, 101, 103, 101, 102),
        bar(4, 102, 104, 102, 103.5),
        bar(5, 103.5, 105, 103, 104.5),    # SH cand 105
        bar(6, 104.5, 104.8, 102.5, 103),  # pullback
        bar(7, 103, 103.2, 102.4, 102.8),  # HL pivot 102.4
        bar(8, 102.8, 104, 102.9, 103.8),
        bar(9, 103.8, 104.5, 103.5, 104.2),   # SH 105 y SL 102.4 confirmados
        bar(10, 104.2, 104.8, 103.9, 104.4),
        bar(11, 104.4, 106, 104.3, 105.8),    # BOS bull rompe 105
        bar(12, 105.8, 106.2, 105, 105.5),    # SH cand 106.2
        bar(13, 105.5, 105.7, 104, 104.5),
        bar(14, 104.5, 105, 103.8, 104.8),    # SL pivot 103.8
        bar(15, 104.8, 105, 104, 105.2),
        bar(16, 105.2, 105.4, 104.5, 105),    # SL 103.8 y SH 106.2 confirmados
        bar(17, 105, 105.2, 103.5, 103.2),    # cierre < 103.8 -> CHOCH bear
    ]
    evs = feed(tr, bars)
    kinds = [(e.kind, e.side) for e in evs]
    assert kinds[0] == ("BOS", "bull")
    assert ("CHOCH", "bear") in kinds, kinds
    choch = next(e for e in evs if e.kind == "CHOCH")
    assert choch.level == 103.8 and choch.price == 103.2
    assert tr.trend == "bear"


def test_broken_level_does_not_refire():
    """El nivel roto se consume: sin pivote nuevo, no hay segundo evento."""
    tr = SM.StructureTracker("X", "15m", strength=2)
    bars = [
        bar(1, 100, 101, 99, 100),
        bar(2, 100, 102, 100, 101),
        bar(3, 101, 103, 101, 102),
        bar(4, 102, 102.5, 100.5, 102),
        bar(5, 102, 102.4, 101, 101.5),
        bar(6, 101.5, 103.5, 101.4, 103.2),  # BOS bull rompe 103
        bar(7, 103.2, 104, 103, 103.8),      # sigue subiendo, sin pivote nuevo
    ]
    evs = feed(tr, bars)
    assert len([e for e in evs if e.kind == "BOS"]) == 1


def test_old_bar_ignored():
    tr = SM.StructureTracker("X", "15m", strength=1)
    b1 = bar(10, 1, 1, 1, 1)
    assert tr.update(b1) is None
    assert tr.update(dict(b1)) is None    # ts repetido: ignorada
    assert tr.update(bar(5, 1, 1, 1, 1)) is None  # ts viejo: ignorado
