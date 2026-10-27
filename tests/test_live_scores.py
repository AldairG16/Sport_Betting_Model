"""
Marcadores del día (24-oct-26, el dueño: "también quiero ver si se ganó o
perdió"): el closing lee de ESPN el marcador de los partidos que empezaron y
el dashboard muestra el pick ganado o perdido, provisional hasta la noche.
"""

import pandas as pd

from src.features import live_scores as LS


def _ev(eid, home, away, hg, ag, state="post", completed=True, corners=(5, 4), cards=None):
    comp = {"status": {"type": {"state": state, "completed": completed, "shortDetail": "FT" if completed else "67'"}},
            "competitors": [
                {"homeAway": "home", "score": str(hg), "team": {"id": "1", "displayName": home},
                 "statistics": [{"name": "wonCorners", "displayValue": str(corners[0])}]},
                {"homeAway": "away", "score": str(ag), "team": {"id": "2", "displayName": away},
                 "statistics": [{"name": "wonCorners", "displayValue": str(corners[1])}]}],
            "details": [{"type": {"text": "Yellow Card"}, "team": {"id": t}} for t in (cards or [])]}
    return {"id": eid, "date": "2026-10-24T16:30Z", "competitions": [comp]}


def test_only_started_matches_with_their_score_corners_and_cards():
    data = {"events": [_ev("1", "Chelsea", "Tottenham Hotspur", 0, 2, cards=["1", "1", "2"]),
                       _ev("2", "Arsenal", "Everton", 1, 0, state="in", completed=False),
                       _ev("3", "Lazio", "Parma", 0, 0, state="pre", completed=False)]}
    rows = LS.parse_live(data)
    assert [r["event"] for r in rows] == ["1", "2"]                         # el que no empezó, no
    ch = rows[0]
    assert (ch["home_goals"], ch["away_goals"], ch["status"]) == (0, 2, "final")
    assert (ch["home_corners"], ch["away_corners"], ch["home_cards"], ch["away_cards"]) == (5, 4, 2, 1)
    assert rows[1]["status"] == "live" and rows[1]["clock"] == "67'"


def test_the_provisional_result_uses_the_settlement_rule():
    score = {"home_goals": 2, "away_goals": 1, "home_corners": 4, "away_corners": 7, "home_cards": 4, "away_cards": 4}
    assert LS.provisional_result("btts", score, 1.9) == ("win", 0.9)
    assert LS.provisional_result("under25", score, 1.9) == ("loss", -1.0)
    assert LS.provisional_result("cards_over_4.5", score, 2.0) == ("win", 1.0)
    assert LS.provisional_result("corners_under_10.5", score, 1.8) == ("loss", -1.0)
    assert LS.provisional_result("btts", score, None) == ("win", None)          # sin momio: cuenta, sin unidades
    assert LS.provisional_result("corners_over_9.5", {"home_goals": 1, "away_goals": 0}, 1.9) == (None, None)


def test_the_dashboard_shows_the_provisional_result():
    from dashboard import board as B
    now = pd.Timestamp("2026-10-24 19:40", tz="UTC")
    pick = {"match": "chelsea vs tottenham", "match_date": pd.Timestamp("2026-10-24 16:30", tz="UTC"),
            "league": "soccer_epl", "label": "Ambos anotan", "market": "btts", "family": "Ambos anotan",
            "reason": None, "model_prob": 0.6, "market_prob": 0.55, "result": None, "profit": None,
            "source": None, "playdoit_odds": 1.85}
    live = pd.DataFrame([{"home": "chelsea", "away": "tottenham", "kickoff": pd.Timestamp("2026-10-24 16:30", tz="UTC"),
                          "home_goals": 0, "away_goals": 2, "home_corners": 4, "away_corners": 6,
                          "home_cards": 4, "away_cards": 4, "status": "final", "clock": "FT"}])
    rows = B.past_matches(pd.DataFrame(), pd.DataFrame(), pd.DataFrame([pick]), pd.DataFrame(),
                          normalize=lambda t: str(t).lower(), now=now, live=live)
    r = rows[0]
    assert r["score"] == [0, 2] and r["live"]["status"] == "final" and r["corners"] == [4, 6] and r["cards"] == 8
    assert r["pick"]["result"] == "loss" and r["pick"]["provisional"] and r["pick"]["profit"] == -1.0
    assert B.summary(rows)["picks_fixed"]["n"] == 0                       # cuenta cuando la noche lo confirma
    live.loc[0, "status"] = "live"
    r = B.past_matches(pd.DataFrame(), pd.DataFrame(), pd.DataFrame([pick]), pd.DataFrame(),
                       normalize=lambda t: str(t).lower(), now=now, live=live)[0]
    assert r["score"] == [0, 2] and r["pick"]["result"] == "pending"        # en juego: todavía no


