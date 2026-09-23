"""
Corrección del historial de liquidaciones (scripts/fix_stat_settlements.py)
y el contrato de update_bankroll dentro de la transacción de una bet.
Base falsa (tests/fake_db.py): se verifica lo que se escribiría.
"""

import pandas as pd
import pytest

import scripts.fix_stat_settlements as fix
import src.models.bankroll_manager as bm
import src.models.save_bets as sb
from tests.fake_db import FakeEngine, FakeResult

CORRECTION = {"id": 3934, "match": "atalanta vs cagliari", "market": "cards_under_4.0",
              "old_result": "loss", "old_profit": -0.09,
              "new_result": "win", "new_profit": 0.0324}


def _bankroll_db(fail_bankroll=False, bet_rowcount=1):
    def respond(sql, params):
        if sql.startswith("UPDATE bankroll"):
            if fail_bankroll:
                raise RuntimeError("bankroll bloqueado")
            return FakeResult(rows=[(57.95,)])
        if sql.startswith("UPDATE bets_history"):
            return FakeResult(rowcount=bet_rowcount)
        return FakeResult()
    return FakeEngine(respond)


def test_update_bankroll_propagates_errors_inside_a_bet_transaction():
    """N3: con `conn`, el error debe llegar al savepoint del llamador para
    que revierta también la bet (antes se tragaba y la bet quedaba final)."""
    eng = _bankroll_db(fail_bankroll=True)
    with eng.begin() as conn:
        with pytest.raises(RuntimeError, match="bankroll bloqueado"):
            bm.update_bankroll(profit=0.5, notes="x", conn=conn)


def test_bet_is_not_counted_as_settled_when_the_bankroll_fails(monkeypatch, capsys):
    bets = pd.DataFrame([{"id": 1, "match": "alpha vs beta", "market": "home_win",
                          "result": "pending", "odds": 1.8, "stake": 1.0,
                          "match_date": pd.Timestamp("2026-09-20 15:00")}])
    matches = pd.DataFrame([{"home_team_l": "alpha", "away_team_l": "beta", "date": "2026-09-20",
                             "home_goals": 2, "away_goals": 1}])

    def fake_read_sql(sql, con=None, params=None, **kw):
        return bets.copy() if "FROM bets_history" in str(sql) else matches.copy()

    monkeypatch.setattr(sb.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(sb, "engine", _bankroll_db(fail_bankroll=True))
    monkeypatch.setattr(sb, "ensure_bankroll_schema", lambda: None)
    sb.update_bet_results()
    assert "✅ Updated 0 bets" in capsys.readouterr().out


def test_apply_corrections_updates_the_bet_and_adjusts_the_bankroll_by_the_difference(monkeypatch):
    eng = _bankroll_db()
    monkeypatch.setattr(fix, "engine", eng)
    monkeypatch.setattr(fix, "ensure_bankroll_schema", lambda: None)
    done = fix.apply_corrections([CORRECTION])
    assert [c["id"] for c in done] == [3934] and done[0]["balance"] == 57.95
    (sql, bet), = eng.statements("UPDATE bets_history")
    assert "AND result = :old_result" in sql                    # guarda contra doble aplicación
    assert (bet["new_result"], bet["new_profit"], bet["old_result"]) == ("win", 0.0324, "loss")
    (_, bank), = eng.statements("UPDATE bankroll")
    assert bank["profit"] == pytest.approx(0.0324 + 0.09)      # la DIFERENCIA, no el profit nuevo
    (_, hist), = eng.statements("INSERT INTO bankroll_history")
    assert hist["notes"].startswith(fix.NOTE) and "loss → win" in hist["notes"]


def test_already_corrected_bets_are_skipped_without_touching_the_bankroll(monkeypatch):
    eng = _bankroll_db(bet_rowcount=0)
    monkeypatch.setattr(fix, "engine", eng)
    monkeypatch.setattr(fix, "ensure_bankroll_schema", lambda: None)
    assert fix.apply_corrections([CORRECTION]) == []
    assert not eng.statements("UPDATE bankroll")


def test_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(fix, "audit_stat_settlements", lambda: {
        "checked": 91, "no_data": 0, "different": 1, "unverifiable": 1,
        "details": ["#3934 ..."], "corrections": [CORRECTION]})
    monkeypatch.setattr(fix, "apply_corrections", lambda c: pytest.fail("no debe escribir"))
    assert fix.main(apply=False) == 0
    out = capsys.readouterr().out
    assert "EN SECO" in out and "+0.12u" in out
