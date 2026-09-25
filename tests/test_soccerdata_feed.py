"""
Tests del xG real de Understat (partes puras y la descarga con fakes, sin
red ni DB). Hasta el 25-sep-26 la carga dependía de `soccerdata`, cuyo
import fallaba en CI: el paso decía OK y nunca hubo xG real.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.soccerdata_feed import current_season, parse_understat

import pandas as pd
import pytest

LEAGUE_DATA = {
    "teams": {
        "1": {"id": "1", "title": "Arsenal", "history": [
            {"h_a": "h", "xG": "2.1", "xGA": "0.7"}, {"h_a": "a", "xG": "1.4", "xGA": "1.1"}]},
        "2": {"id": "2", "title": "Chelsea", "history": [{"h_a": "a", "xG": "0.9", "xGA": "1.8"}]},
        "3": {"id": "3", "title": "Sin partidos", "history": []},
    },
    "players": [
        {"player_name": "Bukayo Saka", "team_title": "Arsenal", "goals": "2", "games": "2", "xG": "1.3"},
        {"player_name": "Sin Goles", "team_title": "Chelsea", "goals": "0", "games": "1", "xG": "0.1"},
    ],
}


class TestUnderstat:

    def test_parse_teams_and_scorers(self):
        teams, players = parse_understat(LEAGUE_DATA)
        assert teams == [
            {"team": "Arsenal", "xg_for": 3.5, "xg_against": 1.8, "matches": 2},
            {"team": "Chelsea", "xg_for": 0.9, "xg_against": 1.8, "matches": 1},
        ]
        assert players == [{"player": "Bukayo Saka", "team": "Arsenal", "goals": 2,
                            "matches": 2, "xg": 1.3}]

    def _refresh(self, monkeypatch, fetch):
        import src.features.soccerdata_feed as sf
        from tests.fake_db import FakeEngine
        eng = FakeEngine()
        monkeypatch.setattr(sf, "engine", eng)
        monkeypatch.setattr(sf, "fetch_understat", fetch)
        return sf.refresh_understat(verbose=False), eng

    def test_all_leagues_load(self, monkeypatch):
        res, eng = self._refresh(monkeypatch, lambda league, season: LEAGUE_DATA)
        assert res["status"] == "ok" and res["team_rows"] == 10     # 2 equipos × 5 ligas
        assert len(eng.statements("INSERT INTO player_club_goals")) == 5

    def test_total_failure_is_reported_not_hidden(self, monkeypatch):
        def down(league, season):
            raise ConnectionError("403")
        res, eng = self._refresh(monkeypatch, down)
        assert res["status"] == "failed" and not eng.executed

    def test_partial(self, monkeypatch):
        res, _ = self._refresh(monkeypatch, lambda league, season:
                               LEAGUE_DATA if league == "EPL" else {"teams": {}})
        assert res["status"] == "partial" and res["team_rows"] == 2

    def test_weekly_step_raises_an_error_when_nothing_loads(self, monkeypatch):
        import scripts.orchestrator as orch
        import src.features.soccerdata_feed as sf
        calls = []

        class FakeLog:
            def error(self, msg):
                calls.append(("error", msg))

            def warning(self, msg):
                calls.append(("warning", msg))

        monkeypatch.setattr(orch, "log", FakeLog())
        monkeypatch.setattr(sf, "refresh_understat",
                            lambda verbose=True: {"status": "failed", "errors": ["EPL/2026: 403"]})
        orch.step_soccerdata_refresh()
        assert calls and calls[0][0] == "error" and "xG real NO disponible" in calls[0][1]


class TestSeason:

    def test_season_format(self):
        s = current_season()
        assert len(s) == 4 and s.isdigit()
        # en sep-dic la temporada empezó este año; en ene-jul el año pasado
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year
        assert s in (str(year), str(year - 1))


class TestScorerModelClubMerge:
    """
    load_scorer_rates suma los goleadores de clubes (player_club_goals) a
    los de selecciones (match_events). Hasta el 23-sep-26 los escribía en
    el caché ANTERIOR y dos líneas después lo reemplazaba: ningún goleador
    de club llegaba a los picks. El test viejo solo buscaba "setdefault" y
    "player_club_goals" en el fuente — y pasaba.
    """

    EVENTS = pd.DataFrame([{
        "date": "2026-09-01", "home_team": "Spain", "away_team": "Italy",
        "home_scorers": '[{"player": "Lamine Yamal"}, {"player": "Pedri"}]',
        "away_scorers": '[{"player": "Retegui"}]',
    }])
    CLUB = pd.DataFrame([
        {"player": "Erling Haaland", "team": "Manchester City", "goals": 12, "matches": 10},
        {"player": "Pedri", "team": "Barcelona", "goals": 3, "matches": 9},   # ya en selecciones
        {"player": "Sin Goles", "team": "Getafe", "goals": 0, "matches": 5},
    ])

    def _rates(self, monkeypatch, events, club):
        from src.models import scorer_model as sm

        def fake_read_sql(sql, con=None, **kw):
            s = str(sql)
            if "FROM match_events" in s:
                return events.copy()
            if "FROM player_club_goals" in s:
                return club.copy()
            raise AssertionError(s[:80])

        monkeypatch.setattr(sm.pd, "read_sql", fake_read_sql)
        return sm.load_scorer_rates(force=True)

    def test_club_scorers_reach_the_rates(self, monkeypatch):
        from src.utils.team_normalizer import normalize_team
        rates = self._rates(monkeypatch, self.EVENTS, self.CLUB)
        haaland = rates["erling haaland"]
        assert haaland["source"] == "club" and haaland["goals"] == 12
        assert haaland["team"] == normalize_team("Manchester City")
        assert haaland["rate_raw"] == pytest.approx(1.2)
        assert "sin goles" not in rates

    def test_national_entry_wins_on_the_same_name(self, monkeypatch):
        rates = self._rates(monkeypatch, self.EVENTS, self.CLUB)
        assert "source" not in rates["pedri"] and rates["pedri"]["goals"] == 1

    def test_clubs_load_even_without_national_events(self, monkeypatch):
        rates = self._rates(monkeypatch, self.EVENTS.iloc[0:0], self.CLUB)
        assert set(rates) == {"erling haaland", "pedri"}
