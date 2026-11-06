"""
El 🎯 solo al favorito fuerte con pago anticipado (v2.9.0, 6-nov-26).

La auditoría del 5-nov: el programa no le gana al mercado y la única apuesta
que no pierde al precio promedio es el favorito de 65%+ (Pinnacle sin margen
lo subestima 2.5-4 pts en 2012-2026), que con el Pago Anticipado de PlayDoit
sube 1.26 pts más. El dueño eligió (4a, 3a): 🎯 solo para él; los demás
partidos siguen con su pick, sin 🎯, y el 🎯 de goles se quita. Sin DB.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from src.models import early_payout as EP
from src.models import program_picks as PP
from tests.fake_db import FakeEngine
from tests.test_program_parlays import _mvs, _up


def test_the_target_is_the_strong_favourite_in_a_league_with_early_payout():
    pick, why = PP.decide(_mvs(), SimpleNamespace(**_up()))
    assert why is None and pick["market"] == "home_win" and pick["strong"]
    row = PP._row(_mvs(), _mvs()["match_date"], "Napoli vs Lecce", pick, why)
    # el mínimo sale del mercado: 1 / (68.7% + 3.5 pts) ≈ -261; el promedio paga -278
    assert row["min_us"] == pytest.approx(-261, abs=3) and row["playdoit_odds"] == pytest.approx(1.36)


def test_other_picks_are_still_shown_without_the_target():
    even = SimpleNamespace(**_up(pin_home_odds=2.3, pin_draw_odds=3.3, pin_away_odds=3.4,
                                 consensus_home_odds=2.2, consensus_draw_odds=3.2, consensus_away_odds=3.3))
    pick, why = PP.decide(_mvs(p_home=0.50, p_draw=0.26, p_away=0.24), even)
    assert pick is not None and why is not None                      # nunca se esconde el pick
    # el mismo favorito fuerte en la MLS (sin pago anticipado en la lista): sin 🎯
    pick, why = PP.decide(_mvs(league="soccer_usa_mls"), SimpleNamespace(**_up()))
    assert pick is not None and why is not None and not pick.get("strong")


def test_a_weakened_strong_favourite_is_not_a_target():
    weak = {"home_win": PP.WEAKENED.format(team="Napoli", n=5, names="a, b, c, d, e")}
    pick, why = PP.decide(_mvs(), SimpleNamespace(**_up()), weak=weak)
    assert why is not None and not (pick or {}).get("strong")


def test_a_pick_that_went_two_goals_up_is_settled_as_a_win(monkeypatch):
    import src.models.save_bets as sb
    eng = FakeEngine()
    kick = pd.Timestamp("2030-01-05 19:45", tz="UTC")
    pending = pd.DataFrame([{"id": 3, "match": "napoli vs lecce", "match_date": kick, "market": "home_win",
                             "league": "soccer_italy_serie_a", "playdoit_odds": 1.36}])
    matches = pd.DataFrame([{"home_team_l": "napoli", "away_team_l": "lecce", "date": kick.tz_convert(None),
                             "home_goals": 2, "away_goals": 3, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PP.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(PP, "_resolve_pre_lineup", lambda engine: 0)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    monkeypatch.setattr(EP, "load_leads", lambda *a, **k: {"napoli vs lecce": [(kick, 2, 1)]})
    assert PP.resolve_picks(engine=eng)["resolved"] == 1
    (sql, plan), = eng.statements("UPDATE program_picks SET result = :result")
    assert "pa_paid = :pa_paid" in sql
    assert plan[0]["result"] == "win" and plan[0]["pa_paid"] is True and plan[0]["profit"] == pytest.approx(0.36)
