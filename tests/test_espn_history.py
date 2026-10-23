"""
Los últimos 5 de verdad (22-oct-26).

El dueño, al abrir Stuttgart–Gladbach: "te estás saltando partidos, no son
los últimos 5 partidos de él, y lo mismo pasa con el rival". `matches` solo
trae las ligas (y la Champions incompleta): faltaban la DFB-Pokal y los
amistosos de la fecha FIFA, y a cualquier equipo de Europa League, sus
jueves. Ahora espn_matches (src/features/espn_history.py) los suma en el
partido abierto y en Equipos. Sin DB ni red.
"""

import pandas as pd
import pytest

from src.features import espn_history as EH
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine, FakeResult


def _event(eid, date, home, away, hg, ag, hid="1", aid="2", corners=(5, 3), cards=(("Yellow Card", "1"),),
           done=True):
    stats = (lambda c: [{"name": "wonCorners", "displayValue": str(c)}]) if corners else (lambda c: [])
    return {"id": eid, "date": date, "competitions": [{
        "status": {"type": {"completed": done}},
        "competitors": [
            {"homeAway": "home", "team": {"id": hid, "displayName": home}, "score": str(hg),
             "statistics": stats(corners[0] if corners else None)},
            {"homeAway": "away", "team": {"id": aid, "displayName": away}, "score": str(ag),
             "statistics": stats(corners[1] if corners else None)}],
        "details": [{"type": {"text": t}, "team": {"id": tid}} for t, tid in cards]}]}


POKAL = _event("9", "2026-08-21T18:00Z", "Hansa Rostock", "VfB Stuttgart", 0, 4, "70", "134", (4, 8),
               (("Yellow Card", "70"), ("Yellow Card", "134"), ("Red Card", "134")))
FRIENDLY = _event("8", "2026-10-01T16:00Z", "VfB Stuttgart", "SpVgg Greuther Fürth", 5, 0, "134", "71",
                  corners=None, cards=())


# ─── leer ESPN ───────────────────────────────────────────────────────────────

def test_a_scoreboard_gives_score_corners_and_cards_red_counts_one():
    pending = _event("7", "2026-10-28T18:00Z", "A", "B", 0, 0, done=False)
    rows = EH.parse_events({"events": [POKAL, FRIENDLY, pending]}, "ger.dfb_pokal")
    pokal, friendly = rows
    assert (pokal["home_goals"], pokal["away_goals"], pokal["home_corners"], pokal["away_corners"]) == (0, 4, 4, 8)
    assert (pokal["home_cards"], pokal["away_cards"]) == (1, 2)                       # amarilla + roja = 2
    assert (friendly["home_corners"], friendly["home_cards"]) == (None, None)          # sin dato, no 0


def test_espn_teams_are_matched_to_our_names_with_the_league_games():
    league = EH.parse_events({"events": [
        _event("1", "2026-10-17T13:30Z", "Hamburg SV", "VfB Stuttgart", 1, 0, "3", "134")]}, "ger.1")
    ours = [{"date": "2026-10-17", "h": "hamburg", "a": "stuttgart", "home_goals": 1, "away_goals": 0},
            {"date": "2026-10-17", "h": "leipzig", "a": "freiburg", "home_goals": 1, "away_goals": 0}]
    assert EH.learn_team_ids(league, ours) == {"3": "hamburg", "134": "stuttgart"}
    # mismo marcador y fecha pero nombres que no se parecen: no se adivina
    assert EH.learn_team_ids(league, ours[1:]) == {}
    rows = EH.resolve(EH.parse_events({"events": [POKAL]}, "ger.dfb_pokal"), {"134": "stuttgart"})
    assert (rows[0]["away"], rows[0]["home"], rows[0]["competition"], rows[0]["kind"]) == \
        ("stuttgart", "hansa rostock", "DFB-Pokal", "copa")


def test_months_back():
    assert EH.months_back(pd.Timestamp("2026-10-22", tz="UTC"), 3) == ["202610", "202609", "202608"]


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


def test_the_refresh_saves_cups_and_friendlies_with_our_names(monkeypatch):
    boards = {"ger.1": {"events": [_event("1", "2026-10-17T13:30Z", "Hamburg SV", "VfB Stuttgart", 1, 0, "3", "134")]},
              "ger.dfb_pokal": {"events": [POKAL]}, "club.friendly": {"events": [FRIENDLY]}}
    monkeypatch.setattr(EH, "_league_slugs", lambda: {"soccer_germany_bundesliga": "ger.1"})
    monkeypatch.setattr(EH.pd, "read_sql", lambda sql, con=None, **k: pd.DataFrame(
        [{"date": "2026-10-17", "h": "hamburg", "a": "stuttgart", "home_goals": 1, "away_goals": 0}]))
    get = (lambda url, **k: _Resp(boards.get(url.split("/soccer/")[1].split("/")[0], {"events": []})))
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[]) if sql.startswith("SELECT espn_id") else FakeResult())
    assert EH.refresh_espn_history(eng, months=1, get=get, pause=0, today="2026-10-22") == 2
    (sql, rows), = eng.statements("INSERT INTO espn_matches")
    assert "ON CONFLICT (espn_id) DO UPDATE" in sql
    assert {(r["home"], r["away"], r["competition"]) for r in rows} == {
        ("hansa rostock", "stuttgart", "DFB-Pokal"), ("stuttgart", "spvgg greuther furth", "Amistoso")}
    (_, ids), = eng.statements("INSERT INTO espn_teams")
    assert {"i": "134", "t": "stuttgart"} in ids


