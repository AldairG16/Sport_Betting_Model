"""
Córners y tarjetas calibrados (20-oct-26): el modelo del pipeline exageraba
(decía 15% y salía 42%); este parte del promedio de la liga, le cree solo en
parte a los equipos y usa binomial negativa en córners.
"""

import pandas as pd
import pytest

from src.models import props_fair as PF
from src.models import strategy_lab as L


def test_team_rating_needs_five_matches_and_shrinks_to_the_league():
    assert PF.team_rating([(8, 2)] * 4, 5.0) is None
    f, a = PF.team_rating([(8, 2)] * 6, 5.0)
    assert 5.0 < f < 8.0 and 2.0 < a < 5.0                       # encogido hacia la liga
    assert f == pytest.approx((6 * 8 + 6 * 5) / 12)
    recent_first = PF.team_rating([(10, 5)] + [(2, 5)] * 9, 5.0)
    assert recent_first[0] > PF.team_rating([(2, 5)] * 9 + [(10, 5)], 5.0)[0]   # lo reciente pesa más


def test_expected_total_only_partly_believes_the_teams():
    big = (8.0, 6.0)
    full = PF.expected_total(5.5, 4.5, big, big, 1.0)
    assert PF.expected_total(5.5, 4.5, big, big, 0.0) == pytest.approx(10.0)
    assert PF.expected_total(5.5, 4.5, big, big, 0.3) == pytest.approx(10 + 0.3 * (full - 10))
    assert PF.expected_total(5.5, 4.5, None, big, 0.3) == pytest.approx(10.0)   # sin datos: la liga


def test_corners_use_fatter_tails_than_poisson():
    nb = PF.prob_over(10.0, 13.5, 48.0)
    po = PF.prob_over(10.0, 13.5, None)
    assert nb > po
    assert PF.prob_over(10.0, 9.5, 48.0) + (1 - PF.prob_over(10.0, 9.5, 48.0)) == pytest.approx(1.0)


def test_a_pick_only_when_the_match_leaves_the_league_average():
    assert PF.best_pick(10.0, 10.0, "corners") is None
    pick = PF.best_pick(12.0, 10.0, "corners")
    assert pick["side"] == "más" and pick["gap"] >= PF.MIN_GAP and pick["prob"] > 0.5
    assert pick["min_american"] is not None
    low = PF.best_pick(3.0, 4.0, "cards")
    assert low["side"] == "menos"


def _hist():
    rows = []
    for i in range(40):                                          # liga: 40 partidos de 5+5 córners
        rows.append({"date": pd.Timestamp("2026-08-01") + pd.Timedelta(days=i), "league": "soccer_epl",
                     "home": f"x{i}", "away": f"y{i}", "home_corners": 5, "away_corners": 5,
                     "home_cards": 2, "away_cards": 2})
    for i in range(6):                                           # alpha saca muchos córners
        rows.append({"date": pd.Timestamp("2026-09-15") + pd.Timedelta(days=i), "league": "soccer_epl",
                     "home": "alpha", "away": f"z{i}", "home_corners": 11, "away_corners": 3,
                     "home_cards": 1, "away_cards": 1})
    for i in range(6):                                           # beta: equipo normal
        rows.append({"date": pd.Timestamp("2026-09-15") + pd.Timedelta(days=i), "league": "soccer_epl",
                     "home": "beta", "away": f"w{i}", "home_corners": 5, "away_corners": 5,
                     "home_cards": 2, "away_cards": 2})
    rows.append({"date": pd.Timestamp("2026-10-10"), "league": "soccer_epl", "home": "alpha", "away": "gamma",
                 "home_corners": 30, "away_corners": 0, "home_cards": 9, "away_cards": 9})   # el propio partido
    return pd.DataFrame(rows).sort_values("date", ascending=False).reset_index(drop=True)


def test_match_props_only_sees_the_past():
    hist = _hist()
    both = PF.match_props(hist, "alpha", "beta", "soccer_epl", "2026-10-10")["corners"]
    assert both["teams_known"] and both["mu"] > both["league_mu"]         # alpha saca más córners
    mp = PF.match_props(hist, "alpha", "gamma", "soccer_epl", "2026-10-10")
    c = mp["corners"]
    assert c["league_mu"] == pytest.approx((40 * 10 + 6 * 14 + 6 * 10) / 52)   # la liga incluye a alpha y beta
    assert c["mu"] == pytest.approx(c["league_mu"]) and c["teams_known"] is False   # beta sin historia: la liga
    known = PF.match_props(hist, "alpha", "x1", "soccer_epl", "2026-10-10")["corners"]
    assert known["teams_known"] is False                                 # x1 jugó 1 partido: tampoco alcanza
    # el resultado del propio partido (30 córners) no entra
    assert len(PF.team_history(hist, "alpha", "corners", "2026-10-10")) == 6
    assert PF.match_props(hist, "alpha", "gamma", "soccer_mexico_ligamx", "2026-10-10") == {}   # sin liga, sin precio


def test_lab_judges_corners_and_cards_with_results(monkeypatch):
    shadow = pd.DataFrame([
        {"match": "alpha vs beta", "match_date": "2026-10-11", "league": "soccer_epl",
         "market": m, "odds": o, "result": r, "profit": (o - 1) if r == "win" else -1.0}
        for m, o, r in (("corners_over_9.5", 1.90, "win"), ("corners_under_9.5", 1.90, "loss"),
                        ("cards_over_4.5", 2.00, "loss"), ("btts", 1.80, "win"))])
    monkeypatch.setattr(PF, "load_history", lambda engine=None, days=PF.HISTORY_DAYS: _hist())
    rows = L.read_props(shadow)
    assert sorted(rows["market"]) == ["cards_over_4.5", "corners_over_9.5", "corners_under_9.5"]
    over = rows.set_index("market")["p_model"]
    assert over["corners_over_9.5"] + over["corners_under_9.5"] == pytest.approx(1.0)
    lab = L.props_lab(rows)
    assert lab["roi"]["verdict"] == "insuficiente"
    picked = rows[rows["p_model"] - 1 / rows["odds"] >= L.PROPS_MIN_EDGE]
    assert lab["candidates"] == len(picked) and lab["results"]["played"] == len(picked)
    text = "\n".join(L.format_lines({"strategies": {}, "kalshi": {}, "props": lab}, html=False))
    assert "Córners y tarjetas" in text and "faltan datos" in text
    assert L.props_lab(pd.DataFrame()) == {}
