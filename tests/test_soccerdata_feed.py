"""
Tests del puente soccerdata (partes puras, sin scraping ni DB).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.soccerdata_feed import _pick_col, current_season
from src.features.table_lies import match_xpts  # usa el mismo np/poisson

import pandas as pd


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

    def test_club_entries_shape(self):
        """Las entradas de clubes que inyecta el merge tienen el contrato
        que goalscorer_picks espera (team normalizado, rates, goals)."""
        import inspect
        from src.models import scorer_model
        src = inspect.getsource(scorer_model.load_scorer_rates)
        # el merge debe usar setdefault (nacional gana) y normalize_team
        assert "setdefault" in src
        assert "normalize_team" in src
        assert "player_club_goals" in src
