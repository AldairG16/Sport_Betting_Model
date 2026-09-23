"""
tests/test_round9_audit.py
==========================
Tests de la Ronda 9: unidad de agregación del shadow (D1/R12), el closing
de producción ve resueltas y rellena shadow (D1-raíz), contadores de
población del sweep (D3/R17).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.clv_gate as cg

# ============================================================
# D1/R12 — agregación en la unidad de la decisión
# ============================================================

def test_shadow_clv_groups_by_market_and_band():
    """La conclusión CLV se agrega por (mercado × banda) — el piso es por
    mercado — y separa "hay filas" (n_banda) de "hay medición"
    (n_con_closing)."""
    import pandas as pd
    df = pd.DataFrame([
        {"market": "over25", "deviation": 0.04, "odds": 2.00, "closing_odds": 1.90},
        {"market": "over25", "deviation": 0.03, "odds": 2.00, "closing_odds": None},
        {"market": "over25", "deviation": 0.12, "odds": 2.00, "closing_odds": 2.10},
        {"market": "btts", "deviation": 0.04, "odds": 1.80, "closing_odds": 1.80},
    ])
    out = cg.aggregate_shadow_clv(df)
    assert set(out) == {"over25", "btts"}
    low = out["over25"]["0-5"]
    assert (low["n_banda"], low["n_con_closing"]) == (2, 1)
    assert low["avg_clv"] == pytest.approx(1 / 1.90 - 1 / 2.00, abs=1e-5)
    assert out["over25"]["10-15"]["avg_clv"] < 0
    assert out["btts"]["0-5"]["avg_clv"] == 0.0


# ============================================================
# D1-raíz — el closing de producción cubre resueltas y shadow
# (que el shadow se cierre, con o sin bets, lo ejecutan los tests de
# test_round7_audit.py sobre el closing real)
# ============================================================

def test_production_closing_includes_resolved_bets(monkeypatch):
    """El filtro pending-only dejaba bets resueltas sin closing para
    siempre (btts 0/22 medido en ronda 7). Se verifica la consulta que el
    closing de producción envía de verdad."""
    from tests.test_round7_audit import _closing_world
    _, sql_seen, _, _, _ = _closing_world(monkeypatch, lambda k: [])
    bets_sql = next(s for s in sql_seen if "FROM bets_history" in s)
    assert "result IN ('pending','win','loss','half_win','half_loss','push')" in bets_sql
    assert "INTERVAL '7 days'" in bets_sql      # ventana de recuperación


def test_shadow_closing_has_kickoff_guard(monkeypatch):
    """El cierre del shadow se busca solo para partidos que ya están a
    menos de 90 min del kickoff (no en cuanto se crea la fila: registraría
    el precio de ~un día antes) y hasta 10 días hacia atrás."""
    from tests.test_round7_audit import _closing_world
    _, sql_seen, _, _, _ = _closing_world(monkeypatch, lambda k: [])
    shadow_sql = next(s for s in sql_seen if "FROM shadow_bets" in s)
    assert "NOW() + INTERVAL '90 minutes'" in shadow_sql
    assert "NOW() - INTERVAL '10 days'" in shadow_sql


# ============================================================
# D3/R17 — población declarada del sweep
# ============================================================

def test_sweep_reports_population_counters(monkeypatch, capsys):
    """Tres contadores: pares con cuota, con referencia, sobre el piso.
    Sin ellos la muestra no declara qué fracción del slate representa.
    Verificado ejecutando el pipeline (22-sep-26)."""
    import re
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    log = capsys.readouterr().out
    m = re.search(r"pares con cuota: (\d+) · con referencia: (\d+) \((\d+)/(\d+)\) · sobre piso 2pt: (\d+)", log)
    assert m, "falta la línea de población del sweep"
    total, ref, ref2, total2, swept = map(int, m.groups())
    assert (ref, total) == (ref2, total2) and swept <= ref <= total
    assert swept == len(out["shadow"])


# ============================================================
# D2 — la proyección ya no cita 280/semana sin dedupe
# ============================================================

def test_slate_window_is_three_days(monkeypatch):
    """El slate del pipeline es NOW()+1h..NOW()+3d → cada partido aparece
    en ~3 slates; cualquier tasa semanal debe dividir entre ~3. Se verifica
    la consulta que el pipeline envía."""
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    slate = next(s for s in out["sql"] if "FROM upcoming_matches" in s)
    assert "NOW() + INTERVAL '1 hour' AND NOW() + INTERVAL '3 days'" in slate
