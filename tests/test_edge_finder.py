"""
Momios históricos y buscador de ventajas (19-oct-26).

- load_historical_odds: lee los CSV de football-data prefiriendo el cierre y
  descarta cuotas absurdas.
- edge_finder: los rasgos de cada partido usan solo partidos anteriores, y una
  idea pasa solo si gana en los tres periodos sin poder ser suerte.
"""

import numpy as np
import pandas as pd
import pytest

from scripts import load_historical_odds as LO
from src.models import edge_finder as EF


# ============================================================
# load_historical_odds
# ============================================================

def _csv(**extra):
    base = {"Div": ["E0", "E0"], "Date": ["16/08/2024", "17/08/2024"],
            "HomeTeam": ["Man United", "Ipswich"], "AwayTeam": ["Fulham", "Liverpool"],
            "FTHG": [1, 0], "FTAG": [0, 2], "HST": [5, 2], "AST": [2, 6]}
    base.update(extra)
    return pd.DataFrame(base)


def test_closing_prices_win_over_opening():
    df = _csv(PSH=[1.70, 6.0], PSD=[4.0, 4.5], PSA=[5.0, 1.5], PSCH=[1.65, 6.5], PSCD=[4.2, 4.6],
              PSCA=[5.3, 1.45], AvgH=[1.68, 5.8], AvgD=[4.0, 4.3], AvgA=[4.9, 1.5])
    rows = LO.parse_odds(df, "soccer_epl", 2024, "E0/2425")
    assert len(rows) == 2
    r = rows[0]
    assert (r["pin_home"], r["pin_closing"]) == (1.65, True)           # PSCH, no PSH
    assert (r["avg_home"], r["avg_closing"]) == (1.68, False)          # sin AvgCH: la de apertura
    assert r["home"] == "manchester united" and r["season"] == 2024
    assert (r["home_goals"], r["away_goals"], r["home_shots_target"]) == (1, 0, 5)
    assert r["max_home"] is None and r["pin_over25"] is None


def test_old_seasons_use_betbrain_average_and_bad_rows_are_dropped():
    df = _csv(PSCH=[1.0, 2.0], PSCD=[3.0, 3.0], PSCA=[4.0, 4.0], BbAvH=[1.9, 2.1], BbAvD=[3.1, 3.2],
              BbAvA=[4.0, 3.5], FTHG=[1, None])
    rows = LO.parse_odds(df, "soccer_epl", 2012, "E0/1213")
    assert len(rows) == 1                                               # sin resultado: fuera
    assert rows[0]["pin_home"] is None                                  # cuota 1.0: absurda
    assert rows[0]["avg_home"] == 1.9


def test_season_start_and_new_files():
    assert [LO.season_start(s) for s in ("2012/2013", "2025", "2021", "x")] == [2012, 2025, 2021, None]
    # códigos de mmz4281: nunca como año (el 20-oct-26 '1920' y '2021' se guardaron así)
    assert [LO.code_start(c) for c in ("1213", "1920", "2021", "2627")] == [2012, 2019, 2020, 2026]
    new = pd.DataFrame({"Country": ["Mexico"] * 2, "League": ["Liga MX"] * 2,
                        "Season": ["2011/2012", "2024/2025"], "Date": ["01/08/2011", "01/08/2024"],
                        "Home": ["America", "Toluca"], "Away": ["Cruz Azul", "Pachuca"],
                        "HG": [1, 2], "AG": [1, 0], "PSCH": [2.0, 1.8], "PSCD": [3.3, 3.5], "PSCA": [3.8, 4.4]})
    rows = LO.parse_new(new, "MEX")
    assert [r["season"] for r in rows] == [2024]                        # antes de 2012-13 no
    assert rows[0]["sport_key"] == "soccer_mexico_ligamx" and rows[0]["pin_closing"] is True
    with pytest.raises(ValueError):
        LO.parse_new(new.assign(Country="Norway"), "MEX")


# ============================================================
# edge_finder — rasgos
# ============================================================

