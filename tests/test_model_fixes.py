"""
Errores silenciosos del modelo (21-oct-26), encontrados con la demostración
de los partidos del día:
  - el xG de Understat entraba como total de la temporada (23 "por partido")
    y los goles esperados chocaban con el tope en las 5 grandes ligas;
  - Dixon-Coles pesaba 25% siempre: el umbral de historia miraba la ventana
    de la forma (8 partidos como máximo);
  - la confianza ajustada del modelo se pisaba con la cruda.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# xG real por partido
# ============================================================

def _xg_table(monkeypatch, row):
    import src.features.soccerdata_feed as sf
    monkeypatch.setattr(sf.pd, "read_sql", lambda *a, **k: pd.DataFrame([row]))
    return sf


def test_real_xg_is_per_match_not_the_season_total(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    sf = _xg_table(monkeypatch, {"team": "Real Madrid", "season": "2026", "xg_for": 23.2116, "xg_against": 9.6266,
                                 "matches": 9, "updated_at": now})
    xg = sf.get_real_xg("Real Madrid")
    assert xg["xg_for"] == pytest.approx(2.579, abs=1e-3)       # antes: 23.21
    assert xg["xg_against"] == pytest.approx(1.070, abs=1e-3)
    assert xg["matches"] == 9 and xg["source"] == "understat"


def test_real_xg_without_matches_or_absurd_is_dropped(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    assert _xg_table(monkeypatch, {"team": "x", "season": "2026", "xg_for": 5.0, "xg_against": 4.0, "matches": 0,
                                   "updated_at": now}).get_real_xg("x") is None
    assert _xg_table(monkeypatch, {"team": "x", "season": "2026", "xg_for": 50.0, "xg_against": 4.0, "matches": 2,
                                   "updated_at": now}).get_real_xg("x") is None


def test_real_xg_finds_the_understat_name_of_our_team(monkeypatch):
    # 23-oct-26: Understat guarda "Bayern Munich" y "VfB Stuttgart"; el pipeline
    # pregunta "bayern munchen" y "stuttgart". 24 de 96 equipos caían al estimado.
    import src.features.soccerdata_feed as sf
    now = pd.Timestamp.now(tz="UTC")
    rows = pd.DataFrame([
        {"team": "Bayern Munich", "season": "2026", "xg_for": 20.0, "xg_against": 5.0, "matches": 8, "updated_at": now},
        {"team": "VfB Stuttgart", "season": "2026", "xg_for": 12.0, "xg_against": 10.0, "matches": 8, "updated_at": now},
        {"team": "VfB Stuttgart", "season": "2025", "xg_for": 50.0, "xg_against": 40.0, "matches": 34, "updated_at": now},
        {"team": "RasenBallsport Leipzig", "season": "2026", "xg_for": 14.0, "xg_against": 9.0, "matches": 8,
         "updated_at": now}])
    monkeypatch.setattr(sf.pd, "read_sql", lambda *a, **k: rows.copy())
    assert sf.get_real_xg("bayern munchen")["xg_for"] == pytest.approx(2.5)
    assert sf.get_real_xg("stuttgart")["xg_for"] == pytest.approx(1.5)            # la temporada más reciente
    assert sf.get_real_xg("leipzig")["xg_against"] == pytest.approx(1.125)
    assert sf.get_real_xg("arsenal") is None


def test_pipeline_only_blends_credible_xg():
    from src.pipeline.prediction_pipeline import _credible_xg
    assert _credible_xg({"xg_for": 2.1, "xg_against": 1.0}) is not None
    assert _credible_xg({"xg_for": 23.2, "xg_against": 9.6}) is None     # total de temporada
    assert _credible_xg({"xg_for": None, "xg_against": 1.0}) is None
    assert _credible_xg(None) is None


# ============================================================
# Peso de Dixon-Coles por la historia real del equipo
# ============================================================

def test_dc_weight_follows_three_years_of_history():
    from src.pipeline.prediction_pipeline import dc_weight_for
    assert dc_weight_for(120) == 0.75 and dc_weight_for(30) == 0.75
    assert dc_weight_for(29) == 0.55 and dc_weight_for(15) == 0.55
    assert dc_weight_for(14) == 0.25 and dc_weight_for(0) == 0.25


def _lambdas(monkeypatch, counts, form_matches=8):
    import src.pipeline.prediction_pipeline as pp
    for name in ("get_h2h_stats", "get_team_corners", "get_team_shots", "get_team_cards"):
        monkeypatch.setattr(pp, name, lambda *a, **k: None)
    monkeypatch.setattr(pp, "get_weather_multiplier", lambda *a, **k: 1.0)
    monkeypatch.setattr(pp, "get_dc_lambdas", lambda h, a, is_neutral=False: (3.0, 0.5))
    form = {"matches": form_matches, "attack_rating": 1.35, "defense_rating": 1.35}
    T = SimpleNamespace(home="real madrid", away="leipzig", home_form=form, away_form=form,
                        home_attack=1.35, home_defense=1.35, away_attack=1.35, away_defense=1.35,
                        HOME_ADVANTAGE=1.2, TEMPO=1.0, H2H_WEIGHT=0.15)
    ctx = SimpleNamespace(mle_fresh=True, hist_counts=counts)
    row = pd.Series({"sport_key": "soccer_uefa_champs_league", "league": "soccer_uefa_champs_league"})
    from collections import Counter
    return pp._stage_lambdas(row, T, ctx, Counter())


def test_teams_with_long_history_lean_on_dixon_coles(monkeypatch):
    L = _lambdas(monkeypatch, {"real madrid": 140, "leipzig": 135})
    assert L.mle_w == 0.75                                  # antes: 0.25 (la forma nunca pasa de 8)
    assert L.lambda_home > 2.5 and L.lambda_away < 1.0


def test_short_history_keeps_the_form(monkeypatch):
    assert _lambdas(monkeypatch, {"real madrid": 140, "leipzig": 9}).mle_w == 0.25
    assert _lambdas(monkeypatch, {"real madrid": 140, "leipzig": 20}).mle_w == 0.55


def test_without_counts_the_weight_falls_back_to_the_form_window(monkeypatch):
    assert _lambdas(monkeypatch, None, form_matches=8).mle_w == 0.25


# ============================================================
# Confianza ajustada
# ============================================================

def test_market_context_uses_the_adjusted_confidence():
    import inspect
    import src.pipeline.prediction_pipeline as pp
    src = inspect.getsource(pp._stage_market_context)
    body = src.split('"""', 2)[2]
    assert "confidence = T.confidence" not in body


