"""
tests/test_big_names.py
=======================
Grandes de nombre que vienen mal (13-nov-26). El dueño pidió una "lista negra"
de equipos grandes que vienen en decadencia (Chelsea, Juventus, ManU). Se midió
y eligió (2): medir primero y avisar sin decidir. Aquí: el ranking por regla (no
una lista a mano), cómo viene cada grande, el aviso que viaja con el pick y que
ni el pick ni el 🎯 cambian. Sin DB.
"""

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from src.models import big_names as BN
from src.models import edge_finder as EF
from src.models import favourite_watch as FW
from src.models import final_alerts as FA
from src.models import program_picks as PP
from tests.fake_db import FakeEngine, FakeResult
from tests.test_program_parlays import _mvs, _up

NOW = pd.Timestamp("2026-11-13 12:00", tz="UTC")
TEAMS = ["juve", "napoli", "inter", "milan", "roma", "lazio", "atalanta", "bologna", "torino", "lecce"]


def _rep(season, last, probs, league="liga", n=30):
    """Filas del ranking: la probabilidad media de ganar de cada equipo en una temporada."""
    return [{"sport_key": league, "season": season, "last_season": last, "team": t, "p": p, "n": n}
            for t, p in probs.items()]


def _present(last, teams, league="liga"):
    return [{"sport_key": league, "season": last, "last_season": last, "team": t, "p": None, "n": 6} for t in teams]


def _season_probs(order):
    return {t: 0.70 - 0.05 * i for i, t in enumerate(order)}


def _game(day, home, away, hg, ag, league="liga"):
    return {"league": league, "date": pd.Timestamp(day), "home": home, "away": away, "home_goals": hg, "away_goals": ag}


def _state(points, league="soccer_italy_serie_a", big=("napoli", "juve", "inter", "milan")):
    return {"big": {league: {"season": 2026, "teams": sorted(big)}}, "form": {league: dict(points)}}


# ─── el ranking: una regla, no una lista a mano ─────────────────────────────

def test_the_big_names_are_the_four_that_won_most_in_the_two_previous_seasons():
    rows = (_rep(2024, 2026, _season_probs(TEAMS)) + _rep(2025, 2026, _season_probs(TEAMS))
            + _present(2026, TEAMS))
    assert BN.big_by_league(rows) == {"liga": {"season": 2026, "teams": ["inter", "juve", "milan", "napoli"]}}
    # la temporada en curso no hace el nombre: aunque lecce vaya arrasando hoy, no es un grande
    hot = rows + [{"sport_key": "liga", "season": 2026, "last_season": 2026, "team": "lecce", "p": None, "n": 12}]
    assert "lecce" not in BN.big_by_league(hot)["liga"]["teams"]
    # un ascendido (una sola temporada antes) no entra; el que ya no está en la liga, tampoco
    one = [r for r in rows if not (r["team"] == "juve" and r["season"] == 2024)]
    assert BN.big_by_league(one)["liga"]["teams"] == ["inter", "milan", "napoli", "roma"]
    gone = [r for r in rows if not (r["team"] == "napoli" and r["season"] == 2026)]
    assert BN.big_by_league(gone)["liga"]["teams"] == ["inter", "juve", "milan", "roma"]
    # temporadas con pocos partidos con precio no cuentan, y con menos de 8 equipos no hay ranking que valga
    thin = _rep(2024, 2026, _season_probs(TEAMS), n=4) + _rep(2025, 2026, _season_probs(TEAMS))
    assert BN.big_by_league(thin) == {}
    few = _rep(2024, 2026, _season_probs(TEAMS[:6])) + _rep(2025, 2026, _season_probs(TEAMS[:6]))
    assert BN.big_by_league(few) == {}
    named = BN.big_by_league(rows, normalize=lambda t: {"juve": "juventus"}.get(t, t))
    assert "juventus" in named["liga"]["teams"]                      # con el nombre que usa el resto del programa
    assert "CASE WHEN pin_home > 1" in BN.REPUTATION_SQL and "avg_home" in BN.REPUTATION_SQL   # sin Pinnacle, el promedio


