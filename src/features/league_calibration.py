"""
League Calibration
==================
Factores de ajuste específicos por liga calculados desde datos históricos reales.

Por qué importa:
  - El modelo actual usa HOME_ADVANTAGE=1.10 y TEMPO=1.2 para TODAS las ligas
  - Pero la Bundesliga tiene ~3.06 goles/partido vs Argentina con ~2.23
  - Brasil tiene ventaja local de 1.47x vs Serie A de 1.19x
  - Usar los mismos parámetros para todas introduce error sistemático

Mejora v2 — Over/Under por Liga:
  El mercado over/under varía MUCHO por liga:
    Eredivisie:   62% de partidos terminan over 2.5
    Championship: 47% de partidos terminan over 2.5
    Argentina:    38% de partidos terminan over 2.5

  El campo "over25_rate" es la tasa histórica real de over 2.5 por liga.
  Se usa en el pipeline para calibrar las probabilidades de totales:
    final_over25 = poisson_over25 * (1-OVER25_SHRINK) + league_over25_rate * OVER25_SHRINK
  donde OVER25_SHRINK = 0.20 (20% de shrinkage hacia la media de la liga)

Liga                        | Home ADV | Tempo  | Draw Rate | Over25
soccer_epl                  |  1.214   | 1.129  |  23.6%    | 53.8%
soccer_italy_serie_a        |  1.190   | 1.094  |  25.5%    | 51.2%
soccer_spain_la_liga        |  1.319   | 1.051  |  26.1%    | 49.8%
soccer_france_ligue_one     |  1.256   | 1.081  |  25.4%    | 48.1%
soccer_germany_bundesliga   |  1.253   | 1.225  |  24.7%    | 62.1%
soccer_netherlands_eredivisie| 1.278   | 1.228  |  23.3%    | 62.4%
soccer_brazil_campeonato    |  1.470   | 0.956  |  26.9%    | 42.3%
soccer_argentina            |  1.330   | 0.892  |  30.9%    | 38.1%
"""

# =========================
# FACTORES POR LIGA
# =========================
# home_advantage: ratio avg_home_goals / avg_away_goals
# tempo:          ratio avg_total_goals / 2.5 (baseline europeo)
# draw_rate:      tasa histórica de empates [0,1]
# over25_rate:    % histórico de partidos con más de 2.5 goles [0,1]

