"""
tests/test_round12_audit.py
===========================
Tests de la Ronda 12: herencia del warm-start desde Neon (G1/R15) y
existencia/estructura del documento operativo de salida de papel (G2).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter

DOC = Path(__file__).parent.parent / "docs" / "SALIDA_PAPEL.md"


def _source(module):
    return inspect.getsource(module)


# ============================================================
# G1/R15 — el warm-start lee la fuente de verdad
# ============================================================

def test_warm_start_prefers_neon():
    """El weekly de CI (sin archivo local) debe heredar el fit anterior
    desde model_state — sin esto, cada lunes producía un fit desde zeros
    (home_adv 0.59, +45% en λ_home) que sobrescribía el bueno."""
    src = _source(fitter)
    fit_src = src.split("def fit_dc_parameters", 1)[1]
    fit_src = fit_src.split("# ── Optimización", 1)[0]
    i_fix = fit_src.index("G1 (ronda 12)")
    i_db = fit_src.index("prev = _load_params_from_db() or {}")
    i_file = fit_src.index("DC_PARAMS_FILE.exists()")
    # dentro del fit: Neon primero, archivo solo como fallback
    assert i_fix < i_db < i_file


def test_file_remains_as_fallback_only():
    """R15: las lecturas restantes del archivo son fallback y caché —
    ninguna decide el comportamiento del CI."""
    src = _source(fitter)
    assert "if not prev and DC_PARAMS_FILE.exists():" in src
    # el loader de predicción ya era base-primero (r4)
    assert "_load_params_from_db()" in src


# ============================================================
# G2 — el documento de salida existe y tiene las puertas simétricas
# ============================================================

def test_salida_papel_doc_exists_with_required_sections():
    text = DOC.read_text(encoding="utf-8")
    for section in ("## §0 — Estado de partida", "## §6 — Bitácora"):
        assert section in text, f"falta {section}"


def test_enable_gate_mirrors_block_gate():
    """Habilitar exige el espejo del bloqueo: límite inferior del IC95 > 0.
    La puerta que abre exposición no puede ser más floja que la que
    protege."""
    text = DOC.read_text(encoding="utf-8")
    assert "límite INFERIOR del IC95 unilateral del CLV > 0" in text
    assert "no-descalificador" in text


def test_fit_fingerprint_is_baseline_of_the_doc():
    """§0: el documento declara la generación vigente — sin eso el primer
    corte mezclaría las bets pre-huella."""
    text = DOC.read_text(encoding="utf-8")
    assert "fit_fingerprint" in text
    assert "Cohortes excluidas" in text
