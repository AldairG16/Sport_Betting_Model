"""
Auditoría de los picks de mañana (22-oct-26).

El dueño: "revisa los partidos de mañana y dime si las funciones que tenemos,
toda la valoración para decir este es el pick, funciona correctamente". La
elección del pick cuadró con un recálculo independiente en los 24 partidos,
pero salieron tres errores de los datos que la alimentan:

  - análisis viejos: 16 partidos que la corrida de la mañana ya no evaluó
    (poca historia) seguían con el análisis de una corrida anterior, y de ahí
    salía su pick (uno, del modelo con el error del xG);
  - córners 0-0 = sin dato en Brasil, Liga MX y Argentina: el modelo de
    córners veía ligas de 0 córners y daba 0% a "más de 9.5";
  - Elo NaN en 107 equipos (Ligue 1, LaLiga, Grecia, Champions…): dos
    partidos sin marcador contagiaban a todos sus rivales.
Sin DB.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tests.fake_db import FakeEngine


# ─── análisis viejos ─────────────────────────────────────────────────────────

def test_a_match_the_last_run_did_not_evaluate_has_no_pick_from_an_old_analysis():
    from src.models import program_picks as PP
    from tests.test_program_picks import MODEL_EVEN, _mvs, _u
    row = _mvs(model=tuple(MODEL_EVEN), over=0.60)
    assert PP.decide(row, _u())[0]["market"] == "over25"
    assert PP.decide({**row, "skipped_at": pd.Timestamp("2030-01-05 12:00", tz="UTC")}, _u()) == (None, PP.STALE)
    assert PP.decide({**row, "skipped_at": pd.NaT}, _u())[0]["market"] == "over25"     # NaT = sí se evaluó


def test_the_pipeline_marks_the_matches_it_no_longer_evaluates(monkeypatch):
    from tests.pipeline_harness import TEAMS, _form, _row, run_pipeline
    thin = {"matches": 3}
    form = (lambda team, venue=None, **k: {**_form("beta" if team == "nuevo" else team, venue), **(thin if team == "nuevo" else {})})
    df = pd.DataFrame([_row("alpha", "beta", "soccer_spain_la_liga", home_odds=1.80, draw_odds=3.70, away_odds=4.60),
                       _row("nuevo", "gamma", "soccer_spain_la_liga", home_odds=2.60, draw_odds=3.30, away_odds=2.70,
                            match_date="2030-01-05T17:00:00+00:00")])
    assert "nuevo" not in TEAMS
    out = run_pipeline(monkeypatch, matches=df, form_fn=form)
    assert [s["match"] for s in out["skipped"]] == ["nuevo vs gamma"]
    assert str(out["skipped"][0]["match_date"]) == "2030-01-05T17:00:00+00:00"


def test_mark_skipped_and_a_new_analysis_clears_it(monkeypatch):
    import config.database as db
    from src.models import side_bias as sb
    eng = FakeEngine()
    monkeypatch.setattr(db, "engine", eng)
    assert sb.mark_skipped([{"match": "fc seoul vs gangwon fc", "match_date": "2026-10-24T05:00:00+00:00"}]) == 1
    (sql, params), = eng.statements("UPDATE model_vs_sharp SET skipped_at = NOW()")
    assert params == [{"match": "fc seoul vs gangwon fc", "match_date": "2026-10-24T05:00:00+00:00"}]
    rec = sb.record("a vs b", "2030-01-05", "soccer_epl", {"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
                    {"home": 0.45, "draw": 0.27, "away": 0.28})
    sb.persist([rec])
    (sql, _), = eng.statements("INSERT INTO model_vs_sharp")
    assert "skipped_at = NULL" in sql
    assert sb.is_skipped({"skipped_at": pd.Timestamp("2030-01-05", tz="UTC")}) is True
    assert sb.is_skipped({"skipped_at": None}) is False and sb.is_skipped(SimpleNamespace(skipped_at=pd.NaT)) is False


def test_the_dashboard_does_not_show_an_old_analysis():
    from dashboard import visibility as V
    kick = pd.Timestamp("2030-01-05 19:00", tz="UTC")
    mvs = pd.DataFrame([{"match": "fc seoul vs gangwon fc", "match_date": kick, "league": "soccer_korea_kleague1",
                         "p_home": 0.29, "p_draw": 0.30, "p_away": 0.42, "pin_home": 0.52, "pin_draw": 0.27,
                         "pin_away": 0.21, "lam_home": 0.84, "lam_away": 1.24, "p_over25": 0.38, "p_btts": 0.43,
                         "created_at": kick - pd.Timedelta(days=2), "skipped_at": kick - pd.Timedelta(hours=8)}])
    (m,) = V.predictions(mvs, pd.DataFrame(columns=["home_team_norm", "away_team_norm"]), set())
    assert [s["model"] for s in m["sides"]] == [None, None, None] and m["skipped"] is True
    assert (m["lam"], m["p_over25"], m["p_btts"], m["old_model"]) == (None, None, None, False)
    assert [s["pin"] for s in m["sides"]] == [0.52, 0.27, 0.21]              # Pinnacle sí


# ─── córners 0-0 = sin dato ──────────────────────────────────────────────────

def test_zero_zero_corners_are_missing_data_not_a_league_without_corners(monkeypatch):
    from src.models import props_fair as PF
    seen = []

    def read_sql(sql, con=None, **k):
        seen.append(" ".join(str(sql).split()))
        return pd.DataFrame(columns=["date", "league", "home", "away", "home_corners", "away_corners",
                                     "home_cards", "away_cards"])
    monkeypatch.setattr(PF.pd, "read_sql", read_sql)
    PF.load_history(engine=object())
    assert "CASE WHEN (home_corners = 0 AND away_corners = 0) THEN NULL ELSE home_corners END AS home_corners" in seen[0]
    # sin córners reales la liga no tiene promedio: no hay precio (antes: 0%)
    hist = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=60, freq="D"), "league": "soccer_brazil_campeonato",
                         "home": "cruzeiro", "away": "remo", "home_corners": np.nan, "away_corners": np.nan,
                         "home_cards": np.nan, "away_cards": np.nan})
    assert PF.match_props(hist, "cruzeiro", "remo", "soccer_brazil_campeonato", pd.Timestamp("2026-10-23")) == {}


# ─── Elo sin NaN ─────────────────────────────────────────────────────────────

def test_a_match_without_score_does_not_turn_the_elo_into_nan(monkeypatch):
    import math
    import src.features.elo_rating as ER
    seen = []
    games = pd.DataFrame([
        {"home_team": "Brest", "away_team": "Nice", "home_goals": 2, "away_goals": 0, "date": "2026-09-01"},
        {"home_team": "Lorient", "away_team": "Brest", "home_goals": None, "away_goals": None, "date": "2026-09-08"},
        {"home_team": "Nice", "away_team": "Lorient", "home_goals": 1, "away_goals": 1, "date": "2026-09-15"},
    ])

    def read_sql(sql, con=None, **k):
        seen.append(" ".join(str(sql).split()))
        return games.copy()
    monkeypatch.setattr(ER.pd, "read_sql", read_sql)
    elo = ER.compute_elo()
    assert "home_goals IS NOT NULL AND away_goals IS NOT NULL" in seen[0]
    assert set(elo) == {"brest", "nice", "lorient"} and all(math.isfinite(v) for v in elo.values())
    assert elo["brest"] > 1500 > elo["nice"]


def test_a_nan_elo_is_an_error_that_reaches_telegram(monkeypatch):
    from src.utils.log import RUN_ISSUES
    from tests.pipeline_harness import ELO, run_pipeline
    RUN_ISSUES.reset()
    run_pipeline(monkeypatch, overrides={"compute_elo": lambda: {**ELO, "brest": float("nan")}})
    assert any("Elo inválido" in e and "brest" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


# ─── el pick usa el 1X2 corregido por el sesgo por lado ──────────────────────

BIAS = {"home": 0.04, "draw": -0.02, "away": -0.02}


def test_the_pick_uses_the_1x2_after_the_side_bias_like_the_pipeline():
    from src.models import program_picks as PP
    from tests.test_program_picks import _mvs, _u
    row = _mvs(model=(0.48, 0.25, 0.27), over=None, btts=None)        # Pinnacle de _u: ≈ 43 / 26 / 28
    pk, why = PP.decide(row, _u())
    assert pk["market"] == "home_win" and why is None                     # crudo: 5 pts, pasa la regla
    pk, why = PP.decide(row, _u(), bias=BIAS)
    assert pk["market"] == "home_win" and why == PP.NO_EDGE               # corregido: ~44%, sin ventaja
    assert pk["model"] == pytest.approx(0.44, abs=0.005)
    assert PP.model_1x2(row) == [0.48, 0.25, 0.27]


def test_the_dashboard_bar_is_the_corrected_1x2_the_same_as_the_pick():
    from dashboard import visibility as V
    kick = pd.Timestamp("2030-01-05 19:00", tz="UTC")
    mvs = pd.DataFrame([{"match": "brest vs nice", "match_date": kick, "league": "soccer_france_ligue_one",
                         "p_home": 0.48, "p_draw": 0.25, "p_away": 0.27, "pin_home": 0.43, "pin_draw": 0.26,
                         "pin_away": 0.31, "lam_home": 1.5, "lam_away": 1.1, "p_over25": 0.5, "p_btts": 0.5,
                         "created_at": kick, "skipped_at": pd.NaT}])
    (m,) = V.predictions(mvs, pd.DataFrame(columns=["home_team_norm", "away_team_norm"]), set(), bias=BIAS)
    assert [round(s["model"], 2) for s in m["sides"]] == [0.44, 0.27, 0.29]


def test_the_snapshot_reads_the_side_bias_with_its_own_engine():
    import json
    from src.models import program_picks as PP
    from tests.fake_db import FakeResult
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[(json.dumps(BIAS),)]) if "model_state" in sql else FakeResult())
    assert PP.load_bias(eng) == BIAS
    assert PP.load_bias(FakeEngine()) is None                             # sin aprender todavía: el crudo


# ─── liquidar sin datos de córners y tiros (23-oct-26) ───────────────────────

def test_zero_zero_corners_and_shots_are_not_settled():
    from src.models.save_bets import resolve_market
    from src.utils.output_checks import settle_independent
    no_data = {"home_goals": 1, "away_goals": 0, "home_corners": 0, "away_corners": 0,
               "home_shots_target": 0, "away_shots_target": 0}
    assert resolve_market("corners_under_9.5", no_data, 1.9, 1.0)[0] == "unresolved"     # antes: win
    assert resolve_market("shots_under_7.5", no_data, 1.9, 1.0)[0] == "unresolved"
    assert settle_independent("corners_under_9.5", no_data) is None
    real = {**no_data, "home_corners": 6, "away_corners": 2}
    assert resolve_market("corners_under_9.5", real, 1.9, 1.0)[0] == "win"
    assert settle_independent("corners_under_9.5", real) == "win"
