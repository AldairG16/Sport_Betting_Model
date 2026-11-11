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


def test_the_rows_come_from_every_analysed_favourite_with_its_result(monkeypatch):
    """read_rows: el favorito es el lado con más probabilidad de Pinnacle; se cobra con la
    misma función que las apuestas y lo que no tiene resultado no cuenta."""
    import src.models.early_payout as EP
    import src.models.save_bets as SB
    favs = pd.DataFrame([
        {"match": "a vs b", "match_date": NOW, "league": "liga_pa", "pin_home": 0.72, "pin_away": 0.10},
        {"match": "c vs d", "match_date": NOW, "league": "liga_pa", "pin_home": 0.12, "pin_away": 0.68},
        {"match": "e vs f", "match_date": NOW, "league": "liga_pa", "pin_home": 0.80, "pin_away": 0.07}])
    seen = {}

    def fake_read_sql(sql, con=None, params=None, **kw):
        seen["params"] = params
        return favs

    def fake_plan(pending, matches):
        seen["markets"] = list(pending["market"])
        return [{"id": 0, "result": "win", "profit": 1.0}, {"id": 1, "result": "loss", "profit": -1.0}]   # e vs f: sin marcador

    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(SB, "_preload_matches", lambda dates: pd.DataFrame())
    monkeypatch.setattr(SB, "plan_shadow_resolutions", fake_plan)
    monkeypatch.setattr(EP, "load_leads", lambda engine, since, normalize=None: {"c vs d": [(NOW, 0, 2)]})
    rows = F.read_rows(engine=object())
    assert seen["params"] == {"d": F.WINDOW_DAYS, "p": F.STRONG}
    assert seen["markets"] == ["home_win", "away_win", "home_win"]
    assert rows == [{"p": 0.72, "won": True, "league": "liga_pa", "lead": None},
                    {"p": 0.68, "won": False, "league": "liga_pa", "lead": 2}]


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