def test_the_closing_reads_the_scores_before_the_picks(monkeypatch):
    import scripts.orchestrator as O
    ran = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, fn: ran.append(name))
    O.run_closing(None)
    assert ran.index("Marcadores del día (ESPN)") == ran.index("Pre-kickoff closing odds") + 1
    assert ran.index("Marcadores del día (ESPN)") < ran.index("Picks del programa")


def test_capture_matches_espn_to_our_fixtures(monkeypatch):
    from tests.fake_db import FakeEngine, FakeResult
    kick = pd.Timestamp("2026-10-24 16:30", tz="UTC").to_pydatetime()
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[("soccer_epl", "chelsea", "tottenham", kick)])
                     if "FROM upcoming_matches" in sql else FakeResult())
    data = {"events": [_ev("1", "Chelsea", "Tottenham Hotspur", 0, 2)]}

    class R:
        status_code = 200

        def json(self):
            return data
    n = LS.capture_live_scores(eng, get=lambda url, headers=None, timeout=None: R(),
                               now=pd.Timestamp("2026-10-24 19:40", tz="UTC").to_pydatetime())
    assert n == 1
    (sql, params), = eng.statements("INSERT INTO live_scores")
    assert params[0]["home"] == "chelsea" and params[0]["away"] == "tottenham" and params[0]["status"] == "final"


def test_the_daily_check_catches_a_provisional_score_that_was_wrong():
    from src.utils.output_checks import live_score_mismatches
    kick = pd.Timestamp("2026-10-24 16:30", tz="UTC")
    live = [{"home": "chelsea", "away": "tottenham", "kickoff": kick, "home_goals": 0, "away_goals": 2, "status": "final"},
            {"home": "arsenal", "away": "everton", "kickoff": kick, "home_goals": 2, "away_goals": 0, "status": "final"},
            {"home": "lazio", "away": "parma", "kickoff": kick, "home_goals": 1, "away_goals": 0, "status": "live"}]
    matches = [{"h": "chelsea", "a": "tottenham", "d": "2026-10-24", "home_goals": 0, "away_goals": 2},
               {"h": "arsenal", "a": "everton", "d": "2026-10-24", "home_goals": 2, "away_goals": 1},     # ESPN dijo 2-0
               {"h": "lazio", "a": "parma", "d": "2026-10-24", "home_goals": 2, "away_goals": 1}]          # en juego: no
    bad = live_score_mismatches(live, matches)
    assert bad == [{"match": "arsenal vs everton", "espn": "2-0", "oficial": "2-1"}]


# ─── córners y tarjetas de los partidos pasados (v2.5.1, 26-oct-26) ──────────
# El dueño: "¿por qué solo los de hoy tienen tarjetas y córners y los de ayer no? ni los del pasado".

