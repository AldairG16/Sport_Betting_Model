"""
tests/test_round12_audit.py
===========================
Tests de la Ronda 12: herencia del warm-start desde Neon (G1/R15) y
existencia/estructura del documento operativo de salida de papel (G2).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter

DOC = Path(__file__).parent.parent / "docs" / "SALIDA_PAPEL.md"


# ============================================================
# G1/R15 — el warm-start lee la fuente de verdad
# ============================================================

def test_warm_start_prefers_neon(monkeypatch, tmp_path):
    """El weekly de CI (sin archivo local) hereda el fit anterior desde
    model_state — sin esto, cada lunes producía un fit desde zeros
    (home_adv 0.59, +45% en λ_home) que sobrescribía el bueno. Con los dos
    disponibles, gana Neon."""
    stale = tmp_path / "dc_params.json"
    stale.write_text(json.dumps({"home_adv": 0.59}))
    monkeypatch.setattr(fitter, "DC_PARAMS_FILE", stale)
    monkeypatch.setattr(fitter, "_load_params_from_db", lambda: {"home_adv": 0.24})
    assert fitter._previous_fit() == {"home_adv": 0.24}


def test_file_remains_as_fallback_only(monkeypatch, tmp_path):
    """R15: el archivo local solo se usa si Neon no tiene fit; si tampoco
    hay archivo (o está corrupto), se arranca desde cero."""
    local = tmp_path / "dc_params.json"
    monkeypatch.setattr(fitter, "DC_PARAMS_FILE", local)
    monkeypatch.setattr(fitter, "_load_params_from_db", lambda: {})
    assert fitter._previous_fit() == {}
    local.write_text(json.dumps({"home_adv": 0.3}))
    assert fitter._previous_fit() == {"home_adv": 0.3}
    local.write_text("{roto")
    assert fitter._previous_fit() == {}


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


def test_r13_window_rate_and_floor_cap_declared():
    """R16/R13: ventana acumulada declarada, tasa de activación al lado del
    umbral, y el techo del piso (H2) documentado."""
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "ACUMULADA desde el inicio de la generación" in text
    assert "piso_max(mercado) = 0.35 × (techo − 5pt) − slip(mercado)" in text
    assert "Tasa de activación por mercado (R16" in text
    assert "Si ningún mercado habilita nunca" in text


def test_fit_fingerprint_is_baseline_of_the_doc():
    """§0: el documento declara la generación vigente — sin eso el primer
    corte mezclaría las bets pre-huella."""
    text = DOC.read_text(encoding="utf-8")
    assert "fit_fingerprint" in text
    assert "Cohortes excluidas" in text
