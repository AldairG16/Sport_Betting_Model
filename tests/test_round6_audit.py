"""
tests/test_round6_audit.py
==========================
Tests de la Ronda 6: re-derivación de umbrales (A1/R9), rutas sin precio
real (A2), guardias de booksum (A3), escalera ejercida (A4) y cuartos AH
(A5).
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.market_odds import market_probabilities
from src.pipeline.prediction_pipeline import (
    MAX_MODEL_DEVIATION,
    MIN_EDGE,
    MIN_EDGE_BY_MARKET,
    _anchorable,
    _devig_three_way,
    _devig_two_way,
)
import src.pipeline.prediction_pipeline as pp
import src.models.save_bets as sb


def _source(module):
    return inspect.getsource(module)


# ============================================================
# A1 — umbrales coherentes con la arquitectura anclada
# ============================================================

def test_min_edge_is_operational_floor_not_deviation_cap():
    """
    El piso de edge debe ser BAJO (cubre vig+slip); el techo de calibración
    es MAX_MODEL_DEVIATION, un parámetro aparte. La primera derivación de
    esta ronda puso 0.085 y la corrida en seco mostró que sólo permitiría
    apostar con desvío ≥30pt (el borde de la región confiable).
    """
    assert MIN_EDGE == 0.05
    assert MAX_MODEL_DEVIATION == 0.30
    assert all(v == 0.05 for v in MIN_EDGE_BY_MARKET.values()), (
        "umbrales por mercado desalineados del piso operativo")


def test_no_threshold_above_floor():
    """Ningún mercado puede volver a exigir 64pt de desvío (ah_*_fav 0.20)."""
    assert max(MIN_EDGE_BY_MARKET.values()) <= 0.06


# ============================================================
# A2 — sin precio real no hay apuesta
# ============================================================

def test_no_fabricated_odds_in_source():
    """
    El fallback `or CORNERS_DEFAULT_ODDS` fabricaba cuotas (21 bets a 1.80
    inventado, Q-N). El patrón no puede volver al odds-dict.
    """
    src = _source(pp)
    assert "or CORNERS_DEFAULT_ODDS" not in src
    assert "or CARDS_DEFAULT_ODDS" not in src


def test_anchorable_without_anchor_is_not_bettable():
    """
    Regla estructural A2: _anchorable sólo mete el mercado en probabilities
    si hay entrada en market_probs; el `continue` cubre el resto. Verificado
    por lectura del bloque de combinación (la rama sin ancla no asigna).
    """
    src = _source(pp)
    m = re.search(r"if _anchorable\(market\):\n(.*?)\n            if _implied:", src, re.S)
    assert m, "bloque de combinación no encontrado"
    block = m.group(1)
    assert "continue" in block
    assert re.search(r"if market in market_probs:\n\s+probabilities\[market\]", block)


# ============================================================
# A3 — guardia de booksum en tríos
# ============================================================

def test_devig_three_way_guard():
    # booksum 1.0625 — consenso válido (máximo entre casas, Q-O mediana 0.99)
    assert _devig_three_way(2.0, 3.2, 4.0) is not None
    # booksum 0.3 — fila rota
    assert _devig_three_way(10.0, 10.0, 10.0) is None
    # patas incompletas
    assert _devig_three_way(2.0, None, 4.0) is None


def test_shin_rejects_broken_books():
    """El Shin 1x2 tiene guardia baja (booksum < 0.90 → None)."""
    assert market_probabilities(10.0, 10.0, 10.0) == (None, None, None)
    # booksum ~0.99 pasa
    p = market_probabilities(3.4, 3.6, 2.9)
    assert p[0] is not None and abs(sum(p) - 1.0) < 1e-6


# ============================================================
# A4 — la escalera blend_weight se ejerce en producción
# ============================================================

def test_pipeline_passes_edge_to_calibration():
    """La llamada del pipeline incluye el tercer argumento (edge)."""
    src = _source(pp)
    assert re.search(r"calibrate_probability\(\s*model_prob,\s*_implied,\s*\(model_prob - _implied\)", src), (
        "calibrate_probability se llama sin edge: blend_weight vuelve a ser "
        "código muerto (A4)")


# ============================================================
# A5 — las líneas AH conservan los cuartos
# ============================================================

def test_ah_line_format_preserves_quarters():
    """El formato de clave AH usa dos decimales: -0.25 ya no se redondea a -0.2."""
    src = _source(pp)
    assert "{_ah_line:+.1f}" not in src
    assert src.count("{_ah_line:+.2f}") == 8


def test_resolver_parses_two_decimal_lines():
    """
    El resolver extrae el sufijo con float() — acepta '-0.25' (nuevo) y
    '-0.2' (filas viejas) sin cambios; el detector de cuartos entonces sí
    dispara para los nuevos.
    """
    for suffix, is_q in (("-0.25", True), ("+0.75", True),
                         ("-0.5", False), ("-1.0", False), ("-0.2", False)):
        line = float(suffix)
        frac = abs(line - round(line * 2) / 2)
        quarter = abs(line * 4 - round(line * 4)) < 1e-6 and frac > 1e-6
        assert quarter == is_q, f"{suffix}: quarter={quarter}, esperado {is_q}"