def _match(date, home, away, hg, ag, season=2020, league="soccer_epl", hst=4, ast=4, odds=(2.0, 3.4, 3.8)):
    return {"sport_key": league, "season": season, "match_date": pd.Timestamp(date), "home": home,
            "away": away, "home_goals": hg, "away_goals": ag, "home_shots_target": hst,
            "away_shots_target": ast, "pin_home": odds[0], "pin_draw": odds[1], "pin_away": odds[2],
            "avg_home": odds[0] * 0.96, "avg_draw": odds[1] * 0.96, "avg_away": odds[2] * 0.96,
            "pin_over25": 1.9, "pin_under25": 2.0, "avg_over25": 1.85, "avg_under25": 1.95}


def test_features_only_use_previous_matches():
    dates = ["2020-08-10", "2020-08-17", "2020-08-24", "2020-08-31", "2020-09-14"]
    rows = [_match(d, "alpha", f"rival{i}", 2, 0) for i, d in enumerate(dates)]
    rows.append(_match("2020-09-18", "alpha", "beta", 0, 3))
    f = EF.build_features(pd.DataFrame(rows))
    last = f.iloc[-1]
    assert last.h_played == 5 and last.h_pts5 == 15 and last.h_scored5 == 5 and last.h_conceded5 == 0
    assert last.h_gf5 == 10 and last.h_ga5 == 0                          # goles a favor y en contra, no partidos
    assert last.h_rest == 4 and last.h_last == 2                        # 14-sep → 18-sep, ganó 2-0
    assert np.isnan(last.a_pts5) and last.a_played == 0                 # beta sin historia
    assert np.isnan(f.iloc[0].h_pts5)                                   # el primer partido no se ve a sí mismo


def test_a_leaky_defense_points_to_a_goals_bet():
    # 26-oct-26, el dueño: "si un equipo es propenso a recibir muchos goles, que también pienses en un pick de goles"
    dates = ["2020-08-10", "2020-08-17", "2020-08-24", "2020-08-31", "2020-09-14"]
    rows = [_match(d, f"rival{i}", "beta", 3, 1) for i, d in enumerate(dates)]      # beta recibe 3 por partido
    rows.append(_match("2020-09-18", "alpha", "beta", 1, 1))
    last = EF.build_features(pd.DataFrame(rows)).iloc[-1:]
    assert last.a_ga5.iloc[0] == 15 and last.a_gf5.iloc[0] == 5
    rules = {k: (side, r) for k, _, side, r in EF.HYPOTHESES}
    fires = lambda k: bool(rules[k][1](last).fillna(False).iloc[0])
    assert fires("visita_goleable") and fires("alguno_goleable") and fires("alguno_goleable_menos")
    assert not fires("local_goleable")                                  # alpha sin historia
    assert rules["alguno_goleable"][0] == "O" and rules["alguno_goleable_menos"][0] == "U"


def test_promoted_luck_and_relegation_zone():
    rows = [_match("2019-08-10", "alpha", "beta", 1, 0, season=2019)]
    rows += [_match("2020-08-10", "gamma", "alpha", 1, 1)]
    f = EF.build_features(pd.DataFrame(rows))
    assert bool(f.iloc[1].h_promoted) and not bool(f.iloc[1].a_promoted)   # gamma no estaba en 2019
    # suerte: 6 victorias 1-0 generando menos que el rival
    luck = [_match(f"2021-0{1 + i // 4}-{1 + 7 * (i % 4):02d}", "delta", f"r{i}", 1, 0, season=2021,
                   hst=1, ast=6) for i in range(7)]
    g = EF.build_features(pd.DataFrame(luck)).iloc[-1]
    assert g.h_luck6 == pytest.approx(6 * (1 - 0.3 * (1 - 6)))


# ============================================================
# edge_finder — veredicto
# ============================================================

def _season_rows(season, n, home_wins, odds=(2.0, 3.4, 3.8)):
    rows = []
    for i in range(n):
        hg, ag = (1, 0) if i < home_wins else (0, 1)
        rows.append(_match(pd.Timestamp(f"{season}-08-01") + pd.Timedelta(days=i), f"h{i}", f"a{i}",
                           hg, ag, season=season, odds=odds))
    return rows


