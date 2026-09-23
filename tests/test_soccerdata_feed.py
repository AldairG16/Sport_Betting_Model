"""
Tests del puente soccerdata (partes puras, sin scraping ni DB).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.soccerdata_feed import _pick_col, current_season

import pandas as pd
import pytest


class TestPickCol:

    def test_finds_first_candidate(self):
        df = pd.DataFrame(columns=["team", "xG", "matches"])
        assert _pick_col(df, "xg_for", "xG", "xg") == "xG"

    def test_case_insensitive(self):
        df = pd.DataFrame(columns=["Xg_For"])
        assert _pick_col(df, "xg_for") == "Xg_For"

    def test_returns_none_when_missing(self):
        df = pd.DataFrame(columns=["team"])
        assert _pick_col(df, "xg_for", "xG") is None


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
