"""
tests/test_favourite_watch.py
=============================
Los supuestos del 🎯, vigilados cada semana (11-nov-26). El 🎯 descansa en que
el muy favorito gana más de lo que dice Pinnacle y en el pago anticipado; con
17 🎯 cobrados no se puede saber por ganados y perdidos si eso dejó de
cumplirse. Se mide con todos los muy favoritos analizados.
"""

import pandas as pd

from src.models import favourite_watch as F

PA = ("liga_pa",)
NOW = pd.Timestamp("2026-11-16 04:00", tz="UTC")


def _favs(n, p, wins, league="liga_pa", lead=None):
    return [{"p": p, "won": i < wins, "league": league, "lead": lead} for i in range(n)]


def test_few_favourites_have_no_verdict():
    st = F.evaluate(_favs(40, 0.75, 34), PA, NOW)
    assert st["verdict"] == "insuficiente" and st["scope"]["n"] == 40
    assert not F.changed(None, st)                                 # la primera vez, sin nada en contra: silencio


def test_favourites_that_win_as_pinnacle_says_are_no_evidence():
    st = F.evaluate(_favs(200, 0.75, 152), PA, NOW)               # 76 de cada 100 contra 75
    assert st["verdict"] == "sin evidencia" and st["scope"]["extra_pts"] == 1.0
    assert ("los muy favoritos ganan 1 más de cada 100 de lo que dice Pinnacle, pero con 200 partidos todavía puede "
            "ser suerte") in F.format_report(st)
    low = F.format_report(F.evaluate(_favs(200, 0.75, 146), PA, NOW))
    assert "ganan 2 menos de cada 100 de lo que dice Pinnacle" in low
    assert st["updated_at"].startswith("2026-11-16")


def test_favourites_winning_more_is_the_history_holding():
    st = F.evaluate(_favs(300, 0.75, 246), PA, NOW)               # 82 de cada 100
    assert st["verdict"] == "a favor" and st["scope"]["z"] >= F.Z
    text = F.format_report(st)
    assert "Favoritos de 70 de cada 100 o más: ganaron 82 de cada 100 y Pinnacle decía 75, en 300 partidos" in text
    assert "ganan más de lo que dice Pinnacle" in text and "Favoritos de 65 a 70: todavía ninguno" in text


def test_favourites_winning_less_is_told_once_and_in_plain_words():
    st = F.evaluate(_favs(300, 0.75, 204), PA, NOW)               # 68 de cada 100
    assert st["verdict"] == "en contra" and st["scope"]["z"] <= -F.Z
    assert F.changed(None, st) and F.changed({"verdict": "sin evidencia"}, st)
    assert not F.changed({"verdict": "en contra"}, st)             # ya se dijo
    text = F.format_report(st, {"verdict": "a favor"}, html=True)
    assert "<b>Veredicto</b>: los muy favoritos ganan MENOS de lo que dice Pinnacle" in text
    assert "Antes: ganan más de lo que dice Pinnacle" in text and "conviene revisar la regla" in text
    assert "ROI" not in text and "z=" not in text                  # palabras del dueño, no estadística


def test_each_band_is_measured_apart_and_other_leagues_do_not_count():
    rows = (_favs(120, 0.67, 82) + _favs(150, 0.78, 123)           # 65-70 y 70+ en ligas con pago anticipado
            + _favs(500, 0.80, 100, league="otra_liga"))           # sin pago anticipado: no entra al veredicto
    st = F.evaluate(rows, PA, NOW)
    assert st["bands"]["65-70"]["n"] == 120 and st["bands"]["70+"]["n"] == 150
    assert st["scope"]["n"] == 270 and st["other_leagues"]["n"] == 500
    assert st["verdict"] != "en contra"
    assert st["bands"]["70+"]["extra_pts"] == 4.0                  # 82 de cada 100 contra 78


def test_the_early_payout_counts_only_matches_with_a_known_lead():
    rows = (_favs(100, 0.75, 75, lead=2)                           # 25 no ganaron… y todos iban 2 arriba
            + _favs(100, 0.75, 75, lead=1) + _favs(50, 0.75, 40))  # sin llegar a 2 / sin dato de ESPN
    ep = F.evaluate(rows, PA, NOW)["early_payout"]
    assert ep == {"n": 200, "rescued": 25, "per_100": 12.5}
    assert ("en 200 partidos con la ventaja guardada, 25 veces el favorito iba 2 goles arriba y no ganó"
            in F.format_report(F.evaluate(rows, PA, NOW)))
    assert F.evaluate(_favs(120, 0.75, 90), PA, NOW)["early_payout"] == {"n": 0, "rescued": 0, "per_100": None}


def _game(day, home, away, hg, ag, hs, as_, league="liga_pa"):
    return {"league": league, "date": pd.Timestamp(day), "home": home, "away": away, "home_goals": hg,
            "away_goals": ag, "home_shots_target": hs, "away_shots_target": as_}


