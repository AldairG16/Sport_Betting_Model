"""
Higiene de datos (auditoría del 25-sep-26):
  - odds_history guarda solo fotos cuya cuota cambió y ya no borra nada;
  - fechas con día y mes intercambiados (cargador viejo sin dayfirst);
  - el resumen de la noche muestra las canceladas como canceladas.
"""

from datetime import date

import pandas as pd


def test_odds_snapshot_only_stores_changes_and_never_deletes(monkeypatch):
    import scripts.odds_history as oh
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(oh, "engine", eng)
    oh.capture_snapshot(verbose=False)
    (sql, _), = eng.statements("INSERT INTO odds_history")
    assert "LEFT JOIN LATERAL" in sql and "IS DISTINCT FROM" in sql
    assert "prev.home_odds IS NULL" in sql                     # sin foto previa: se guarda
    assert not eng.statements("DELETE")


def test_swapped_dates_plan():
    from scripts.fix_swapped_dates import plan_swaps
    rows = pd.DataFrame([
        {"id": 1, "date": "2026-11-03", "home_team": "boca", "away_team": "san lorenzo"},
        {"id": 2, "date": "2026-12-25", "home_team": "a", "away_team": "b"},    # día > 12
        {"id": 3, "date": "2026-10-02", "home_team": "c", "away_team": "d"},    # ya existe corregido
        {"id": 4, "date": "2026-12-11", "home_team": "e", "away_team": "f"},    # 11-dic → 12-nov: futuro
    ])
    fixes, skipped = plan_swaps(rows, {("c", "d", date(2026, 2, 10))}, today=date(2026, 9, 25))
    assert fixes == [(1, date(2026, 11, 3), date(2026, 3, 11))]
    assert [i for i, _ in skipped] == [2, 3, 4]


def test_evening_summary_shows_cancelled_bets_as_cancelled(monkeypatch):
    import scripts.notify_telegram as nt
    day = pd.DataFrame([
        {"match": "a vs b", "match_date": pd.Timestamp("2026-09-24 20:00"), "market": "home_win",
         "odds": 1.9, "stake": 1.0, "result": "win", "profit": 0.9, "edge": 0.06,
         "probability": 0.58, "league": "soccer_epl"},
        {"match": "c vs d", "match_date": pd.Timestamp("2026-09-24 22:00"), "market": "over25",
         "odds": 2.0, "stake": 1.0, "result": "stale", "profit": 0.0, "edge": 0.06,
         "probability": 0.56, "league": "soccer_epl"},
    ])
    sent = []
    monkeypatch.setattr(nt.pd, "read_sql",
                        lambda sql, con=None, **k: day.copy() if "ORDER BY match_date" in str(sql)
                        else pd.DataFrame(columns=["match", "market", "odds", "probability", "stake", "edge"]))
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    nt.send_evening_summary(target_date=date(2026, 9, 24))
    msg = sent[0]
    assert "Bets hoy:   1  (1✅  0❌  0⏳)  ·  1 cancelada 🚫" in msg
    assert "🚫 c vs d" in msg and "cancelada antes del partido" in msg
    assert "revisar (sin resolver)" not in msg