# ============================================================
# Lo aprendido de la opinión del modelo empieza con el modelo corregido
# ============================================================

def test_learners_of_the_model_opinion_start_after_the_fix():
    from config.settings import LEARNING_SINCE, MODEL_SINCE
    import src.models.anchor_learner as al
    import inspect
    import src.models.side_bias as sb
    assert MODEL_SINCE >= "2026-10-22" and al.SINCE == max(LEARNING_SINCE, MODEL_SINCE)
    assert "MODEL_SINCE" in inspect.getsource(sb.read_pairs)


# ============================================================
# Sin historia suficiente, el modelo no opina
# ============================================================

def test_teams_with_almost_no_history_get_only_an_informative_pick(monkeypatch):
    """21-oct-26: con 2 partidos el modelo daba 46/22/32 al Inter contra 77/14/9
    de Pinnacle: no opina para apostar. 24-oct-26 (el dueño: "aunque tenga poca
    historia quiero ver el pick"): se analiza igual, marcado, sin apuestas."""
    from tests.pipeline_harness import _form, extended_kwargs, run_pipeline
    kw = extended_kwargs()
    kw["form_fn"] = lambda team, venue=None, **k: {**_form(team, venue), **({"matches": 2} if team == "pi" else {})}
    m = kw["matches"]
    omi = (m["home_team"].str.lower() == "omi").to_numpy()
    for c, v in (("pin_home_odds", 2.3), ("pin_draw_odds", 3.3), ("pin_away_odds", 3.2)):  # sin Pinnacle no se registra
        m[c] = [v if o else None for o in omi]
    out = run_pipeline(monkeypatch, **kw)
    rec = next(r for r in out["model_sharp"] if r["match"] == "omi vs pi")
    assert "pi con 2" in rec["low_history"]                          # el motivo, para el pick
    assert "omi vs pi" not in {b["match"] for b in out["bets"] + out["shadow"] + out["paper"]}
    assert all(not r.get("low_history") for r in out["model_sharp"] if r["match"] != "omi vs pi")