def test_the_price_of_the_65_to_70_favourite_is_watched_without_deciding_anything():
    plain = _favs(100, 0.67, 70) + _favs(50, 0.67, 33) + _favs(100, 0.75, 76) + _favs(10, 0.67, 7)
    paid = [{**r, "odds": 1.45, "pays": True} for r in _favs(100, 0.67, 70)]       # llegaba al mínimo
    short = [{**r, "odds": 1.36, "pays": False} for r in _favs(50, 0.67, 33)]      # no llegaba
    strong = [{**r, "odds": 1.25, "pays": False} for r in _favs(100, 0.75, 76)]    # 70+: no entra a esta cuenta
    st = F.evaluate(paid + short + strong + _favs(10, 0.67, 7), PA, NOW)           # sin pick guardado: tampoco
    assert st["price"]["pays"] == {"n": 100, "won": 70, "per_100": 1.5, "profit_50": 75}
    assert st["price"]["short"] == {"n": 50, "won": 33, "per_100": -10.2, "profit_50": -256}
    assert st["verdict"] == F.evaluate(plain, PA, NOW)["verdict"]                  # medir no cambia el veredicto
    text = F.format_report(st)
    assert ("En observación, el precio (favoritos de 65 a 70): cuando el momio estimado de PlayDoit llegaba al "
            "mínimo, 100 partidos: +75 pesos apostando 50 a cada uno; cuando no llegaba, 50 partidos: −256 pesos "
            "(en el historial, los que no llegan pierden 6.8 por cada 100 apostados)") in text
    assert "ROI" not in text


def test_goal_luck_is_watched_among_the_strong_favourites():
    calm = [{**r, "luck": -0.2} for r in _favs(60, 0.75, 50)]      # venían sin suerte: 83 de cada 100
    edge = [{**r, "luck": F.LUCK_CUT} for r in _favs(1, 0.75, 1)]  # justo en el corte: cuenta como sin suerte
    hot = [{**r, "luck": 0.8} for r in _favs(40, 0.75, 30)]        # venían con suerte: 75
    other = [{**r, "luck": 0.9} for r in _favs(20, 0.67, 10)] + _favs(5, 0.75, 5)   # 65-70 / sin dato de tiros
    st = F.evaluate(calm + hot + other, PA, NOW)
    assert st["luck"]["low"]["n"] == 60 and st["luck"]["low"]["won"] == 50
    assert st["luck"]["high"]["n"] == 40 and st["luck"]["cut"] == F.LUCK_CUT
    assert F.evaluate(calm + edge + hot, PA, NOW)["luck"]["low"]["n"] == 61
    visit = [{**r, "away": True} for r in calm[:20]] + [{**r, "away": True} for r in hot[:10]]   # el visitante, aparte
    by_side = F.evaluate(visit + calm[20:] + hot[10:], PA, NOW)["luck"]
    assert by_side["away_low"]["n"] == 20 and by_side["away_high"]["n"] == 10 and by_side["low"]["n"] == 60
    text = F.format_report(st)
    assert "los que venían sin suerte ganaron 83 de cada 100 y Pinnacle decía 75, en 60 partidos" in text
    assert "los que venían con suerte ganaron 75 de cada 100 y Pinnacle decía 75, en 40 partidos" in text
    old = {k: v for k, v in st.items() if k not in ("price", "luck")}    # un estado de antes se sigue leyendo
    assert "En observación" not in F.format_report(old)
    assert "En observación" not in F.format_report(F.evaluate(_favs(200, 0.75, 152), PA, NOW))


def test_goal_luck_is_the_goals_above_the_shots_in_the_last_ten_league_games():
    days = pd.date_range("2026-08-01", periods=12, freq="7D")
    games = [_game(d, "a", "x", 3, 0, 5, 5) for d in days]                    # a: 3 goles de más por partido
    games.append(_game("2026-11-16", "a", "x", 0, 9, 1, 1))                   # el día del partido: no cuenta
    games.append(_game("2026-08-03", "a", "x", 0, 9, 1, 1, league="otra"))    # otra liga: no cuenta
    idx = F.luck_index(games)
    assert F.luck_of(idx, "liga_pa", "a", NOW) == 3.0 and F.luck_of(idx, "liga_pa", "x", NOW) == -3.0
    shots = F.luck_index([_game(d, "a", "x", 2, 0, 8, 2) for d in days])      # 2 − 0.31 × 6 tiros de más
    assert round(F.luck_of(shots, "liga_pa", "a", NOW), 2) == 0.14
    assert F.luck_of(F.luck_index(games[:7]), "liga_pa", "a", NOW) is None    # menos de 8: sin dato
    old = [_game(d, "a", "x", 9, 0, 5, 5) for d in days[:2]] + [_game(d, "a", "x", 1, 0, 5, 5) for d in days[2:]]
    assert F.luck_of(F.luck_index(old), "liga_pa", "a", NOW) == 1.0           # solo los últimos 10
    assert F.luck_index([{**games[0], "home_shots_target": None}]) == {}      # sin tiros: no entra
    assert F.luck_index([{**games[0], "home_shots_target": float("nan")}]) == {}
    named = F.luck_index(games, normalize=lambda t: {"a": "alfa"}.get(t, t))  # mismo nombre que usa el cobro
    assert F.luck_of(named, "liga_pa", "A", NOW, normalize=lambda t: {"A": "alfa"}.get(t, t)) == 3.0