def test_how_a_team_comes_is_its_points_in_its_last_ten_league_games():
    days = pd.date_range("2026-08-01", periods=12, freq="7D")
    games = [_game(d, "juve", f"r{i}", 0, 1) for i, d in enumerate(days[:2])]          # dos derrotas viejas
    games += [_game(d, "juve", f"r{i}", 1, 0) for i, d in enumerate(days[2:7])]        # 5 victorias
    games += [_game(d, f"r{i}", "juve", 1, 1) for i, d in enumerate(days[7:])]         # 5 empates de visita
    games.append(_game("2026-11-13", "juve", "rx", 0, 5))                             # el día del partido: no cuenta
    games.append(_game("2026-09-01", "juve", "rx", 5, 0, league="copa"))              # otra competencia: no cuenta
    idx = BN.points_index(games)
    assert BN.points_last(idx, "liga", "juve", NOW) == 5 * 3 + 5 * 1                  # solo los últimos 10
    assert BN.points_last(idx, "liga", "juve") == 4 * 3 + 5 * 1 + 0                   # sin fecha: hasta el último jugado
    assert BN.points_last(BN.points_index(games[:9]), "liga", "juve", NOW) is None    # menos de 10: sin dato
    assert BN.points_index([{**games[0], "home_goals": None}]) == {}                  # sin marcador: no entra
    assert BN.points_last(idx, "liga", "JUVE", NOW, normalize=str.lower) == 20


def test_each_favourite_falls_in_one_group():
    assert [BN.group_of(True, p) for p in (8, 15, 16, 19, 20, 30)] == ["poor", "poor", "normal", "normal", "good", "good"]
    assert BN.group_of(False, None) == "rest" and BN.group_of(False, 5) == "rest"
    assert BN.group_of(None, 12) is None and BN.group_of(True, None) is None           # sin ranking o sin sus 10: no se mide


# ─── el aviso: informa, no decide ───────────────────────────────────────────

def test_the_note_names_the_big_team_that_comes_badly_and_nothing_else():
    st = _state({"juve": 15, "napoli": 12, "inter": 25, "milan": 16})
    lg, shown = "soccer_italy_serie_a", ("Juventus", "Lecce")
    note = BN.form_note(st, lg, "juve", "lecce", shown)
    assert note == ("Juventus viene mal: 15 puntos de sus últimos 30 en liga. Es de los equipos grandes de su liga, "
                    "pero hoy no está ganando como grande. Es un aviso: el pick no cambia")
    assert BN.form_note(st, lg, "milan", "lecce", ("Milan", "Lecce")) is None          # 16: flojo, pero no "viene mal"
    assert BN.form_note(st, lg, "inter", "lecce", ("Inter", "Lecce")) is None          # viene bien
    assert BN.form_note(st, lg, "lecce", "torino", ("Lecce", "Torino")) is None        # ninguno es un grande
    both = BN.form_note(st, lg, "juve", "napoli", ("Juventus", "Napoli"))
    assert both.startswith("Juventus viene mal: 15") and " · Napoli viene mal: 12 puntos" in both
    assert BN.form_note(st, "soccer_usa_mls", "juve", "lecce", shown) is None          # liga sin ranking
    assert BN.form_note(None, lg, "juve", "lecce", shown) is None and BN.form_note({}, lg, "juve", "lecce") is None
    assert BN.is_big(st, lg, "juve") is True and BN.is_big(st, lg, "lecce") is False and BN.is_big(st, "otra", "juve") is None
    assert "%" not in BN.FORM_NOTE and "ROI" not in BN.FORM_NOTE


def test_the_note_travels_with_the_pick_and_changes_neither_the_pick_nor_the_target():
    st = _state({"napoli": 12})
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[(json.dumps(st),)]) if "FROM model_state" in sql else FakeResult())
    notes = BN.notes_for(eng)
    assert notes("napoli", "lecce", "soccer_italy_serie_a", ("Napoli", "Lecce")).startswith("Napoli viene mal: 12")
    mvs, up = pd.DataFrame([_mvs()]), pd.DataFrame([_up()])
    plain, = PP.build_picks(mvs, up)
    warned, = PP.build_picks(mvs, up, form_for=notes)
    assert plain["form_note"] is None and warned["form_note"].startswith("Napoli viene mal: 12 puntos")
    same = {k: v for k, v in warned.items() if k != "form_note"}
    assert same == {k: v for k, v in plain.items() if k != "form_note"}               # el pick es idéntico
    assert warned["reason"] is None and warned["star_note"] is None                   # sigue siendo 🎯
    # sin 🎯 (el precio no llega) el aviso también sale, junto a la nota de por qué no es 🎯
    low, = PP.build_picks(mvs, pd.DataFrame([_up(consensus_home_odds=1.36)]), form_for=notes)
    assert low["reason"] and low["star_note"].startswith("Napoli paga poco") and low["form_note"].startswith("Napoli viene mal")
    # sin pick no hay aviso; y si el estado no se puede leer, el pick sale igual, sin aviso
    gone, = PP.build_picks(pd.DataFrame([_mvs(skipped_at=pd.Timestamp("2030-01-05", tz="UTC"))]), up, form_for=notes)
    assert gone["market"] is None and gone["form_note"] is None

    class Broken:
        def connect(self):
            raise RuntimeError("neon caído")
    assert BN.notes_for(Broken())("napoli", "lecce", "soccer_italy_serie_a") is None
    assert "form_note" in PP._COLS and any("ADD COLUMN IF NOT EXISTS form_note" in a for a in PP.PICKS_ALTERS)


