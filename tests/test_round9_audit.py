"""
tests/test_round9_audit.py
==========================
Tests de la Ronda 9: unidad de agregación del shadow (D1/R12), el closing
de producción ve resueltas y rellena shadow (D1-raíz), contadores de
población del sweep (D3/R17).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.clv_gate as cg
import scripts.update_closing_odds as uco
import src.pipeline.prediction_pipeline as pp
import src.models.save_bets as sb


def _source(module):
    return inspect.getsource(module)


# ============================================================
# D1/R12 — agregación en la unidad de la decisión
# ============================================================

def test_shadow_clv_groups_by_market_and_band():
    """La conclusión CLV debe agregarse por (mercado × banda): el piso es
    por mercado, no global."""
    src = _source(cg)
    assert 'groupby(["market", "band"])' in src
    assert "n_con_closing" in src          # "hay filas" ≠ "hay medición"
    assert '"by_market"' in src


# ============================================================
# D1-raíz — el closing de producción cubre resueltas y shadow
# ============================================================

def test_production_closing_includes_resolved_bets():
    """El filtro pending-only dejaba bets resueltas sin closing para
    siempre (btts 0/22 medido en ronda 7)."""
    src = _source(uco)
    assert "result IN ('pending','win','loss','half_win','half_loss','push')" in src
    assert "INTERVAL '7 days'" in src      # ventana de recuperación


def test_production_closing_fills_shadow():
    """El shadow se rellena donde corre el closing real (el gemelo de
    save_bets casi no corre en producción)."""
    src = _source(uco)
    assert "_update_shadow_closing" in src


def test_shadow_closing_has_kickoff_guard():
    """El 'closing' debe capturarse cerca del kickoff, no en cuanto se
    crea la fila (si no, registra el precio de ~un día antes)."""
    src = _source(sb)
    assert "INTERVAL '90 minutes'" in src
    assert "INTERVAL '10 days'" in src


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

def test_slate_window_is_three_days():
    """El slate del pipeline es NOW()+1h..NOW()+3d → cada partido aparece
    en ~3 slates; cualquier tasa semanal debe dividir entre ~3."""
    src = _source(pp)
    assert "INTERVAL '3 days'" in src
    assert "INTERVAL '1 hour'" in src
