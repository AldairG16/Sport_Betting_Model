"""
tests/test_realistic_price.py
=============================
Auditoría del 13-oct-26:

- "La mejor cuota" del sistema es la más alta de ~20 casas europeas (con
  intercambios); queda ~8% arriba del promedio. Una casa típica como PlayDoit
  paga cerca del promedio: las candidatas guardan también ese precio
  (odds_avg) y el laboratorio mide a los dos.
- odds_history guarda Pinnacle y el promedio, y solo antes del partido (la
  API trae cuotas en vivo después del kickoff).
- El filtro de alineaciones (API-Football) se apagaba callado: ahora dice
  por qué y se revisa una vez al día.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.log import RUN_ISSUES


@pytest.fixture(autouse=True)
def _clean():
    RUN_ISSUES.reset()
    yield
    RUN_ISSUES.reset()


# ============================================================
# Precio promedio
# ============================================================

def _book(key, home, draw, away, over, under):
    return {"key": key, "markets": [
        {"key": "h2h", "outcomes": [{"name": "Alpha", "price": home}, {"name": "Beta", "price": away},
                                    {"name": "Draw", "price": draw}]},
        {"key": "totals", "outcomes": [{"name": "Over", "point": 2.5, "price": over},
                                       {"name": "Under", "point": 2.5, "price": under}]}]}


def test_parse_match_keeps_the_average_over_under_beside_the_best():
    from scripts.update_upcoming_matches import parse_match
    row = parse_match({"id": "e1", "home_team": "Alpha", "away_team": "Beta",
                       "commence_time": "2030-01-05T15:00:00Z",
                       "bookmakers": [_book("a", 2.2, 3.5, 3.3, 2.00, 1.80),
                                      _book("b", 2.0, 3.3, 3.5, 1.80, 2.00)]}, "soccer_epl")
    assert (row["over25_odds"], row["under25_odds"]) == (2.00, 2.00)          # mejor
    assert (row["consensus_over25_odds"], row["consensus_under25_odds"]) == (1.9, 1.9)  # promedio
    assert row["consensus_home_odds"] == 2.1


def test_candidates_take_the_average_price_of_their_market():
    from src.pipeline.prediction_pipeline import _consensus_odds
    row = pd.Series({"consensus_home_odds": 2.1, "consensus_draw_odds": float("nan"),
                     "consensus_over25_odds": 1.9})
    assert _consensus_odds("home_win", row) == 2.1
    assert _consensus_odds("over25", row) == 1.9
    assert _consensus_odds("draw", row) is None              # NaN → sin precio
    assert _consensus_odds("btts", row) is None              # mercado sin promedio guardado
    assert _consensus_odds("under25", row) is None           # columna ausente


def test_a_registered_candidate_only_receives_the_missing_average(monkeypatch):
    """La candidata conserva su primera cuota; si nació sin promedio (antes
    del 13-oct-26), la siguiente pasada se lo pone, una sola vez."""
    import src.models.save_bets as sb
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(sb, "engine", eng)
    sb.persist_shadow_bets([{"match": "a vs b", "match_date": pd.Timestamp("2026-10-20", tz="UTC"),
                             "league": "soccer_epl", "market": "home_win", "p_final": 0.5,
                             "p_ref": 0.5, "deviation": 0.03, "odds": 2.1, "edge_market": 0.02,
                             "reason": "sweep", "odds_avg": 1.95}])
    sql, params = eng.statements("INSERT INTO shadow_bets")[0]
    assert "DO UPDATE SET odds_avg = EXCLUDED.odds_avg" in sql
    assert "WHERE shadow_bets.odds_avg IS NULL AND EXCLUDED.odds_avg IS NOT NULL" in sql
    assert params["odds_avg"] == 1.95


def _lab_rows(n, odds, odds_avg, close):
    ko = pd.Timestamp("2026-10-10 18:00", tz="UTC")
    return pd.DataFrame([{
        "match": f"l{i} vs v{i}", "match_date": ko + pd.Timedelta(hours=i),
        "created_at": ko - pd.Timedelta(days=2), "market": "home_win", "p_final": 0.5,
        "odds": odds, "odds_avg": odds_avg, "edge_market": 0.0, "pin_prob": 0.52,
        "pin_close_prob": close + (0.002 if i % 2 else -0.002),
        "pin_close_at": ko + pd.Timedelta(hours=i) - pd.Timedelta(minutes=20),
        "result": "win", "profit": odds - 1} for i in range(n)])


def test_lab_judges_the_same_strategy_at_the_average_price():
    from src.models.strategy_lab import build_lab, format_lines
    lab = build_lab(_lab_rows(150, odds=2.10, odds_avg=1.90, close=0.50))
    st = lab["strategies"]["precio_0"]
    assert st["value"]["verdict"] == "a favor"           # 0.50×2.10−1 = +5%
    assert st["value_avg"]["verdict"] == "en contra"     # 0.50×1.90−1 = −5%
    text = "\n".join(format_lines(lab))
    assert "a precio promedio (tu casa): -5.0% → ❌ pierde contra el mercado" in text


def test_lab_says_it_is_measuring_when_there_is_no_average_yet():
    from src.models.strategy_lab import build_lab, format_lines
    rows = _lab_rows(150, odds=2.10, odds_avg=None, close=0.50).drop(columns=["odds_avg"])
    text = "\n".join(format_lines(build_lab(rows)))
    assert "a precio promedio (tu casa): ⏳ midiendo (0 de 100)" in text


# ============================================================
# Historial de cuotas
# ============================================================

def test_odds_history_records_pinnacle_and_the_average_only_before_kickoff():
    from scripts.odds_history import SNAPSHOT_COLS, snapshot_sql
    sql = " ".join(snapshot_sql().split())
    for c in ("pin_home_odds", "pin_over25_odds", "consensus_home_odds", "consensus_under25_odds"):
        assert c in SNAPSHOT_COLS and f"u.{c}" in sql
    assert "AND u.match_date > NOW()" in sql


# ============================================================
# API-Football
# ============================================================

def test_api_football_error_reasons_are_read_from_the_body():
    from src.features.lineups import api_errors
    assert api_errors({"errors": {"plan": "Free plans do not have access to this season"}}) == \
        "plan: Free plans do not have access to this season"
    assert api_errors({"errors": [], "response": [1]}) is None
    assert api_errors({"errors": ["token inválido"]}) == "token inválido"


def test_self_check_reports_a_plan_that_does_not_cover_the_season(monkeypatch):
    import src.features.lineups as ln
    monkeypatch.setenv("API_FOOTBALL_KEY", "x")
    answers = {"/status": ({"response": {"subscription": {"plan": "Free"},
                                         "requests": {"current": 3, "limit_day": 100}}}, None),
               "/fixtures": ({"errors": {"plan": "no season"}, "response": []}, "plan: no season")}
    monkeypatch.setattr(ln, "_fetch", lambda url: next(v for k, v in answers.items() if k in url))
    r = ln.self_check()
    assert r["ok"] is False and r["plan"] == "Free" and r["error"] == "plan: no season"
    answers["/fixtures"] = ({"response": [{}] * 412}, None)
    r = ln.self_check()
    assert r["ok"] is True and r["fixtures_today"] == 412 and r["requests"] == "3/100"


def test_daily_check_alerts_once_and_not_every_day(monkeypatch):
    import scripts.orchestrator as orch
    import src.features.lineups as ln
    import src.utils.model_state as ms
    store = {}
    monkeypatch.setattr(ms, "load_state", lambda k: store.get(k))
    monkeypatch.setattr(ms, "save_state", lambda k, v: store.__setitem__(k, dict(v)))
    monkeypatch.setattr(ln, "self_check", lambda: {"ok": False, "error": "plan: no season"})
    orch.step_api_football_check()
    assert len(RUN_ISSUES.errors()) == 1 and "filtro de alineaciones" in RUN_ISSUES.errors()[0]
    orch.step_api_football_check()                       # mismo día: no vuelve a consultar
    assert len(RUN_ISSUES.errors()) == 1
    store["api_football_check"]["day"] = "2000-01-01"    # otro día, mismo motivo: sin Telegram
    orch.step_api_football_check()
    assert len(RUN_ISSUES.errors()) == 1
