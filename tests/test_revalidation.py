"""
Revalidación pre-kickoff (scripts/revalidate_pending_bets.py) con base
falsa: una corrida normal no reporta "errores tolerados".

El 23-sep-26, con el closing ya cada 30 min, cada corrida mandaba el
Telegram "closing: 1 errores tolerados" por la línea de resumen
"❌ Canceladas: 0", que se registraba con nivel ERROR.

Desde el 25-sep-26 la revalidación aplica también el filtro Pinnacle del
pipeline: una pendiente sin valor real frente a Pinnacle a la cuota de
ahora (o sin referencia y sin evidencia de su grupo) se cancela.
"""

import json
from types import SimpleNamespace

import pandas as pd

import scripts.revalidate_pending_bets as rv
from src.features.pinnacle import PIN_COLS
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine, FakeResult

UPCOMING_COLS = ["home_odds", "draw_odds", "away_odds", "over25_odds", "under25_odds",
                 "btts_yes_odds", "btts_no_odds", "ah_home_odds", "ah_away_odds", "ah_line",
                 "dnb_home_odds", "dnb_away_odds", "dc_1x_odds", "dc_x2_odds", "dc_12_odds",
                 "h1_home_odds", "h1_draw_odds", "h1_away_odds", "h2_home_odds", "h2_draw_odds",
                 "h2_away_odds", "corners_over_odds", "corners_under_odds", "corners_line",
                 "cards_over_odds", "cards_under_odds", "cards_line", *PIN_COLS]

PIN_VALUE = (1.55, 4.20, 6.00)       # local ≈ 62 %: a 2.00 (50 %) hay valor real
PIN_NO_VALUE = (2.00, 3.40, 3.60)    # local ≈ 47 %: a 2.00 no


def _run(monkeypatch, fresh_home_odds, pin=PIN_VALUE, recorded_pin=None, reactivated=()):
    bets = pd.DataFrame([{"id": 1, "match": "alpha vs beta", "market": "home_win",
                          "probability": 0.60, "odds": 1.90, "pin_prob": recorded_pin}])
    row = SimpleNamespace(**{c: None for c in UPCOMING_COLS})
    row.home_odds = fresh_home_odds
    if pin:
        row.pin_home_odds, row.pin_draw_odds, row.pin_away_odds = pin

    def respond(sql, params):
        if "FROM upcoming_matches" in sql:
            return FakeResult(rows=[row])
        return FakeResult()

    eng = FakeEngine(respond)
    monkeypatch.setattr(rv.pd, "read_sql", lambda sql, con=None, **kw: bets.copy())
    monkeypatch.setattr(rv, "engine", eng)
    monkeypatch.setattr(rv, "lineup_guard", lambda verbose=True: 0)
    monkeypatch.setattr(rv, "_reactivated_groups", lambda: set(reactivated))
    RUN_ISSUES.reset()
    summary = rv.revalidate_pending_bets(verbose=True)
    return summary, eng


def _decision(eng):
    (sql, params), = eng.statements("UPDATE bets_history")
    return sql, params, json.loads(params["note"])


def test_normal_revalidation_reports_no_tolerated_errors(monkeypatch, capsys):
    summary, _ = _run(monkeypatch, fresh_home_odds=2.00)      # la odd mejoró
    assert summary["kept_better_odds"] == 1 and summary["cancelled"] == 0
    assert summary["cancelled_sharp"] == 0
    assert "Canceladas:          0" in capsys.readouterr().out
    assert RUN_ISSUES.errors() == []


def test_cancelled_bet_is_a_result_not_an_error(monkeypatch):
    """Cancelar una bet porque la línea absorbió el edge es el sistema
    funcionando: se anota como stale, no se reporta como fallo."""
    summary, eng = _run(monkeypatch, fresh_home_odds=1.60)    # edge 0.60 − 1/1.60 < 2%
    assert summary["cancelled"] == 1
    sql, params, note = _decision(eng)
    assert "result = 'stale'" in sql and params["id"] == 1
    assert note["decision"] == "cancelled_line_moved"
    assert RUN_ISSUES.errors() == []


