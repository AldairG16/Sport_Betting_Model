"""
Apuestas 'stale' de un partido pospuesto (2-nov-26): Mjällby vs Djurgården
del 31-may se jugó el 3-sep, y el weekly la reportaba cada lunes como
"❌ Sin datos fuente aún · partido no está en matches". Sin DB.
"""

import pandas as pd

import scripts.resolve_stale_bets as rsb


def _rows(*games):
    return pd.DataFrame([{"home_l": h, "away_l": a, "home_c": h, "away_c": a, "date": pd.Timestamp(d)}
                         for h, a, d in games], columns=["home_l", "away_l", "home_c", "away_c", "date"])


def _world(monkeypatch, window, later):
    bets = pd.DataFrame([{"id": 3562, "match": "mjallby aif vs djurgardens if", "market": "home_win",
                          "match_date": pd.Timestamp("2026-05-31 14:30"), "league": "soccer_sweden_allsvenskan"}])
    asked = []

    def read_sql(sql, con=None, params=None, **kw):
        s = str(sql)
        asked.append(params)
        if "FROM bets_history" in s:
            return bets
        if "home_goals IS NOT NULL" in s:                 # la fecha nueva de un pospuesto
            return later
        return window
    monkeypatch.setattr(rsb.pd, "read_sql", read_sql)
    return asked


def test_a_postponed_match_is_reported_as_such_not_as_missing(monkeypatch, capsys):
    asked = _world(monkeypatch, _rows(("ifk goteborg", "mjallby aif", "2026-05-25")),
                   _rows(("mjallby aif", "djurgardens if", "2026-09-03")))
    out = rsb.resolve_stale_bets(apply=False)
    assert out == {"total": 1, "recovered": 0, "still_stale": 0, "postponed": 1}
    printed = capsys.readouterr().out
    assert "se jugó el 03-Sep" in printed and "no está en matches" not in printed
    assert asked[-1]["d_from"] == "2026-06-02"                  # más de 2 días después


def test_a_match_with_no_later_date_is_still_missing(monkeypatch, capsys):
    _world(monkeypatch, _rows(), _rows(("otro", "equipo", "2026-09-03")))
    out = rsb.resolve_stale_bets(apply=False)
    assert out["still_stale"] == 1 and out["postponed"] == 0
    assert "partido no está en matches: 1" in capsys.readouterr().out


def test_same_pair_matches_the_cleaned_name():
    rows = pd.DataFrame([{"home_l": "nott'm forest", "away_l": "everton", "home_c": "nott'm forest",
                          "away_c": "everton", "date": pd.Timestamp("2026-10-04")}])
    assert len(rsb.same_pair(rows, "nott'm forest", "everton")) == 1
    assert rsb.same_pair(rows, "everton", "nott'm forest").empty
    assert rsb.same_pair(_rows(), "a", "b").empty
