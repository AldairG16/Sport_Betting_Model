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
    # el mínimo sale del mercado: 1 / (68.7% + 3.5 pts) ≈ -260; el promedio paga -256: llega
    assert row["min_us"] == pytest.approx(-260, abs=1) and row["playdoit_odds"] == pytest.approx(1.39)
    assert row["value"] is True


def test_between_65_and_70_the_target_needs_a_price_that_reaches_the_minimum():
    """12-nov-26, el dueño (1a). En el historial, el favorito de 65 a 70 cuyo precio no llegaba al
    mínimo perdió 6.8 por cada 100 apostados en las tres épocas; de 70 para arriba no hay diferencia."""
    short = SimpleNamespace(**_up(consensus_home_odds=1.36))           # -278 no llega a -260
    pick, why = PP.decide(_mvs(), short)
    assert pick is not None and why is not None and not pick.get("strong")     # el partido sigue con su pick, sin 🎯
    assert PP.strong_favourite(PP._candidates_of(_mvs(), short), "soccer_italy_serie_a") is None
    note = PP.no_star_note(PP._candidates_of(_mvs(), short), "soccer_italy_serie_a", None, ("Napoli", "Lecce"))
    assert note == ("Napoli paga poco para lo que gana (-278): es muy favorito (69 de cada 100 según Pinnacle), pero a "
                    "ese precio, a la larga, se pierde. Vale la pena solo si PlayDoit lo paga a -260 o mejor")
    edge = SimpleNamespace(**_up(consensus_home_odds=1.385))           # justo en el mínimo (-260): llega
    assert PP.decide(_mvs(), edge)[1] is None
    # de 70 para arriba el precio no se exige: 73 de cada 100, mínimo -329, paga -357 y sigue siendo 🎯
    top = SimpleNamespace(**_up(pin_home_odds=1.30, pin_draw_odds=5.5, pin_away_odds=10.0, consensus_home_odds=1.28))
    pick, why = PP.decide(_mvs(), top)
    assert why is None and pick["strong"] and pick["market_prob"] >= PP.PRICE_BAND
    row = PP._row(_mvs(), _mvs()["match_date"], "Napoli vs Lecce", pick, why)
    assert row["value"] is False and row["min_us"] == pytest.approx(-329, abs=1)
    # la cuenta es la misma que el `value` guardado: un 🎯 de menos de 70 siempre lo trae en True
    assert PP.reaches_minimum({"playdoit_odds": 1.39, "market_prob": 0.687})
    assert not PP.reaches_minimum({"playdoit_odds": 1.36, "market_prob": 0.687})
    assert not PP.reaches_minimum({"playdoit_odds": None, "market_prob": 0.687})


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