def test_bet_without_value_against_pinnacle_is_cancelled(monkeypatch):
    """El modelo ve 10 pt de edge a 2.00, pero Pinnacle da 47 %: sin valor real."""
    summary, eng = _run(monkeypatch, fresh_home_odds=2.00, pin=PIN_NO_VALUE)
    assert (summary["cancelled_sharp"], summary["kept_better_odds"]) == (1, 0)
    sql, params, note = _decision(eng)
    assert "result = 'stale'" in sql
    assert note["decision"] == "cancelled_no_value_pinnacle"
    assert abs(note["pin_prob"] - 0.47) < 0.02
    assert RUN_ISSUES.errors() == []


def test_without_reference_the_bet_waits_for_its_group_evidence(monkeypatch):
    summary, eng = _run(monkeypatch, fresh_home_odds=2.00, pin=None)
    assert summary["cancelled_sharp"] == 1
    assert _decision(eng)[2]["decision"] == "cancelled_no_reference"
    summary, _ = _run(monkeypatch, fresh_home_odds=2.00, pin=None, reactivated={"sinref:1x2"})
    assert (summary["cancelled_sharp"], summary["kept_better_odds"]) == (0, 1)


def test_the_price_recorded_with_the_bet_is_the_fallback(monkeypatch):
    """Si la fila fresca ya no trae Pinnacle, vale el precio guardado con la bet."""
    summary, _ = _run(monkeypatch, fresh_home_odds=2.00, pin=None, recorded_pin=0.62)
    assert (summary["cancelled_sharp"], summary["kept_better_odds"]) == (0, 1)
    summary, eng = _run(monkeypatch, fresh_home_odds=2.00, pin=None, recorded_pin=0.45)
    assert _decision(eng)[2]["decision"] == "cancelled_no_value_pinnacle"


def test_dry_run_decides_the_same_without_writing(monkeypatch):
    bets = pd.DataFrame([{"id": 1, "match": "alpha vs beta", "market": "home_win",
                          "probability": 0.60, "odds": 1.90, "pin_prob": None}])
    row = SimpleNamespace(**{c: None for c in UPCOMING_COLS})
    row.home_odds = 2.00
    row.pin_home_odds, row.pin_draw_odds, row.pin_away_odds = PIN_NO_VALUE
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[row]) if "FROM upcoming_matches" in sql
                     else FakeResult())
    monkeypatch.setattr(rv.pd, "read_sql", lambda sql, con=None, **kw: bets.copy())
    monkeypatch.setattr(rv, "engine", eng)
    monkeypatch.setattr(rv, "_reactivated_groups", lambda: set())
    guard = []
    monkeypatch.setattr(rv, "lineup_guard", lambda verbose=True: guard.append(1) or 0)
    summary = rv.revalidate_pending_bets(verbose=False, dry_run=True)
    assert summary["cancelled_sharp"] == 1 and summary["dry_run"] is True
    assert eng.statements("UPDATE") == [] and guard == []


def test_sharp_decision_is_the_pipeline_gate():
    from src.utils.min_odds import MIN_EDGE_TO_PLACE
    row = {"pin_home_odds": 1.55, "pin_draw_odds": 4.20, "pin_away_odds": 6.00}
    reason, p = rv.sharp_decision("home_win", row, 2.00, set())
    assert reason is None and p - 1 / 2.00 >= MIN_EDGE_TO_PLACE
    assert rv.sharp_decision("home_win", row, 1.60, set())[0] == "cancelled_no_value_pinnacle"
    # btts nunca tiene precio de Pinnacle: depende de la evidencia de su grupo
    assert rv.sharp_decision("btts_no", row, 2.00, set()) == ("cancelled_no_reference", None)
    assert rv.sharp_decision("btts_no", row, 2.00, {"sinref:btts"}) == (None, None)
