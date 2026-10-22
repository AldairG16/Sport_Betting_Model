"""
Veredicto de alineaciones en el dashboard (v1.7.1, 21-oct-26): por equipo,
qué titulares habituales faltan y si están en la banca; por partido, el
veredicto y cuánto se movió Pinnacle desde la mañana. Sin DB.
"""

import pandas as pd

from dashboard import lineup_verdict as LV

KICK = pd.Timestamp("2026-10-21 19:00", tz="UTC")
PROFILE = {"n": 15, "regulars": {str(i) for i in range(1, 12)},
           "top": "9", "top_goals": 8, "names": {str(i): f"Jugador {i}" for i in range(1, 30)}}


def _row(starters, bench=()):
    names = {str(i): f"Jugador {i}" for i in list(starters) + list(bench)}
    return {"kickoff": KICK, "formation": "4-3-3", "starters": [str(x) for x in starters], "names": names}


def test_team_lineup_lists_missing_regulars_and_who_is_on_the_bench():
    t = LV.team_lineup(_row([1, 2, 3, 4, 5, 6, 7, 8, 20, 21, 22], bench=[9, 23]), PROFILE)
    assert t["known"] and t["formation"] == "4-3-3"
    assert [(m["name"], m["bench"]) for m in t["missing"]] == [
        ("Jugador 10", False), ("Jugador 11", False), ("Jugador 9", True)]
    assert t["top_out"] == "Jugador 9"
    assert LV.team_lineup(None, PROFILE) == {"published": False}
    assert LV.team_lineup(_row(range(1, 12)), {"n": 3, "regulars": set()})["known"] is False


def _fixture(**kw):
    base = {"home_team_norm": "bayern munchen", "away_team_norm": "arsenal", "home_team": "Bayern Munich",
            "away_team": "Arsenal", "sport_key": "soccer_uefa_champs_league", "match_date": KICK,
            "pin_home_odds": 2.29, "pin_draw_odds": 3.80, "pin_away_odds": 3.05}
    return pd.DataFrame([{**base, **kw}])


def test_rotation_verdict_mentions_the_team_and_the_market_move():
    lineups = {"bayern munchen": [_row([1, 2, 3, 4, 5, 6, 7, 8, 20, 21, 9], bench=[10])],
               "arsenal": [_row(range(1, 12))]}
    morning = {"bayern munchen vs arsenal": [0.50, 0.24, 0.26]}
    (v,) = LV.lineup_verdicts(_fixture(), lineups, {"bayern munchen": PROFILE, "arsenal": PROFILE}, [],
                              morning, now=KICK - pd.Timedelta(minutes=50))
    assert v["level"] == "rotation"
    assert "Bayern Munich sin Jugador 10 (banca), Jugador 11" in v["text"]
    assert "Pinnacle bajó 8 pts al local" in v["text"] and v["move"]["side"] == "home"


def test_normal_lineups_and_unpublished_ones():
    lineups = {"bayern munchen": [_row(range(1, 12))], "arsenal": [_row(range(1, 12))]}
    profiles = {"bayern munchen": PROFILE, "arsenal": PROFILE}
    (v,) = LV.lineup_verdicts(_fixture(), lineups, profiles, [], {"bayern munchen vs arsenal": [0.5, 0.24, 0.26]},
                              now=KICK)
    assert v["level"] == "normal" and v["text"].startswith("✅") and v["move"] is None   # ya empezó
    (v,) = LV.lineup_verdicts(_fixture(), {}, profiles, [], {}, now=KICK)
    assert v["level"] == "pending"


def test_an_alert_shows_the_minimum_price_and_claudes_review():
    lineups = {"bayern munchen": [_row([1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23])], "arsenal": [_row(range(1, 12))]}
    alerts = [{"home": "bayern munchen", "away": "arsenal", "weakened": "bayern munchen",
               "missing": ["Jugador 8", "Jugador 9", "Jugador 10", "Jugador 11"], "market": "dc_x2",
               "min_american": 175, "analyst_verdict": "CONFIRMA", "analyst_reason": "lesiones confirmadas"}]
    (v,) = LV.lineup_verdicts(_fixture(), lineups, {"bayern munchen": PROFILE, "arsenal": PROFILE},
                              alerts, {}, now=KICK - pd.Timedelta(minutes=40))
    assert v["level"] == "alert"
    assert "Bayern Munich sale sin 4 titulares" in v["text"]
    assert "Empate o Arsenal: solo si PlayDoit paga +175 o mejor" in v["text"]
    assert "confirma — lesiones confirmadas" in v["text"]


def test_four_missing_without_an_alert_explains_why():
    lineups = {"bayern munchen": [_row([1, 2, 3, 4, 5, 6, 7, 20, 21, 22, 23])], "arsenal": [_row(range(1, 12))]}
    (v,) = LV.lineup_verdicts(_fixture(), lineups, {"bayern munchen": PROFILE, "arsenal": PROFILE}, [], {},
                              now=KICK)
    assert v["level"] == "weak" and "menos de 20%" in v["text"]


def test_lineups_endpoint_answers_with_synthetic_data(monkeypatch):
    import dashboard.app as dash
    import src.models.lineup_alerts as la

    def fake_q(sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM upcoming_matches" in s:
            return _fixture()
        if "FROM lineups" in s:
            return pd.DataFrame([{"team": "bayern munchen", "kickoff": KICK, "formation": "4-2-3-1",
                                  "starters": [str(i) for i in range(1, 12)], "names": {}},
                                 {"team": "arsenal", "kickoff": KICK, "formation": "4-3-3",
                                  "starters": [str(i) for i in range(1, 12)], "names": {}}])
        if "FROM lineup_alerts" in s:
            return pd.DataFrame()
        if "FROM model_vs_sharp" in s:
            return pd.DataFrame([{"match": "bayern munchen vs arsenal", "pin_home": 0.5,
                                  "pin_draw": 0.24, "pin_away": 0.26}])
        raise AssertionError(f"SQL inesperado: {s[:100]}")
    monkeypatch.setattr(dash, "_q", fake_q)
    monkeypatch.setattr(la, "load_profiles", lambda engine, teams: {t: PROFILE for t in teams})
    c = dash.app.test_client()
    d = c.get("/api/lineups").get_json()
    assert d["ok"] and d["matches"][0]["match"] == "Bayern Munich vs Arsenal"
    assert d["matches"][0]["level"] in ("normal", "rotation")
