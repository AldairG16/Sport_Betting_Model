def calibrate_probability(model_prob, market_prob, edge=None):

    if market_prob is None:
        return model_prob

    # 🔥 seguridad
    model_prob = max(0.001, min(0.999, model_prob))
    market_prob = max(0.001, min(0.999, market_prob))

    if edge is None:
        weight_model = 0.65

    else:
        abs_edge = abs(edge)

        # 🔥 DIRECCIÓN DEL EDGE (CLAVE)
        if edge > 0:  # modelo mejor

            if abs_edge > 0.20:
                weight_model = 0.90
            elif abs_edge > 0.12:
                weight_model = 0.82
            elif abs_edge > 0.06:
                weight_model = 0.72
            elif abs_edge > 0.03:
                weight_model = 0.64
            else:
                weight_model = 0.58

        else:  # mercado mejor

            if abs_edge > 0.20:
                weight_model = 0.50
            elif abs_edge > 0.12:
                weight_model = 0.55
            elif abs_edge > 0.06:
                weight_model = 0.60
            elif abs_edge > 0.03:
                weight_model = 0.62
            else:
                weight_model = 0.65

    weight_market = 1 - weight_model

    calibrated = (
        model_prob * weight_model +
        market_prob * weight_market
    )

    return max(0.001, min(0.999, calibrated))


def is_strong_edge(edge, confidence):

    abs_edge = abs(edge)

    if abs_edge > 0.10 and confidence >= 0.7:
        return True

    if abs_edge > 0.07 and confidence >= 0.9:
        return True

    if abs_edge > 0.15:
        return True

    return False