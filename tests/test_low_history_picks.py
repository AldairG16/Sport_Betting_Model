"""
Pick aunque haya poca historia (24-oct-26, el dueño: "aunque tenga poca
historia o poco análisis o no cumpla, yo quiero ver el pick, con una breve
descripción"), y los partidos de hoy que ya empezaron a la vista.

Un partido con menos de 5 partidos de un equipo, o con Dixon-Coles por debajo
del 50%, se analiza igual y se guarda marcado (model_vs_sharp.low_history):
su pick se ve, nunca 🎯, con el motivo; no apuesta, no aprende el sesgo y no
cuenta en los aciertos del dashboard.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import board as B
from src.models import program_picks as PK

ROW = {"match": "lommel sk vs leuven", "match_date": "2026-10-25 14:00+00:00", "league": "soccer_belgium_first_div",
       "p_home": 0.40, "p_draw": 0.27, "p_away": 0.33, "pin_home": 0.36, "pin_draw": 0.28, "pin_away": 0.36,
       "lam_home": 1.4, "lam_away": 1.2, "p_over25": 0.52, "p_btts": 0.55, "league_factor": None,
       "skipped_at": None, "created_at": "2026-10-24 12:00+00:00"}
UP = SimpleNamespace(pin_home_odds=2.70, pin_draw_odds=3.50, pin_away_odds=2.70, home_odds=2.80, draw_odds=3.6,
                     away_odds=2.8, consensus_home_odds=2.6, consensus_draw_odds=3.4, consensus_away_odds=2.6,
                     consensus_over25_odds=1.95, consensus_under25_odds=1.85, over25_odds=2.0, under25_odds=1.9,
                     btts_yes_odds=1.8, btts_no_odds=2.0, pin_total_line=None, pin_total_over_odds=None,
                     pin_total_under_odds=None, corners_line=None, cards_line=None)


def test_a_low_history_match_still_gets_a_pick_with_its_reason():
    normal, why_normal = PK.decide(dict(ROW), UP)
    low, why = PK.decide({**ROW, "low_history": "lommel sk con 10 partidos en la base"}, UP)
    assert low is not None and low["market"] == normal["market"]                   # el mismo pick…
    assert why.startswith(PK.LOW_HISTORY) and "lommel sk con 10" in why            # …nunca 🎯, con el motivo
    assert PK.decide({**ROW, "low_history": float("nan")}, UP) == (normal, why_normal)


def _pick(match, when, **kw):
    return {"match": match, "match_date": pd.Timestamp(when, tz="UTC"), "league": "soccer_epl", "label": "Ambos anotan",
            "market": "btts", "family": "Ambos anotan", "reason": None, "model_prob": 0.6, "market_prob": 0.55,
            "result": None, "profit": None, "source": None, **kw}


def test_matches_that_already_started_show_up_without_a_score():
    """24-oct-26: los partidos de hoy de las 5 grandes no estaban ni en Próximos
    (ya empezaron) ni en Anteriores (el marcador llega en la noche)."""
    now = pd.Timestamp("2026-10-24 19:40", tz="UTC")
    picks = pd.DataFrame([_pick("chelsea vs tottenham", "2026-10-24 16:30"),
                          _pick("arsenal vs everton", "2026-10-26 14:00")])           # todavía no empieza: no
    mvs = pd.DataFrame([{"match": "chelsea vs tottenham", "match_date": pd.Timestamp("2026-10-24 16:30", tz="UTC"),
                         "p_home": 0.53, "p_draw": 0.24, "p_away": 0.23, "pin_home": 0.53, "pin_draw": 0.23,
                         "pin_away": 0.24, "low_history": None}])
    rows = B.past_matches(pd.DataFrame(), mvs, picks, pd.DataFrame(), normalize=lambda t: str(t).lower(), now=now)
    assert [r["key"] for r in rows] == ["chelsea vs tottenham"]
    r = rows[0]
    assert r["score"] is None and r["started"] and r["date"] == "2026-10-24"
    assert r["pick"]["label"] == "Ambos anotan" and r["pick"]["result"] == "pending"
    assert r["fav"]["model"] == "home" and r["fav"]["model_hit"] is None
    s = B.summary(rows)
    assert s["matches"] == 0 and s["analyzed"] == 0 and s["picks_fixed"]["n"] == 0
    assert B.weekly_accuracy(rows) == []


def test_low_history_picks_are_shown_but_not_counted():
    matches = pd.DataFrame([{"date": pd.Timestamp("2026-10-24"), "league": "soccer_belgium_first_div",
                             "h": "lommel sk", "a": "leuven", "home_goals": 1, "away_goals": 0, "home_corners": 5,
                             "away_corners": 4, "home_yellow": 1, "away_yellow": 2, "home_red": 0, "away_red": 0}])
    mvs = pd.DataFrame([{"match": "lommel sk vs leuven", "match_date": pd.Timestamp("2026-10-24 14:00", tz="UTC"),
                         "p_home": 0.4, "p_draw": 0.27, "p_away": 0.33, "pin_home": 0.36, "pin_draw": 0.28,
                         "pin_away": 0.36, "low_history": "lommel sk con 10 partidos"}])
    picks = pd.DataFrame([_pick("lommel sk vs leuven", "2026-10-24 14:00", label="Gana el local", market="home_win",
                                family="Ganador", result="win", profit=1.5,
                                reason=f"{PK.LOW_HISTORY}: lommel sk con 10 partidos")])
    rows = B.past_matches(matches, mvs, picks, pd.DataFrame(), normalize=lambda t: str(t).lower())
    r = rows[0]
    assert r["pick"]["low"] and r["fav"]["low"] and r["pick"]["label"] == "Gana el local"   # se ve
    s = B.summary(rows)
    assert s["matches"] == 1 and s["analyzed"] == 0 and s["picks_fixed"]["n"] == 0           # no cuenta


def test_low_history_rows_do_not_teach_the_side_bias(monkeypatch):
    import src.models.side_bias as SB
    import config.database as db
    seen = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return None
    monkeypatch.setattr(db, "engine", SimpleNamespace(begin=lambda: Conn()))
    monkeypatch.setattr(SB.pd, "read_sql", lambda sql, con=None, params=None: seen.append(str(sql)) or pd.DataFrame())
    SB.read_pairs()
    assert "low_history IS NULL" in seen[0] and "skipped_at IS NULL" in seen[0]


def test_a_rescheduled_match_does_not_show_up_as_started():
    """24-oct-26: Toulouse vs Troyes pasó del 24 al 25; la fecha vieja salía como empezada."""
    now = pd.Timestamp("2026-10-24 19:40", tz="UTC")
    picks = pd.DataFrame([_pick("toulouse vs troyes", "2026-10-24 15:15", market=None, label=None,
                                reason=PK.STALE),
                          _pick("toulouse vs troyes", "2026-10-25 16:15")])
    rows = B.past_matches(pd.DataFrame(), pd.DataFrame(), picks, pd.DataFrame(),
                          normalize=lambda t: str(t).lower(), now=now)
    assert rows == []


def test_the_market_view_shows_every_market_the_program_weighed():
    """26-oct-26 (LAFC vs Galaxy): el dueño quería ver por qué el pick fue el local y
    no ambos anotan. La vista enseña todos los mercados, de la más a favor a la más
    en contra, con los mismos candidatos que el pick."""
    view = PK.market_view(dict(ROW), UP)
    assert [v["gap"] for v in view] == sorted((v["gap"] for v in view), reverse=True)
    labels = {v["label"] for v in view}
    assert {"Gana el local", "Empate", "Gana el visitante", "Más de 2.5 goles", "Ambos anotan"} <= labels
    pick, _ = PK.decide(dict(ROW), UP)
    top = next(v for v in view if v["market"] == pick["market"])
    assert top["gap"] == pytest.approx(pick["model"] - pick["market_prob"], abs=1e-3)
    assert PK.market_view({**ROW, "p_home": None}, UP) == []