def test_the_final_pick_in_telegram_carries_the_warning_in_plain_words():
    row = {"match": "napoli vs lecce", "display": "Napoli vs Lecce", "match_date": pd.Timestamp("2026-11-08 19:00", tz="UTC"),
           "league": "soccer_italy_serie_a", "label": "Gana el local", "family": "Ganador", "market": "home_win",
           "reason": None, "star_note": None, "min_us": -260, "lineup": "confirmada", "result": None,
           "form_note": BN.FORM_NOTE.format(team="Napoli", pts=12)}
    assert FA.form_line(row) == ("⚠️ Napoli viene mal: 12 puntos de sus últimos 30 en liga "
                                 "<i>(es un aviso: el pick no cambia)</i>")
    assert FA.form_line({**row, "form_note": None}) == "" and FA.form_line({}) == ""
    from zoneinfo import ZoneInfo
    (msg,) = FA.final_texts([{"kind": "star", "row": row, "new": False, "late": False}], {}, ZoneInfo("America/Mexico_City"),
                            {"soccer_italy_serie_a"})
    assert "🎯 <b>13:00</b> Napoli vs Lecce" in msg and "   ⚠️ Napoli viene mal: 12 puntos de sus últimos 30 en liga" in msg
    assert "form_note" in FA._PICKS_SQL and "ROI" not in msg


