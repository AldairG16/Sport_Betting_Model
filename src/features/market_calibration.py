"""
Calibración de probabilidades modelo vs mercado.

RONDA 5 (N1/N2) — reescrita. La versión anterior tenía dos defectos medidos:

  1. `is_strong_edge` DESCARTABA el precio del mercado por completo a partir
     de ~7-10pp de desacuerdo (con confianza de producción 0.8-1.0, el umbral
     real era 7.1pp). Q-G midió la consecuencia: la brecha de calibración
     crece monótonamente con el edge — 6.7pt en el tramo 0-5% hasta 19.8-25pt
     (58pt con n=12) en los tramos donde el mercado se tiraba entero.
  2. El peso del blend dependía del SIGNO del desacuerdo: el modelo se
     auto-asignaba hasta 90% de peso justo en la dirección que produce
     apuestas y bajaba a 50% en la contraria — amplificador de sesgo de
     confirmación sin base estadística.

Regla nueva: peso del modelo SIMÉTRICO y DECRECIENTE en |edge|. Un prior
formado por dinero real gana credibilidad mientras mayor el desacuerdo; el
modelo aporta señal en la zona cercana al precio. Los mercados con devig
confiable ni siquiera pasan por aquí: los combina el ancla 65/35 del
pipeline (ver ANCHOR_PARAMETRIC_PREFIXES y el bloque de anclaje).
"""


def blend_weight(abs_edge: float) -> float:
    """Peso del modelo, simétrico y decreciente en el desacuerdo."""
    if abs_edge > 0.20:
        return 0.30
    if abs_edge > 0.12:
        return 0.35
    if abs_edge > 0.06:
        return 0.40
    if abs_edge > 0.03:
        return 0.45
    return 0.50


def calibrate_probability(model_prob, market_prob, edge=None):

    if market_prob is None:
        return model_prob

    # seguridad
    model_prob = max(0.001, min(0.999, model_prob))
    market_prob = max(0.001, min(0.999, market_prob))

    if edge is None:
        weight_model = 0.35
    else:
        weight_model = blend_weight(abs(edge))

    weight_market = 1 - weight_model

    calibrated = (
        model_prob * weight_model +
        market_prob * weight_market
    )

    return max(0.001, min(0.999, calibrated))
