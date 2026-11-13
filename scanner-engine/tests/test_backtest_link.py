"""Tests for the backtest-system link (the validation layer behind the live engine).

Run:  python -m pytest scanner-engine/tests/test_backtest_link.py -v
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import backtest_link as BTL


def _artifact(run_id: str, symbol: str, tf: str, rows: list, *,
              fee: float = 5.0, slip: float = 0.5, rt: float = 11.0) -> dict:
    return {
        "run_id": run_id,
        "strategy": "cbpack",
        "symbol": symbol,
        "timeframe": tf,
        "created_utc": "2026-10-17T20:00:00+00:00",
        "metrics": {"fee_bps_per_side": fee, "slippage_bps_per_side": slip,
                    "cost_bp_roundtrip": rt},
        "tables": rows,
    }


def _row(id_: str, pf: float, net: float, trades: float) -> dict:
    return {"id": id_, "name": f"strategy {id_}", "status": "ok", "PF": pf,
            "net_%": net, "gross_%": net + 5.0, "excess_%": net - 2.0,
            "trades": trades, "win_%": 50.0, "sharpe": 1.0,
            "maxDD_%": -20.0, "buy_hold_%": 10.0, "exposure_%": 40.0}


def _write_root(tmp: str, artifacts: list) -> None:
    d = pathlib.Path(tmp) / "results" / "runs"
    d.mkdir(parents=True)
    for i, a in enumerate(artifacts):
        (d / f"{i:016x}.json").write_text(json.dumps(a), encoding="utf-8")


def test_missing_root_declares_absence_instead_of_raising():
    """The other repo can be moved or absent. The live engine must NOT break:
    it has to say 'no link' and keep scanning."""
    old = BTL.BACKTEST_ROOT
    try:
        BTL.BACKTEST_ROOT = pathlib.Path(r"C:\nonexistent\backtest-system-xyz")
        data = BTL.scan()
        assert data["root_exists"] is False
        assert data["artifacts"] == 0
        assert data["candidates"] == {}
        assert "error" in data
    finally:
        BTL.BACKTEST_ROOT = old


def test_candidate_must_carry_run_id_and_artifact_path():
    """REGLA 0: a number without an artifact is not a result. Every admitted
    candidate must point at the artifact it came from."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_root(tmp, [_artifact("a" * 16, "XRPUSDT", "1h",
                                     [_row("S10", 1.8, 40.0, 120)])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            data = BTL.scan()
            assert data["candidates"], "expected one admitted candidate"
            for c in data["candidates"]["XRPUSDT@1h"]:
                assert c["run_id"] == "a" * 16
                assert c["artifact"].endswith(".json")
                assert pathlib.Path(c["artifact"]).is_file()
        finally:
            BTL.BACKTEST_ROOT = old


def test_gates_reject_pf_trades_and_negative_net():
    """The repo's own validation criteria, enforced here so a weak artifact
    can never be presented as a candidate."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_root(tmp, [_artifact("b" * 16, "SOLUSDT", "1h", [
            _row("WEAK_PF", 1.10, 50.0, 200),      # PF too low
            _row("FEW_TRADES", 2.00, 50.0, 12),   # n too low
            _row("NEGATIVE", 1.90, -5.0, 200),     # net negative
            _row("GOOD", 1.60, 25.0, 150),         # admitted
        ])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            data = BTL.scan()
            got = {c["id"] for c in data["candidates"].get("SOLUSDT@1h", [])}
            assert got == {"GOOD"}, f"gates leaked: {got}"
            # the rejected rows are still COUNTED as evaluated
            assert data["scored"]["SOLUSDT@1h"]["rows_scored"] == 4
            assert data["scored"]["SOLUSDT@1h"]["rows_passed"] == 1
        finally:
            BTL.BACKTEST_ROOT = old


def test_artifact_with_other_cost_assumption_is_not_admitted():
    """A cheap-cost or zero-cost artifact must never produce a candidate:
    the repo's costs are 5 bps + 0.5 bps per side (11 bps roundtrip, measured
    2026-10-25 on live books)."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_root(tmp, [
            _artifact("c" * 16, "ETHUSDT", "1h",
                      [_row("CHEAP", 3.00, 90.0, 200)],
                      fee=5.0, slip=2.0, rt=14.0),
            _artifact("d" * 16, "ETHUSDT", "1h",
                      [_row("ZERO", 9.00, 300.0, 200)],
                      fee=0.0, slip=0.0, rt=0.0),
        ])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            data = BTL.scan()
            assert "ETHUSDT@1h" not in data["candidates"]
            assert data["other_cost_artifacts"] == 2
        finally:
            BTL.BACKTEST_ROOT = old