def test_without_any_competition_it_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()
    monkeypatch.setattr(EH, "_league_slugs", lambda: {})
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[]))
    assert EH.refresh_espn_history(eng, months=1, get=lambda url, **k: _Resp(None, 502), pause=0) == 0
    assert eng.statements("INSERT INTO espn_matches") == []
    assert any("Historial ESPN" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


# ─── el partido abierto y Equipos ────────────────────────────────────────────

LEAGUE = [{"date": "2026-10-17", "league": "soccer_germany_bundesliga", "h": "hamburg", "a": "stuttgart",
           "home_goals": 1, "away_goals": 0, "home_corners": 5, "away_corners": 9, "home_yellow": 2,
           "away_yellow": 1, "home_red": 0, "away_red": 0},
          {"date": "2026-10-14", "league": "soccer_uefa_champs_league", "h": "sk slovan bratislava", "a": "stuttgart",
           "home_goals": 0, "away_goals": 3, "home_corners": None, "away_corners": None, "home_yellow": None,
           "away_yellow": None, "home_red": None, "away_red": None}]
ESPN = [{"date": "2026-10-14T16:45Z", "league": "Champions League", "kind": "europa", "h": "slovan bratislava",
         "a": "stuttgart", "home_goals": 0, "away_goals": 3, "home_corners": 1, "away_corners": 8,
         "home_cards": 1, "away_cards": 3},                                   # el mismo partido, otro nombre
        {"date": "2026-10-01T16:00Z", "league": "Amistoso", "kind": "amistoso", "h": "stuttgart",
         "a": "spvgg greuther furth", "home_goals": 5, "away_goals": 0, "home_corners": None, "away_corners": None,
         "home_cards": None, "away_cards": None},
        {"date": "2026-08-21T18:00Z", "league": "DFB-Pokal", "kind": "copa", "h": "hansa rostock", "a": "stuttgart",
         "home_goals": 0, "away_goals": 4, "home_corners": 4, "away_corners": 8, "home_cards": 1, "away_cards": 2}]


def test_the_last_five_include_every_competition_without_repeating_a_match():
    from dashboard import match_detail as MD
    d = MD.match_detail(pd.DataFrame(LEAGUE), pd.DataFrame(), "stuttgart", "gladbach",
                        pd.Timestamp("2026-10-23 18:30", tz="UTC"), pd.DataFrame(ESPN))
    ms = d["home"]["matches"]
    assert [(m["opponent"][:6], m["league"], m["friendly"]) for m in ms] == [
        ("Hambur", "Bundesliga", False), ("Sk Slo", "Champions League", False),
        ("Spvgg ", "Amistoso", True), ("Hansa ", "DFB-Pokal", False)]
    # la Champions de The Odds API llega sin córners ni tarjetas: se completan con ESPN
    assert (ms[1]["corners"], ms[1]["cards"]) == ([8, 1], 4)
    s = d["home"]["summary"]
    # el 5-0 amistoso se ve, pero no cuenta en los promedios
    assert (s["n"], s["friendlies"], s["w"], s["l"]) == (3, 1, 2, 1)
    assert s["goals_for"] == pytest.approx(round(7 / 3, 1)) and s["corners_for"] == pytest.approx(8.3)


def test_the_team_tab_lists_cups_and_friendlies_but_the_record_is_official():
    from dashboard import board as B
    v = B.team_view("stuttgart", pd.DataFrame(LEAGUE), pd.DataFrame(), pd.DataFrame(ESPN))
    assert [m["league"] for m in v["matches"]] == ["DFB-Pokal", "Amistoso", "Champions League", "Bundesliga"]
    assert v["record"] == {"w": 2, "d": 0, "l": 1} and [m["friendly"] for m in v["matches"]] == [False, True, False, False]


# ─── el orquestador ─────────────────────────────────────────────────────────

def test_the_evening_reads_espn_before_the_predictions(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_evening")
    assert names.index("Historial ESPN (copas, Europa, amistosos)") == names.index("Predictions (mañana)") - 1
    assert steps["Historial ESPN (copas, Europa, amistosos)"] is O.step_espn_history