LEAGUE_FACTORS = {
    "soccer_epl": {
        "home_advantage": 1.214,
        "tempo":          1.129,
        "draw_rate":      0.236,
        "over25_rate":    0.538,  # 53.8% partidos over 2.5
    },
    "soccer_italy_serie_a": {
        "home_advantage": 1.190,
        "tempo":          1.094,
        "draw_rate":      0.255,
        "over25_rate":    0.512,
    },
    "soccer_spain_la_liga": {
        "home_advantage": 1.319,
        "tempo":          1.051,
        "draw_rate":      0.261,
        "over25_rate":    0.498,
    },
    "soccer_france_ligue_one": {
        "home_advantage": 1.256,
        "tempo":          1.081,
        "draw_rate":      0.254,
        "over25_rate":    0.481,  # liga más defensiva de las Big5
    },
    "soccer_uefa_champs_league": {
        "home_advantage": 1.215,
        "tempo":          1.102,
        "draw_rate":      0.265,
        "over25_rate":    0.532,
    },
    "soccer_germany_bundesliga": {
        "home_advantage": 1.253,
        "tempo":          1.225,
        "draw_rate":      0.247,
        "over25_rate":    0.621,  # liga más goleadora de las Big5
    },
    "soccer_brazil_campeonato": {
        "home_advantage": 1.470,
        "tempo":          0.956,
        "draw_rate":      0.269,
        "over25_rate":    0.423,
    },
    "soccer_mexico_ligamx": {
        "home_advantage": 1.306,
        "tempo":          1.064,
        "draw_rate":      0.271,
        "over25_rate":    0.498,
    },
    "soccer_argentina_primera_division": {
        "home_advantage": 1.330,
        "tempo":          0.892,
        "draw_rate":      0.309,
        "over25_rate":    0.381,  # liga más baja en goles
    },
    "soccer_usa_mls": {
        "home_advantage": 1.180,
        "tempo":          1.050,
        "draw_rate":      0.220,
        "over25_rate":    0.487,
    },

    # Nuevas ligas europeas — valores calculados de datos reales
    "soccer_efl_champ": {
        "home_advantage": 1.261,
        "tempo":          1.011,
        "draw_rate":      0.267,
        "over25_rate":    0.471,  # Championship más defensivo que EPL
    },
    "soccer_netherlands_eredivisie": {
        "home_advantage": 1.278,
        "tempo":          1.228,
        "draw_rate":      0.233,
        "over25_rate":    0.624,  # la más alta — ~3.07 goles/partido
    },
    "soccer_portugal_primeira_liga": {
        "home_advantage": 1.252,
        "tempo":          1.039,
        "draw_rate":      0.239,
        "over25_rate":    0.493,
    },
    "soccer_spl": {
        "home_advantage": 1.228,
        "tempo":          1.089,
        "draw_rate":      0.240,
        "over25_rate":    0.512,
    },
    "soccer_uefa_europa_league": {
        "home_advantage": 1.190,
        "tempo":          1.070,
        "draw_rate":      0.255,
        "over25_rate":    0.521,
    },

    # Copa Libertadores — clubes sudamericanos, alta motivación, mercado menos eficiente
    # Equipos argentinos/brasileños ya tienen datos históricos parciales en la DB
    "soccer_conmebol_copa_libertadores": {
        "home_advantage": 1.420,   # ventaja local muy alta en Sudamérica
        "tempo":          0.970,
        "draw_rate":      0.275,
        "over25_rate":    0.445,
    },

    # Clasificatorias Mundial Europa — selecciones, alta motivación, partidos cerrados
    # Mercado menos eficiente que ligas top (menos volumen de apuestas)
    "soccer_fifa_world_cup_qualifiers_europe": {
        "home_advantage": 1.280,   # ventaja local notable en selecciones
        "tempo":          1.010,
        "draw_rate":      0.290,   # más empates que en ligas (partidos más disputados)
        "over25_rate":    0.468,   # tendencia defensiva en clasificatorias
    },

    # MLB (beisbol) — sin empates, promedio ~4.5 carreras por equipo
    "baseball_mlb": {
        "home_advantage": 1.050,   # ventaja local pequena en MLB (~5%)
        "tempo":          1.000,   # ~4.5 carreras/equipo = baseline
        "draw_rate":      0.000,   # no hay empates en beisbol
        "over25_rate":    0.500,   # placeholder (no se usa para MLB)
    },
}

# Valores por defecto si la liga no está en el mapa
DEFAULT_FACTORS = {
    "home_advantage": 1.200,
    "tempo":          1.050,
    "draw_rate":      0.260,
    "over25_rate":    0.510,  # media europea
}

# Peso del shrinkage de la tasa histórica de liga sobre el Poisson
# 0.20 = 20% liga histórica + 80% Poisson del partido
OVER25_SHRINK = 0.20


# =========================
# API PÚBLICA
# =========================

def get_league_factors(league: str) -> dict:
    """
    Retorna los factores de calibración para una liga específica.

    Args:
        league: sport_key de la liga (ej. "soccer_epl")

    Returns:
        {
            "home_advantage": float  — multiplicador lambda_home
            "tempo":          float  — multiplicador de goles totales
            "draw_rate":      float  — tasa histórica de empates
        }
    """
    # Búsqueda exacta
    if league in LEAGUE_FACTORS:
        return LEAGUE_FACTORS[league].copy()

    # Búsqueda parcial (ej. "soccer_epl_extra" → "soccer_epl")
    for key, factors in LEAGUE_FACTORS.items():
        if key in league or league in key:
            return factors.copy()

    return DEFAULT_FACTORS.copy()


def get_lambda_multipliers(league: str) -> tuple[float, float]:
    """
    Retorna (home_advantage, tempo) listos para usar en el pipeline.

    Usage:
        home_adv, tempo = get_lambda_multipliers(row.league)
        lambda_home = home_attack * away_defense * home_adv * tempo
        lambda_away = away_attack * home_defense * tempo
    """
    f = get_league_factors(league)
    return f["home_advantage"], f["tempo"]


def get_over25_rate(league: str) -> float:
    """
    Retorna la tasa histórica real de over 2.5 para una liga.
    Usado para calibrar las probabilidades de totales del modelo Poisson.

    Usage en pipeline:
        league_over25 = get_over25_rate(row.league)
        final_over25  = poisson_over25 * (1 - OVER25_SHRINK) + league_over25 * OVER25_SHRINK

    Returns:
        float en [0, 1] — e.g. 0.62 para Eredivisie, 0.48 para Ligue 1
    """
    return get_league_factors(league).get("over25_rate", DEFAULT_FACTORS["over25_rate"])