def test_finals_from_espn_keep_the_system_names_and_skip_what_does_not_match():
    import datetime as dt
    events = LS.parse_live({"events": [_ev("1", "LAFC", "LA Galaxy", 2, 2, corners=(12, 1), cards=["1", "2"]),
                                       _ev("2", "Arsenal", "Everton", 1, 0, state="in", completed=False),
                                       _ev("3", "Unknown FC", "Nobody United", 1, 1),
                                       _ev("4", "Chelsea", "Tottenham Hotspur", 0, 2)]})
    cands = [(pd.Timestamp("2026-10-24"), "lafc", "la galaxy"), (pd.Timestamp("2026-10-24"), "chelsea", "tottenham")]
    rows = LS.final_rows("soccer_usa_mls", events, cands, have={"4"}, start=dt.date(2026, 10, 20))
    assert [(r["home"], r["away"], r["espn_id"]) for r in rows] == [("lafc", "la galaxy", "1")]
    r = rows[0]
    assert (r["home_corners"], r["away_corners"], r["home_cards"], r["away_cards"], r["status"]) == (12, 1, 1, 1, "final")
    # en vivo, sin su partido en matches o ya guardado: no
    assert LS.final_rows("soccer_usa_mls", events, cands, have=set(), start=dt.date(2026, 10, 25)) == []   # antes del inicio


def test_the_board_fills_corners_and_cards_from_espn_only_for_the_same_match():
    from dashboard import board as B
    row = {"date": pd.Timestamp("2026-10-25"), "league": "soccer_usa_mls", "h": "lafc", "a": "la galaxy",
           "home_goals": 2, "away_goals": 2, "home_corners": None, "away_corners": None,
           "home_yellow": None, "away_yellow": None, "home_red": None, "away_red": None}
    espn = {"home": "lafc", "away": "la galaxy", "kickoff": pd.Timestamp("2026-10-26 01:00", tz="UTC"),
            "home_goals": 2, "away_goals": 2, "home_corners": 12, "away_corners": 1, "home_cards": 1,
            "away_cards": 1, "status": "final"}
    (f,) = B.fill_from_espn([row], [espn])
    assert (B._corners(f), B._cards(f)) == ([12, 1], 2)
    assert B._corners(B.fill_from_espn([row], [{**espn, "home_goals": 3}])[0]) is None          # otro marcador: no
    assert B._corners(B.fill_from_espn([row], [{**espn, "status": "live"}])[0]) is None         # no terminado: no
    football_data = {**row, "home_corners": 7, "away_corners": 3, "home_yellow": 2, "away_yellow": 2,
                     "home_red": 1, "away_red": 0}
    assert B.fill_from_espn([football_data], [espn])[0] is football_data                       # lo que hay no se pisa
    rows = B.past_matches(pd.DataFrame([row]), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                          normalize=lambda t: str(t).lower(), now=pd.Timestamp("2026-10-26 20:00", tz="UTC"),
                          live=pd.DataFrame([espn]))
    assert (rows[0]["corners"], rows[0]["cards"]) == ([12, 1], 2)


def test_the_match_detail_fills_its_last_five_without_adding_matches():
    from dashboard import match_detail as MD
    m = [{"date": pd.Timestamp("2026-10-26"), "league": "soccer_usa_mls", "h": "lafc", "a": "la galaxy",   # fecha UTC
          "home_goals": 2, "away_goals": 2, "home_corners": None, "away_corners": None}]
    fill = [{"date": pd.Timestamp("2026-10-26 01:00", tz="UTC"), "h": "lafc", "a": "la galaxy", "home_goals": 2,
             "away_goals": 2, "home_corners": 12, "away_corners": 1, "home_cards": 1, "away_cards": 1},
            {"date": pd.Timestamp("2026-10-19 01:00", tz="UTC"), "h": "lafc", "a": "austin fc", "home_goals": 1,
             "away_goals": 0, "home_corners": 5, "away_corners": 2, "home_cards": 0, "away_cards": 3}]
    out = MD.merge_history(m, [], fill)
    assert len(out) == 1 and (out[0]["home_corners"], out[0]["away_corners"]) == (12, 1)
    assert MD._cards(out[0], "home") == 1


def test_the_evening_saves_espn_finals_after_the_results(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_evening")
    assert names.index("Córners y tarjetas (ESPN)") == names.index("Fetch results (fbdata backup)") + 1
    assert steps["Córners y tarjetas (ESPN)"] is O.step_espn_finals
