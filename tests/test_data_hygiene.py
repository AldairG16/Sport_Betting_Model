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