def test_the_dashboard_shows_the_warning_with_or_without_target():
    from dashboard import board as B
    from dashboard import visibility as V
    root = Path(__file__).resolve().parent.parent
    js = (root / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")
    app = (root / "dashboard" / "app.py").read_text(encoding="utf-8")
    note = BN.FORM_NOTE.format(team="Napoli", pts=12)
    p = {"market": "home_win", "label": "Gana el local", "family": "Ganador", "model_prob": 0.7, "market_prob": 0.69,
         "playdoit_odds": 1.39, "reason": None, "lineup": None, "star_note": None, "form_note": note}
    assert V.saved_pick(p)["form_note"] == note and V.saved_pick(p)["rule"] is True
    assert V.saved_pick({**p, "form_note": None})["form_note"] == ""                    # picks de antes: sin aviso
    info = B._pick_info({**p, "match_date": "2026-11-07T15:00:00Z", "result": None, "profit": None, "source": None})
    assert info["form_note"] == note
    assert re.search(r"^function formTag\(pk\)\{", js, re.M) and "whyTag(pk) + formTag(pk) + lineupTag(" in js
    assert app.count("form_note") == 2                                                 # las dos lecturas de picks la traen


# ─── cuándo se calcula y quién vigila que siga calculándose ─────────────────

def test_the_ranking_is_rebuilt_weekly_and_the_form_every_run(monkeypatch):
    import src.utils.model_state as MS
    import src.utils.team_normalizer as TN
    monkeypatch.setattr(TN, "normalize_team", lambda t: t)           # los nombres de la prueba, tal cual
    rep = pd.DataFrame(_rep(2024, 2026, _season_probs(TEAMS)) + _rep(2025, 2026, _season_probs(TEAMS))
                       + _present(2026, TEAMS)
                       + _rep(2024, 2026, _season_probs([f"x{i}" for i in range(9)]), league="sin_partidos")
                       + _rep(2025, 2026, _season_probs([f"x{i}" for i in range(9)]), league="sin_partidos"))
    days = pd.date_range("2026-08-01", periods=10, freq="7D")
    games = pd.DataFrame([_game(d, "juve", f"r{i}", 0, 1) for i, d in enumerate(days)]          # juve: 0 puntos
                         + [_game(d, "napoli", f"s{i}", 2, 0) for i, d in enumerate(days)]      # napoli: 30
                         + [_game(d, "inter", f"t{i}", 1, 1) for i, d in enumerate(days[:4])])  # inter: solo 4 partidos
    seen, saved = [], {}

    def fake_read_sql(sql, con=None, params=None, **kw):
        q = str(sql)
        seen.append("ranking" if "FROM historical_odds" in q else "partidos")
        return rep if "FROM historical_odds" in q else games
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(MS, "load_state", lambda key, *a, **k: saved.get(key))
    monkeypatch.setattr(MS, "save_state", lambda key, value, *a, **k: saved.__setitem__(key, value) or True)
    st = BN.run_big_names(engine=object(), now=NOW, verbose=False)
    assert seen == ["ranking", "partidos"]
    assert st["big"]["liga"]["teams"] == ["inter", "juve", "milan", "napoli"] and st["form"]["liga"] == {"juve": 0, "napoli": 30}
    # de la liga que el programa sí sigue, lo dice si a un grande le faltan partidos (¿otro nombre?); de la otra, calla
    assert st["missing"] == ["inter (liga)", "milan (liga)"]
    assert BN.poor_teams(st) == [("liga", "juve", 0)]
    report = BN.format_report(st)
    assert "Vienen mal (15 puntos o menos de sus últimos 30 en liga): juve 0" in report and "inter (liga)" in report
    seen.clear()
    BN.run_big_names(engine=object(), now=NOW + pd.Timedelta(days=3), verbose=False)   # a los 3 días: solo cómo vienen
    assert seen == ["partidos"]
    seen.clear()
    BN.run_big_names(engine=object(), now=NOW + pd.Timedelta(days=8), verbose=False)   # a la semana: otra vez el ranking
    assert seen == ["ranking", "partidos"]


def test_it_runs_every_morning_and_evening_and_the_daily_check_watches_it(monkeypatch):
    from src.utils import output_checks as oc
    from tests.test_program_picks import _steps
    for fn in ("run_morning", "run_evening"):
        names, steps, O = _steps(monkeypatch, fn)
        assert steps["Grandes que vienen mal"] is O.step_big_names
        assert names.index("Grandes que vienen mal") > names.index("Picks del programa")   # el closing rehace el pick
    assert BN.STATE_KEY in oc.WEEKLY_KEYS                             # si deja de refrescarse, el chequeo diario avisa


# ─── la medición: partidos nuevos y todo el historial ───────────────────────

def test_the_weekly_watch_measures_each_group_without_deciding_anything():
    def favs(n, wins, name):
        return [{"p": 0.75, "won": i < wins, "league": "liga_pa", "lead": None, "name": name} for i in range(n)]
    rows = favs(40, 26, "poor") + favs(200, 164, "good") + favs(60, 42, "rest") + favs(30, 24, "normal") + favs(10, 9, None)
    st = FW.evaluate(rows, ("liga_pa",), NOW)
    assert st["names"]["poor"]["n"] == 40 and st["names"]["good"]["won"] == 164 and st["names"]["rest"]["n"] == 60
    plain = FW.evaluate([{k: v for k, v in r.items() if k != "name"} for r in rows], ("liga_pa",), NOW)
    assert st["verdict"] == plain["verdict"] and not FW.changed(plain, st)             # medir no cambia el veredicto
    text = FW.format_report(st)
    assert ("En observación, el nombre y cómo viene (muy favoritos): los grandes de nombre que venían mal ganaron 65 de "
            "cada 100 y Pinnacle decía 75, en 40 partidos; los que venían bien ganaron 82 de cada 100 y Pinnacle decía "
            "75, en 200 partidos; los favoritos sin gran nombre ganaron 70 de cada 100 y Pinnacle decía 75, en 60 "
            "partidos (en el historial ganan 0.8, 3.9 y 0.1 más de cada 100)") in text
    assert "el nombre y cómo viene" not in FW.format_report(plain)                    # sin datos, sin renglón


def _hist(date, home, away, hg, ag, season, odds=(2.0, 3.4, 3.8), league="soccer_epl"):
    return {"sport_key": league, "season": season, "match_date": pd.Timestamp(date), "home": home, "away": away,
            "home_goals": hg, "away_goals": ag, "home_shots_target": 4, "away_shots_target": 4,
            "pin_home": odds[0], "pin_draw": odds[1], "pin_away": odds[2], "avg_home": odds[0] * 0.96,
            "avg_draw": odds[1] * 0.96, "avg_away": odds[2] * 0.96, "pin_over25": 1.9, "pin_under25": 2.0,
            "avg_over25": 1.85, "avg_under25": 1.95}


def test_the_edge_finder_tests_the_idea_on_the_whole_history_and_cannot_let_it_pass():
    fav, even, weak = (1.30, 5.5, 11.0), (2.6, 3.3, 2.8), (4.0, 3.5, 1.9)   # el grande, 72 de cada 100; los demás, parejos
    teams = [f"t{i}" for i in range(9)]
    rows = []
    for season in (2018, 2019):                                       # dos temporadas: "big" gana siempre, a precio de grande
        base = pd.Timestamp(f"{season}-08-01")
        for i, t in enumerate(teams):
            for k in range(10):
                rows.append(_hist(base + pd.Timedelta(days=7 * k, hours=i), "big", t, 2, 0, season, odds=fav))
                rows.append(_hist(base + pd.Timedelta(days=7 * k + 3, hours=i), t, teams[(i + 1) % 9], 1, 1, season,
                                  odds=weak if t == "t8" else even))      # t8, el más débil: nunca un grande
    start = pd.Timestamp("2020-08-01")                                # la tercera: "big" pierde sus primeros 10
    rows += [_hist(start + pd.Timedelta(days=7 * k), "big", teams[k % 9], 0, 1, 2020, odds=fav) for k in range(10)]
    rows.append(_hist("2020-12-01", "big", "t8", 1, 0, 2020, odds=fav))                 # el partido a juzgar
    rows.append(_hist("2020-12-02", "t8", "t7", 1, 0, 2020, odds=fav))                  # un favorito sin gran nombre
    f = EF.build_features(pd.DataFrame(rows))
    game = f[(f.home == "big") & (f.match_date == pd.Timestamp("2020-12-01"))]
    assert game.h_big.iloc[0] == 1 and game.h_pts10.iloc[0] == 0 and game.a_big.iloc[0] == 0
    assert f[f.season == 2018].h_big.isna().all()                     # sin dos temporadas antes no hay ranking
    old = f[(f.home == "big") & (f.season == 2019)].iloc[-1]
    assert np.isnan(old.h_big) and old.h_pts10 == 30                  # 2019: aún sin ranking, pero ya se sabe cómo viene
    rules = {k: (side, r) for k, _, side, r in EF.HYPOTHESES}
    fires = lambda k, frame: bool(rules[k][1](frame).fillna(False).iloc[0])
    assert fires("grande_mal_local", game) and fires("contra_grande_mal_local", game) and not fires("grande_bien_local", game)
    assert rules["grande_mal_local"][0] == "H" and rules["contra_grande_mal_local"][0] == "A"
    plain = f[(f.home == "t8") & (f.match_date == pd.Timestamp("2020-12-02"))]
    assert fires("sin_nombre_local", plain) and not fires("grande_mal_local", plain)
    assert not fires("grande_mal_local", game.assign(sport_key="soccer_usa_mls"))     # liga sin pago anticipado
    new = {"grande_mal_local", "grande_bien_local", "sin_nombre_local", "grande_mal_visita", "grande_bien_visita",
           "contra_grande_mal_local"}
    assert new <= EF.FROM_THIS_HISTORY <= {k for k, *_ in EF.HYPOTHESES}                 # nacieron de este historial
    assert (EF.TOP_N, EF.POOR_POINTS, EF.GOOD_POINTS, EF.FORM_GAMES) == (BN.TOP_N, BN.POOR_POINTS, BN.GOOD_POINTS, BN.FORM_GAMES)


def test_the_edge_finder_says_when_the_history_stopped_bringing_pinnacle_prices():
    """13-nov-26: football-data dejó de traer el precio de Pinnacle a principios de 2026 y nadie lo había
    notado: el historial seguía creciendo, pero las ideas que dependen de ese precio ya no sumaban partidos."""
    rows = [_hist("2025-10-01", "a", "b", 1, 0, 2025), _hist("2025-10-08", "c", "d", 0, 1, 2025)]
    late = _hist("2026-10-01", "a", "c", 2, 2, 2026)
    late.update(pin_home=None, pin_draw=None, pin_away=None)
    rep = EF.build_report(pd.DataFrame(rows + [late]))
    assert rep["pinnacle_until"] == "2025-10-08" and rep["history_until"] == "2026-10-01"
    assert ("El historial trae el precio de Pinnacle solo hasta 2025-10-08: lo jugado después (hasta 2026-10-01) no "
            "entra a las ideas que dependen de ese precio.") in EF.format_report(rep, html=False)
    fresh = EF.build_report(pd.DataFrame(rows))
    assert "solo hasta" not in EF.format_report(fresh, html=False)                      # al día: no dice nada