def test_engine_pair_alias_maps_to_backtest_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        _write_root(tmp, [_artifact("e" * 16, "SOLUSDT", "1h",
                                     [_row("S10", 1.5, 30.0, 100)])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            res = BTL.for_pair("SOL/USD")
            assert res["symbol"] == "SOLUSDT"
            assert res["has_candidate"] is True
            assert res["candidates"][0]["engine_pair"] == "SOL/USD"
            assert res["candidates"][0]["backtest_tf"] == "1h"
            # a pair with no artifact must say so, not invent anything
            empty = BTL.for_pair("JUP/USD")
            assert empty["has_candidate"] is False
            assert empty["candidates"] == []
        finally:
            BTL.BACKTEST_ROOT = old


def test_gate_rejects_row_that_loses_to_buy_and_hold():
    """AGENTS.md: winning by losing less than a collapsing market is not alpha.
    Before 2026-10-25 the module only REPORTED excess, so a row with
    excess -279pp was admitted as a candidate."""
    with tempfile.TemporaryDirectory() as tmp:
        row = _row("LOSES_TO_BH", 1.33, 13.7, 66)
        row["excess_%"] = -279.4
        _write_root(tmp, [_artifact("f" * 16, "XRPUSDT", "1h", [row])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            data = BTL.scan()
            assert "XRPUSDT@1h" not in data["candidates"]
            assert data["scored"]["XRPUSDT@1h"]["rows_passed"] == 0
        finally:
            BTL.BACKTEST_ROOT = old


def test_slice_artifacts_are_never_candidates():
    """A gates slice (slice_from/slice_to) is an 8-9 month diagnostic, not a
    validated result. It can be newer than the full-window artifact and win the
    per-id dedupe, overwriting a real number with a slice."""
    with tempfile.TemporaryDirectory() as tmp:
        art = _artifact("a1" * 8, "SOLUSDT", "1h",
                        [_row("S10", 2.5, 100.0, 55)])
        art["cmd"] = ("python code/vrun.py run --strategy cbpack --tf 1h "
                      "--symbol SOLUSDT --params '{\"slice_from\": \"2024-10-14\", "
                      "\"slice_to\": \"2025-06-15\"}'")
        art["params"] = {"slice_from": "2024-10-14", "slice_to": "2025-06-15"}
        _write_root(tmp, [art])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            data = BTL.scan()
            assert data["slice_artifacts"] == 1
            assert data["candidates"] == {}
        finally:
            BTL.BACKTEST_ROOT = old


def test_link_summary_covers_every_engine_pair():
    """The engine's /health section must be complete: a pair with no
    candidate still has to be listed, otherwise 'absent' reads as 'unknown'."""
    s = BTL.link_summary()
    for pair in BTL._PAIR_ALIAS:
        assert pair in s["pairs"], f"{pair} missing from the link summary"
    assert s["gates"]["pf_min"] == BTL.PF_MIN
    assert s["repo_costs"]["roundtrip_bps"] == BTL.REPO_COST_RT_BPS


def test_concentration_fields_travel_with_the_candidate():
    """The auto-router sizes by risk and halves it when the candidate's top-5
    trades are >100% of PnL. It reads `conc_passes` from here, so the field
    must survive the gate (it is NOT a gate: all four live candidates fail it
    and vetoing them would empty the list)."""
    with tempfile.TemporaryDirectory() as tmp:
        row = _row("S10", 1.83, 41.0, 168)
        row.update({"conc_passes": False, "conc_top5_share_pct": 132.59,
                    "conc_passes_decile": True, "funding_drag_pp": -5.72})
        _write_root(tmp, [_artifact("a2" * 8, "XRPUSDT", "1h", [row])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            c = BTL.scan()["candidates"]["XRPUSDT@1h"][0]
            assert c["conc_passes"] is False
            assert round(c["conc_top5_share_pct"], 2) == 132.59
            assert c["conc_passes_decile"] is True
            assert round(c["funding_drag_pp"], 2) == -5.72
        finally:
            BTL.BACKTEST_ROOT = old


def test_missing_concentration_fields_are_none_not_invented():
    """An artifact from before the field existed must read as 'unknown'
    (None), which the router treats as a haircut — never as a pass."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_root(tmp, [_artifact("a3" * 8, "SOLUSDT", "1h",
                                     [_row("S10", 1.6, 30.0, 150)])])
        old = BTL.BACKTEST_ROOT
        try:
            BTL.BACKTEST_ROOT = pathlib.Path(tmp)
            c = BTL.scan()["candidates"]["SOLUSDT@1h"][0]
            assert c["conc_passes"] is None
            assert c["conc_top5_share_pct"] is None
        finally:
            BTL.BACKTEST_ROOT = old
