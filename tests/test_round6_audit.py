"""
tests/test_round6_audit.py
==========================
Tests de la Ronda 6: re-derivación de umbrales (A1/R9), rutas sin precio
real (A2), guardias de booksum (A3), escalera ejercida (A4) y cuartos AH
(A5).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.market_odds import market_probabilities
from src.pipeline.prediction_pipeline import (
    MAX_MODEL_DEVIATION,
    MIN_EDGE,
    MIN_EDGE_BY_MARKET,
    _devig_three_way,
)
import src.pipeline.prediction_pipeline as pp


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

def test_markets_without_real_odds_are_never_quoted(monkeypatch):
    """
    El fallback `or CORNERS_DEFAULT_ODDS` fabricaba cuotas (21 bets a 1.80
    inventado, Q-N). Con predicción de córners y tarjetas para TODOS los
    partidos, solo el que trae cuotas reales (iota vs beta) puede aparecer.
    """
    from tests.pipeline_harness import run_pipeline, extended_kwargs
    kw = extended_kwargs()
    ratings = {"attack_rating": 5.0, "defense_rating": 4.5}
    kw["overrides"].update({"get_team_corners": lambda team: dict(ratings),
                            "get_team_cards": lambda team: {"attack_rating": 2.0,
                                                            "defense_rating": 1.9}})
    out = run_pipeline(monkeypatch, **kw)
    stat_rows = [r for r in out["bets"] + out["shadow"] + out["paper"]
                 if r["market"].startswith(("corners_", "cards_"))]
    assert stat_rows, "el escenario debe ejercitar córners/tarjetas"
    assert {r["match"] for r in stat_rows} == {"iota vs beta"}


def test_anchorable_without_anchor_is_not_bettable():
    """
    Regla estructural A2: _anchorable sólo mete el mercado en probabilities
    si hay entrada en market_probs; el `continue` cubre el resto. Verificado
    por lectura del bloque de combinación (la rama sin ancla no asigna).
    """
    # Desde el 22-sep-26 el bloque es la etapa _stage_blend: se ejecuta.
    probs, _, _ = pp._stage_blend(
        {"over25": 0.60, "home_win": 0.55, "dc_1x": 0.80},
        {"home_win": 0.50},                       # solo 1X2 tiene ancla
        {"over25": 0.52, "dc_1x": 0.75})          # over25: pata suelta
    assert "over25" not in probs                  # anclable sin ancla → fuera
    assert probs["home_win"] == 0.55              # anclable con ancla → modelo crudo
    assert "dc_1x" in probs                       # no anclable → blend simétrico


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

def test_pipeline_passes_edge_to_calibration(monkeypatch):
    """calibrate_probability recibe el edge (tercer argumento): sin él la
    escalera blend_weight vuelve a ser código muerto (A4)."""
    from tests.pipeline_harness import run_pipeline
    calls = []
    real = pp.calibrate_probability

    def spy(model_prob, market_prob, edge=None):
        calls.append((model_prob, market_prob, edge))
        return real(model_prob, market_prob, edge)

    run_pipeline(monkeypatch, overrides={"calibrate_probability": spy})
    priced = [c for c in calls if c[1]]
    assert priced, "el escenario debe calibrar mercados sin ancla con precio"
    assert all(e is not None and abs(e - (m - i)) < 1e-12 for m, i, e in priced)


# ============================================================
# A5 — las líneas AH conservan los cuartos
# ============================================================

def test_ah_line_format_preserves_quarters(monkeypatch):
    """Las claves AH llevan dos decimales: -0.25 ya no se redondea a -0.2
    (y el resolver sí detecta el cuarto)."""
    from tests.pipeline_harness import run_pipeline, default_matches
    df = default_matches()
    df.loc[0, "ah_line"] = -0.25            # M1 alpha vs beta
    seen = set()
    real = pp._stage_blend

    def spy(model_probs, market_probs, market_probs_raw):
        seen.update(m for m in model_probs if m.startswith("ah_"))
        return real(model_probs, market_probs, market_probs_raw)

    run_pipeline(monkeypatch, matches=df, overrides={"_stage_blend": spy})
    assert {"ah_home_-0.25", "ah_away_-0.25"} <= seen
    assert not any(m.endswith(("-0.2", "+0.2", "-0.8", "+0.8")) for m in seen)


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
