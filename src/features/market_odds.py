import pandas as pd
import numpy as np
from scipy.optimize import brentq


# =========================
# SHIN METHOD — Overround Removal
# =========================

def _shin_fair_probs(raw_probs):
    """
    Elimina el margen del bookmaker usando el metodo Shin.

    El metodo Shin es superior a la normalizacion proporcional simple porque
    distribuye el margen de forma realista: los favoritos (odds bajas)
    absorben mas margen que los outsiders (odds altas), igual que hace
    el mercado real.

    Retorna: (fair_probs, overround_pct)
    """
    total = sum(raw_probs)
    overround_pct = round((total - 1.0) / total * 100, 2)

    if abs(total - 1.0) < 1e-6:
        return raw_probs, 0.0

    # Ecuacion de Shin: encontrar z tal que sum de fair_probs = 1
    def equation(z):
        if z >= 1:
            return 1e9
        return sum(
            (np.sqrt(z**2 + 4 * (1 - z) * (p / total)**2) - z) / (2 * (1 - z))
            for p in raw_probs
        ) - 1.0

    try:
        z = brentq(equation, 1e-9, 0.5, maxiter=200)
        fair = [
            (np.sqrt(z**2 + 4 * (1 - z) * (p / total)**2) - z) / (2 * (1 - z))
            for p in raw_probs
        ]
        return fair, overround_pct

    except Exception:
        # Fallback: normalizacion proporcional clasica
        return [p / total for p in raw_probs], overround_pct


# =========================
# API PUBLICA
# =========================

def market_probabilities(home_odds, draw_odds, away_odds):
    """
    Convierte odds de mercado a probabilidades limpias (sin margen del bookmaker).
    Usa el metodo Shin para una distribucion del margen mas precisa.

    Retorna: (p_home, p_draw, p_away) o (None, None, None) si odds invalidas.
    """
    if pd.isna(home_odds) or pd.isna(draw_odds) or pd.isna(away_odds):
        return None, None, None

    try:
        if home_odds <= 1 or draw_odds <= 1 or away_odds <= 1:
            return None, None, None

        raw = [1 / home_odds, 1 / draw_odds, 1 / away_odds]
        fair, overround_pct = _shin_fair_probs(raw)

        # Margen demasiado alto = bookmaker sospechoso (> 12%)
        if overround_pct > 12.0:
            # Normalizacion simple como fallback conservador
            total = sum(raw)
            fair = [p / total for p in raw]

        return fair[0], fair[1], fair[2]

    except Exception:
        return None, None, None


def get_overround(home_odds, draw_odds, away_odds):
    """
    Retorna el margen del bookmaker en porcentaje.
    Util para filtrar bookmakers muy agresivos.
    Ej: Pinnacle ~2.5%, bet365 ~5-7%, otros >8%
    """
    try:
        if any(pd.isna(o) or o <= 1 for o in [home_odds, draw_odds, away_odds]):
            return None
        raw = [1/home_odds, 1/draw_odds, 1/away_odds]
        total = sum(raw)
        return round((total - 1.0) / total * 100, 2)
    except Exception:
        return None


# 🔥 BLEND (backward compatible — sin cambios)
def blend_model_market(model_prob, market_prob, weight_model=0.85, confidence=None):

    if market_prob is None:
        return model_prob

    if pd.isna(model_prob):
        return market_prob

    if confidence is not None:
        weight_model = 0.6 + (0.35 * confidence)  # 0.6 → 0.95

    weight_market = 1 - weight_model
    blended = (model_prob * weight_model) + (market_prob * weight_market)

    return max(0.001, min(0.999, blended))
