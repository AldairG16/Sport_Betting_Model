"""
Revalidación pre-kickoff (scripts/revalidate_pending_bets.py) con base
falsa: una corrida normal no reporta "errores tolerados".

El 23-sep-26, con el closing ya cada 30 min, cada corrida mandaba el
Telegram "closing: 1 errores tolerados" por la línea de resumen
"❌ Canceladas: 0", que se registraba con nivel ERROR.
"""

from types import SimpleNamespace

import pandas as pd

import scripts.revalidate_pending_bets as rv
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine, FakeResult

UPCOMING_COLS = ["home_odds", "draw_odds", "away_odds", "over25_odds", "under25_odds",
                 "btts_yes_odds", "btts_no_odds", "ah_home_odds", "ah_away_odds", "ah_line",
                 "dnb_home_odds", "dnb_away_odds", "dc_1x_odds", "dc_x2_odds", "dc_12_odds",
                 "h1_home_odds", "h1_draw_odds", "h1_away_odds", "h2_home_odds", "h2_draw_odds",
                 "h2_away_odds", "corners_over_odds", "corners_under_odds", "corners_line",
                 "cards_over_odds", "cards_under_odds", "cards_line"]


def _run(monkeypatch, fresh_home_odds):
    bets = pd.DataFrame([{"id": 1, "match": "alpha vs beta", "market": "home_win",
                          "probability": 0.60, "odds": 1.90}])
    row = SimpleNamespace(**{c: None for c in UPCOMING_COLS})
    row.home_odds = fresh_home_odds

    def respond(sql, params):
        if "FROM upcoming_matches" in sql:
            return FakeResult(rows=[row])
        return FakeResult()

    eng = FakeEngine(respond)
    monkeypatch.setattr(rv.pd, "read_sql", lambda sql, con=None, **kw: bets.copy())
    monkeypatch.setattr(rv, "engine", eng)
    monkeypatch.setattr(rv, "lineup_guard", lambda verbose=True: 0)
    RUN_ISSUES.reset()
    summary = rv.revalidate_pending_bets(verbose=True)
    return summary, eng


def test_normal_revalidation_reports_no_tolerated_errors(monkeypatch, capsys):
    summary, _ = _run(monkeypatch, fresh_home_odds=2.00)      # la odd mejoró
    assert summary["kept_better_odds"] == 1 and summary["cancelled"] == 0
    assert "Canceladas:          0" in capsys.readouterr().out
    assert RUN_ISSUES.errors() == []


def test_cancelled_bet_is_a_result_not_an_error(monkeypatch):
    """Cancelar una bet porque la línea absorbió el edge es el sistema
    funcionando: se anota como stale, no se reporta como fallo."""
    summary, eng = _run(monkeypatch, fresh_home_odds=1.60)    # edge 0.60 − 1/1.60 < 2%
    assert summary["cancelled"] == 1
    (sql, params), = [(s, p) for s, p in eng.executed if s.startswith("UPDATE bets_history")]
    assert "result = 'stale'" in sql and params["id"] == 1
    assert RUN_ISSUES.errors() == []