def test_the_rows_come_from_every_analysed_favourite_with_its_result(monkeypatch):
    """read_rows: el favorito es el lado con más probabilidad de Pinnacle; se cobra con la
    misma función que las apuestas y lo que no tiene resultado no cuenta. El precio sale del
    pick guardado de ese lado y la suerte de gol de los partidos de liga anteriores."""
    import src.models.early_payout as EP
    import src.models.save_bets as SB
    favs = pd.DataFrame([
        {"match": "a vs b", "match_date": NOW, "league": "liga_pa", "pin_home": 0.72, "pin_away": 0.10},
        {"match": "c vs d", "match_date": NOW, "league": "liga_pa", "pin_home": 0.12, "pin_away": 0.68},
        {"match": "e vs f", "match_date": NOW, "league": "liga_pa", "pin_home": 0.80, "pin_away": 0.07}])
    picks = pd.DataFrame([
        {"match": "a vs b", "match_date": NOW, "market": "home_win", "market_prob": 0.72, "playdoit_odds": 1.36},
        {"match": "c vs d", "match_date": NOW, "market": "home_win", "market_prob": 0.12, "playdoit_odds": 7.0},
        {"match": "c vs d", "match_date": NOW, "market": "away_win", "market_prob": 0.68, "playdoit_odds": 1.38},
        {"match": "a vs b", "match_date": NOW - pd.Timedelta(days=30), "market": "home_win", "market_prob": 0.5,
         "playdoit_odds": 9.0}])                                   # otro partido de los mismos equipos: no es este
    days = pd.date_range("2026-08-01", periods=10, freq="7D")
    history = pd.DataFrame([_game(d, "a", "x", 2, 0, 4, 4) for d in days] + [_game(d, "y", "b", 1, 1, 5, 5) for d in days]
                           + [_game(d, "c", "z", 1, 0, 3, 3) for d in days[:5]])   # c: 5 partidos, sin dato
    seen = {}

    def fake_read_sql(sql, con=None, params=None, **kw):
        q = str(sql)
        if "FROM program_picks" in q:
            seen["picks"] = params
            return picks
        if "FROM matches" in q:
            seen["history"] = params
            return history
        seen["params"] = params
        return favs

    def fake_plan(pending, matches):
        seen["markets"] = list(pending["market"])
        return [{"id": 0, "result": "win", "profit": 1.0}, {"id": 1, "result": "loss", "profit": -1.0}]   # e vs f: sin marcador

    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(SB, "_preload_matches", lambda dates: pd.DataFrame())
    monkeypatch.setattr(SB, "plan_shadow_resolutions", fake_plan)
    monkeypatch.setattr(EP, "load_leads", lambda engine, since, normalize=None: {"c vs d": [(NOW, 0, 2)]})
    monkeypatch.setattr(EP, "PA_LEAGUES", ("liga_pa",))
    rows = F.read_rows(engine=object())
    assert seen["params"] == {"d": F.WINDOW_DAYS, "p": F.STRONG}
    assert seen["markets"] == ["home_win", "away_win", "home_win"]
    assert seen["history"] == {"s": "2026-04-30", "l0": "liga_pa"}             # 200 días antes del primer partido
    assert rows == [{"p": 0.72, "won": True, "league": "liga_pa", "lead": None, "odds": 1.36, "pays": True,
                     "away": False, "luck": 2.0},                              # a: +2 por partido; b: 0
                    {"p": 0.68, "won": False, "league": "liga_pa", "lead": 2, "odds": 1.38, "pays": False,
                     "away": True, "luck": None}]                              # 1.38 × (0.68 + 0.035) no llega a 1


def test_the_weekly_runs_it_and_the_owner_hears_only_when_the_verdict_changes(monkeypatch):
    import scripts.notify_telegram as T
    import scripts.orchestrator as O
    from src.utils import output_checks as oc
    steps = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, fn, *a, **k: steps.append((name, fn)) or True)
    O.run_weekly(None)
    assert ("Supuestos del 🎯 (favoritos)", O.step_favourite_watch) in steps
    assert F.STATE_KEY in oc.WEEKLY_KEYS                           # el chequeo diario avisa si deja de correr
    sent = []
    monkeypatch.setattr(T, "send_message", lambda m: sent.append(m))
    monkeypatch.setattr(F, "load_favourite_watch", lambda: {"verdict": "a favor"})
    monkeypatch.setattr(F, "run_favourite_watch", lambda verbose=True: F.evaluate(_favs(300, 0.75, 246), PA, NOW))
    O.step_favourite_watch()
    assert sent == []                                              # sigue a favor: silencio
    monkeypatch.setattr(F, "run_favourite_watch", lambda verbose=True: F.evaluate(_favs(300, 0.75, 204), PA, NOW))
    O.step_favourite_watch()
    assert len(sent) == 1 and "ganan MENOS de lo que dice Pinnacle" in sent[0]
