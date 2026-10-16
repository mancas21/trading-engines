"""La tabla de cobertura no puede anunciar una regla retirada.

Medido 2026-10-16. `_IMPLEMENTED[4]` declaraba "scorecard() + SCORE_BANDS" y el
propio fichero documenta cien lineas mas abajo (bloque del scorecard) que
NINGUNA ruta de produccion llama a esas funciones: se retiraron en a93147f tras
medirse sin poder predictivo (pearson +0.0175, spearman -0.0156 sobre 1.205
filas con forward_return resuelto), y rules.json los marca `enforcement:
disabled`.

No era inocuo: `rule_coverage` es lo que el sistema publica para declarar QUE
reglas aplica. Con la 4 listada como implementada, la tabla anunciaba una puerta
que no existe y `rules_not_implemented` escondia su numero.

Contratos que fija este fichero:
  1. La 4 NO figura como implementada y su motivo nombra la retirada.
  2. Las reglas de rules.json aparecen exactamente una vez en la tabla.
  3. Las anclas vivas (6, 7 y 8) siguen declaradas implementadas: el test no
     puede pasar vaciando la tabla.
  4. Ningun `how` de una regla implementada cita el scorecard retirado.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import verdict as V       # noqa: E402


def _cov():
    return {int(r["id"]): r for r in V._rule_coverage()}


def _rules_json_ids():
    raw = json.loads((ROOT / "atas-bridge" / "rules.json")
                     .read_text(encoding="utf-8"))
    return sorted(int(r["id"]) for r in raw.get("rules", []))


def test_la_4_retirada_no_se_anuncia_como_implementada():
    cov = _cov()
    assert 4 in cov, "la 4 desaparecio de la tabla en vez de declararse no implementada"
    assert cov[4]["implemented"] is False
    how = cov[4]["how"].lower()
    assert "retirad" in how or "disabled" in how, (
        "el motivo de la 4 no nombra la retirada: " + cov[4]["how"])
    assert cov[4]["how"] != "scorecard() + SCORE_BANDS", (
        "la 4 volvio a declararse implementada con la afirmacion retirada")
    assert "ninguna ruta" in how, (
        "el motivo tiene que negar el uso en produccion: " + cov[4]["how"])


def test_cada_regla_de_rules_json_aparece_una_vez():
    assert sorted(_cov()) == _rules_json_ids()


def test_las_anclas_vivas_siguen_declaradas():
    cov = _cov()
    for rid in (6, 7):
        assert cov[rid]["implemented"] is True, f"la regla {rid} dejo de declararse"
        assert "rules.json" in cov[rid]["how"], (
            f"la regla {rid} ya no cita de donde salen sus umbrales")
    assert cov[8]["implemented"] is True
    assert "_audit_record_loss" in cov[8]["how"]


def test_ninguna_regla_implementada_cita_el_scorecard_retirado():
    for rid, r in _cov().items():
        if r["implemented"]:
            assert "scorecard" not in r["how"].lower(), (
                f"la regla {rid} declara su implementacion via el scorecard retirado")
