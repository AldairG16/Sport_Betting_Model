"""
Ensemble Model — Tres Señales Independientes
=============================================
Combina tres fuentes de probabilidad independientes para
reducir el error de predicción y aumentar la robustez del modelo.

Por qué un ensemble:
  - Cada señal captura un ángulo distinto del partido
  - Cuando las tres señales COINCIDEN → alta confianza, edge más confiable
  - Cuando DIVERGEN → baja confianza, el modelo es más conservador
  - Reduce el riesgo de sobreajuste a una sola metodología

Las tres señales:
  1. Dixon-Coles Poisson (form + xG + H2H)  → captura forma reciente
  2. ELO puro                                → captura nivel histórico
  3. Form Gradient (attack/defense puro)     → captura momentum
"""

import numpy as np
from scipy.stats import poisson
from src.models.dixon_coles_model import match_outcomes, MAX_GOALS

# =========================
# PESOS BASE
# =========================

W_DIXON  = 0.50   # Dixon-Coles: señal principal (más sofisticada)
W_ELO    = 0.30   # ELO: rating histórico de largo plazo
W_FORM   = 0.20   # Form puro: momentum reciente sin ELO

ELO_HOME_ADV  = 80    # Puntos ELO de ventaja local (calibrado Europa)
ELO_SCALE     = 400   # Escala estándar ELO


# =========================
# SEÑAL 2: ELO PROBABILITY
# =========================

def _elo_probs(elo_home: float, elo_away: float) -> tuple[float, float, float]:
    """
    Convierte ratings ELO a probabilidades 1x2.

    ELO solo predice binario (ganar/perder), así que estimamos
    el empate usando un factor paramétrico calibrado en el fútbol europeo:
    P(draw) es mayor cuando los equipos están muy igualados.
    """
    elo_home_adj = elo_home + ELO_HOME_ADV
    p_home_raw   = 1 / (1 + 10 ** ((elo_away - elo_home_adj) / ELO_SCALE))
    p_away_raw   = 1 - p_home_raw

    # P(draw) es mayor cuando ambos son iguales (|p_home - p_away| ≈ 0)
    # y menor cuando hay un gran favorito
    balance   = 1 - abs(p_home_raw - p_away_raw)   # [0, 1]
    p_draw    = 0.12 + 0.18 * balance               # [0.12, 0.30]

    # Redistribuir el espacio del empate proporcional
    remainder = 1 - p_draw
    p_home    = p_home_raw * remainder
    p_away    = p_away_raw * remainder

    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


# =========================
# SEÑAL 3: FORM GRADIENT
# =========================

def _form_probs(
    home_attack: float, home_defense: float,
    away_attack: float, away_defense: float,
    home_advantage: float = 1.10,
    tempo: float = 1.20
) -> tuple[float, float, float]:
    """
    Calcula probabilidades solo desde los ratings de forma (ataque/defensa),
    sin blend de ELO. Es independiente de la señal Dixon-Coles del pipeline.
    """
    lh = np.clip(home_attack * away_defense * home_advantage * tempo, 0.3, 2.8)
    la = np.clip(away_attack * home_defense * tempo, 0.3, 2.8)
    return match_outcomes(lh, la)


# =========================
# AGREEMENT SCORE
# =========================

def _agreement(p1, p2, p3) -> float:
    """
    Mide qué tan de acuerdo están las tres señales.

    Usa la divergencia promedio entre las distribuciones.
    Cuando todas apuntan al mismo ganador con probabilidades similares → 1.0
    Cuando divergen completamente → 0.0

    Returns: float [0, 1]
    """
    probs = [list(p1), list(p2), list(p3)]

    # Promedio de cada resultado
    avg = [
        (probs[0][i] + probs[1][i] + probs[2][i]) / 3
        for i in range(3)
    ]

    # Varianza promedio entre señales para cada resultado
    variance = sum(
        sum((probs[s][i] - avg[i]) ** 2 for s in range(3)) / 3
        for i in range(3)
    )

    # Normalizar: varianza máxima teórica ~0.33, mapeamos a [0, 1]
    agreement = max(0.0, 1.0 - variance / 0.15)
    return round(min(1.0, agreement), 3)


# =========================
# PESOS ADAPTATIVOS
# =========================

# MEJORA #8 (Sprint 3) — Pesos del ensemble configurables vía JSON.
# Si config/ensemble_weights.json tiene pesos para un mercado dado, se usan
# esos en lugar de los hardcoded. Permite tuneo sin tocar código y prepara
# terreno para un stacking model real (LR/GBM sobre predicciones).
import json as _json
from pathlib import Path as _Path

_ENSEMBLE_WEIGHTS_FILE = (
    _Path(__file__).parent.parent.parent / "config" / "ensemble_weights.json"
)
_ensemble_weights_cache: dict | None = None