def _frame(spec):
    rows = []
    for season, n, wins in spec:
        rows += _season_rows(season, n, wins)
    return EF.build_features(pd.DataFrame(rows))


ALL_HOMES = (("todos", "Todos los locales", "H", lambda f: f.home_goals >= 0),)


def test_an_idea_passes_only_if_it_wins_in_every_period_beyond_luck():
    # cuota promedio 1.92: hace falta ganar > 52%; aquí gana 75% en los tres periodos
    f = _frame([(2015, 400, 300), (2020, 400, 300), (2024, 400, 300)])
    ev = EF.evaluate(f, ALL_HOMES)["hypotheses"]["todos"]
    assert ev["verdict"] == EF.PASSES and all(p["roi"] > 0 for p in ev["periods"].values())
    # gana en dos periodos y pierde en uno: descartada
    f = _frame([(2015, 400, 300), (2020, 400, 150), (2024, 400, 300)])
    assert EF.evaluate(f, ALL_HOMES)["hypotheses"]["todos"]["verdict"] == EF.DISCARDED
    # apenas positiva en los tres: prometedora (puede ser suerte)
    f = _frame([(2015, 60, 33), (2020, 60, 33), (2024, 60, 33)])
    assert EF.evaluate(f, ALL_HOMES)["hypotheses"]["todos"]["verdict"] == EF.PROMISING
    # menos de 50 apuestas en un periodo: pocos datos
    f = _frame([(2015, 400, 300), (2020, 30, 25), (2024, 400, 300)])
    assert EF.evaluate(f, ALL_HOMES)["hypotheses"]["todos"]["verdict"] == EF.THIN


def test_more_ideas_need_more_evidence():
    f = _frame([(2015, 120, 70), (2020, 120, 70), (2024, 120, 70)])
    one = EF.evaluate(f, ALL_HOMES)
    many = EF.evaluate(f, ALL_HOMES * 40)
    assert many["z_needed"] > one["z_needed"]


def test_report_speaks_plainly():
    rows = []
    for season in (2015, 2020, 2024):
        rows += _season_rows(season, 80, 30)
    rep = EF.build_report(pd.DataFrame(rows))
    txt = EF.format_report(rep, html=False)
    assert "BUSCADOR DE VENTAJAS" in txt and "ninguna todavía" in txt
    assert f"{rep['k']} ideas probadas en 240 partidos" in txt
    assert EF.changed({}, rep) and not EF.changed(rep, rep)
    assert "Todavía no hay momios" in EF.format_report(EF.build_report(pd.DataFrame()))


def test_weekly_downloads_only_the_current_and_missing_seasons(monkeypatch):
    from src.utils.log import RUN_ISSUES
    RUN_ISSUES.reset()
    asked, saved = [], []
    monkeypatch.setattr(LO, "season_codes", lambda: ["2223", "2324", "2425"])
    monkeypatch.setattr(LO, "SEASON_DIVISIONS", {"E0": "soccer_epl"})
    monkeypatch.setattr(LO, "NEW_DIVISIONS", {})
    monkeypatch.setattr(LO, "_existing", lambda engine: {("soccer_epl", 2022), ("soccer_epl", 2024)})
    monkeypatch.setattr(LO, "_save", lambda engine, rows: saved.extend(rows) or
                        {"inserted": len(rows), "filled": 0, "existing": 0, "errors": 0, "first_error": None})

    def fake_fetch(url, tries=2):
        asked.append(url.split("/")[-2])
        return (_csv(), "") if "2425" in url else (None, "descarga falló: ReadTimeout")
    monkeypatch.setattr(LO, "_fetch", fake_fetch)
    r = LO.load_historical_odds(pause=0, verbose=False)
    assert asked == ["2324", "2425"]                     # 2223 ya está; la en curso se baja siempre
    assert r["files"] == 1 and len(saved) == 2
    assert r["failed"] == ["E0 2324: descarga falló: ReadTimeout"]
    assert any("Momios históricos" in e for e in RUN_ISSUES.errors())
