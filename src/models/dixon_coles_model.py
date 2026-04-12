from scipy.stats import poisson

# =========================
# PARAMETROS DIXON-COLES
# =========================

# Iteracion hasta 10 goles (anterior: 6)
# Captura >99.9% de la distribucion de probabilidad real en futbol
MAX_GOALS = 10

# Parametro rho: dependencia entre goles bajos en futbol
# Valor empirico calibrado con datos europeos (Dixon & Coles 1997)
# Negativo = los 0-0, 1-0, 0-1, 1-1 ocurren MAS de lo que predice Poisson puro
RHO = -0.13


# =========================
# CORRECCION TAU
# =========================

def _tau(x, y, home_lambda, away_lambda, rho=RHO):
    """
    Correccion Dixon-Coles para resultados de baja puntuacion.

    Poisson asume independencia entre goles de local y visitante.
    En realidad, en futbol existe dependencia negativa: cuando el marcador
    es ajustado (0-0, 1-0, 0-1, 1-1), los equipos cambian su comportamiento
    (defensa, contraataque, tiempo de posesion).

    Esta correccion ajusta las 4 celdas criticas de la matriz.
    Para resultados con >= 2 goles, tau = 1.0 (sin correccion).
    """
    if x == 0 and y == 0:
        return 1.0 - home_lambda * away_lambda * rho
    elif x == 1 and y == 0:
        return 1.0 + away_lambda * rho
    elif x == 0 and y == 1:
        return 1.0 + home_lambda * rho
    elif x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


# =========================
# MODELO PRINCIPAL
# =========================

def match_outcomes(home_lambda, away_lambda, max_goals=MAX_GOALS):
    """
    Calcula P(home_win), P(draw), P(away_win) con modelo Dixon-Coles.

    Mejoras vs version anterior:
      - max_goals: 10 (vs 6 anterior) → captura el 99.9% de la distribucion
      - Correccion tau para resultados 0-0 / 1-0 / 0-1 / 1-1
      - Normalizacion final por si tau introduce desviacion marginal

    Args:
        home_lambda: goles esperados del local
        away_lambda: goles esperados del visitante
        max_goals:   techo de iteracion (default 10)

    Returns:
        (p_home_win, p_draw, p_away_win) — suman 1.0
    """
    home = 0.0
    draw = 0.0
    away = 0.0

    for i in range(max_goals):
        for j in range(max_goals):

            p = (
                poisson.pmf(i, home_lambda)
                * poisson.pmf(j, away_lambda)
                * _tau(i, j, home_lambda, away_lambda)
            )

            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p

    # Normalizar: tau puede introducir una desviacion muy pequena
    total = home + draw + away
    if total > 0:
        home /= total
        draw /= total
        away /= total

    return home, draw, away