def _load_ensemble_weights() -> dict:
    """Carga los pesos overrideados desde JSON (cache en memoria)."""
    global _ensemble_weights_cache
    if _ensemble_weights_cache is not None:
        return _ensemble_weights_cache
    try:
        if _ENSEMBLE_WEIGHTS_FILE.exists():
            _ensemble_weights_cache = _json.loads(_ENSEMBLE_WEIGHTS_FILE.read_text())
        else:
            _ensemble_weights_cache = {}
    except Exception:
        _ensemble_weights_cache = {}
    return _ensemble_weights_cache


def _agreement_band(agreement: float) -> str:
    """Convierte agreement [0,1] en bucket discreto."""
    if agreement >= 0.80: return "high"
    if agreement >= 0.50: return "med"
    return "low"


def _adaptive_weights(agreement: float, elo_available: bool,
                       market: str | None = None) -> tuple[float, float, float]:
    """
    Ajusta los pesos según el nivel de acuerdo entre señales.

    Alta coincidencia → Dixon-Coles domina (es la señal más sofisticada)
    Baja coincidencia → más peso a ELO (señal más estable a largo plazo)
    Sin ELO           → redistribuye peso entre Dixon y Form

    Mejora #8: si `market` se provee y hay override en
    config/ensemble_weights.json para (market, agreement_band), usa esos
    pesos en lugar de los hardcoded. Esto permite tuneo sin tocar código.
    """
    if not elo_available:
        return 0.70, 0.00, 0.30

    # Override desde JSON
    if market is not None:
        weights = _load_ensemble_weights()
        by_market = weights.get("by_market", {})
        market_node = by_market.get(market, {})
        band_weights = market_node.get(_agreement_band(agreement))
        if isinstance(band_weights, (list, tuple)) and len(band_weights) == 3:
            try:
                w_dc, w_elo, w_form = (float(x) for x in band_weights)
                # Sanity check — pesos deben sumar ~1 y ser positivos
                total = w_dc + w_elo + w_form
                if 0.95 <= total <= 1.05 and min(w_dc, w_elo, w_form) >= 0:
                    return w_dc / total, w_elo / total, w_form / total
            except (TypeError, ValueError):
                pass   # mal formado, cae a defaults

    # Defaults hardcoded (los mismos que antes)
    if agreement >= 0.80:
        return 0.60, 0.25, 0.15
    elif agreement >= 0.50:
        return 0.50, 0.30, 0.20
    else:
        return 0.35, 0.45, 0.20


# =========================
# ENSEMBLE PRINCIPAL
# =========================

def ensemble_predict(
    dc_probs:     tuple[float, float, float],
    elo_home:     float,
    elo_away:     float,
    home_attack:  float,
    home_defense: float,
    away_attack:  float,
    away_defense: float,
    market:       str | None = None,
) -> dict:
    """
    Combina las tres señales en una predicción unificada.

    Args:
        dc_probs:     (p_home, p_draw, p_away) de Dixon-Coles
        elo_home:     rating ELO del equipo local
        elo_away:     rating ELO del equipo visitante
        home_attack:  rating de ataque del local (form-based)
        home_defense: rating de defensa del local
        away_attack:  rating de ataque del visitante
        away_defense: rating de defensa del visitante

    Returns:
        {
            "home_win":   float  — prob. final ponderada
            "draw":       float
            "away_win":   float
            "agreement":  float  — nivel de acuerdo [0, 1]
            "confidence_boost": float — ajuste de confianza sugerido
        }
    """
    # Señal 1: Dixon-Coles (ya calculada por el pipeline)
    dc_h, dc_d, dc_a = dc_probs

    # Señal 2: ELO puro
    elo_available = (elo_home > 0 and elo_away > 0)
    if elo_available:
        elo_h, elo_d, elo_a = _elo_probs(elo_home, elo_away)
    else:
        elo_h, elo_d, elo_a = dc_h, dc_d, dc_a   # fallback

    # Señal 3: Form puro (sin ELO)
    form_h, form_d, form_a = _form_probs(
        home_attack, home_defense, away_attack, away_defense
    )

    # Acuerdo entre señales
    agreement = _agreement(
        (dc_h, dc_d, dc_a),
        (elo_h, elo_d, elo_a),
        (form_h, form_d, form_a)
    )

    # Pesos adaptativos (con override por mercado si existe en JSON — Mejora #8)
    w_dc, w_elo, w_form = _adaptive_weights(agreement, elo_available, market=market)

    # Blend final
    h = dc_h * w_dc + elo_h * w_elo + form_h * w_form
    d = dc_d * w_dc + elo_d * w_elo + form_d * w_form
    a = dc_a * w_dc + elo_a * w_elo + form_a * w_form

    # Normalizar
    total = h + d + a
    h /= total
    d /= total
    a /= total

    # Ajuste de confianza: alta coincidencia → más seguro
    if agreement >= 0.80:
        confidence_boost = +0.10
    elif agreement >= 0.50:
        confidence_boost = 0.0
    else:
        confidence_boost = -0.10

    return {
        "home_win":        round(h, 4),
        "draw":            round(d, 4),
        "away_win":        round(a, 4),
        "agreement":       agreement,
        "confidence_boost": confidence_boost,
        "weights":         (round(w_dc, 2), round(w_elo, 2), round(w_form, 2))
    }
