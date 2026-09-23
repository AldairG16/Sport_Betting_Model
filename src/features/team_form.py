import pandas as pd
from config.database import engine
from src.utils.team_normalizer import normalize_team
from sqlalchemy import text

# =========================
# CONFIGURACION DE FORMA
# =========================

# ── FILTRO DE KALMAN (14-sep-26) ─────────────────────────────────────────
# Reemplaza el promedio con decay exponencial por un filtro de Kalman de
# estado escalar: la fuerza de ataque/defensa del equipo es un estado
# oculto que evoluciona como paseo aleatorio y se observa con el ruido de
# los goles (alta varianza). Ventajas sobre el decay:
#   - Incertidumbre recursiva: tras un periodo estable, un cambio repentino
#     se absorbe a ritmo constante; nunca se sobre-reacciona a 1-2 partidos.
#   - Equipos con pocos partidos se encogen hacia la media de la liga
#     (shrinkage jerárquico rudimentario) en vez de dar ratings extremos.
#   - Limitación documentada: no ajusta por la defensa del rival (eso lo
#     hacen los blends del pipeline: xG, DC-MLE).
KALMAN_Q          = 0.10   # varianza de proceso: cuánto puede cambiar la
                           # fuerza real de un equipo por partido
KALMAN_R          = 1.00   # varianza de observación: ruido de los goles
KALMAN_BASELINE   = 1.35   # goles promedio de liga por equipo/partido
                           # (estado inicial = equipo promedio)

# Decay clásico — se mantiene solo para la métrica de puntos (display)
DECAY = 0.85

# Ventana de partidos a consultar
FORM_WINDOW      = 12   # combinado (home + away)
FORM_WINDOW_VENUE = 8   # solo local o solo visitante (menos partidos disponibles)
FORM_MIN_VENUE   = 4    # mínimo para usar forma de venue (si no, usa combinada)


def _kalman_filter(goals_seq: list[float]):
    """
    Filtro de Kalman escalar sobre una secuencia de goles en orden
    cronológico (más viejo → más reciente).

    Returns: (estimación_final, varianza_final)
    """
    x = KALMAN_BASELINE     # estado inicial: equipo promedio
    p = 0.50                # varianza inicial: incertidumbre moderada
    for g in goals_seq:
        if g is None or (isinstance(g, float) and pd.isna(g)):
            continue
        p += KALMAN_Q                 # el estado puede haber cambiado
        k = p / (p + KALMAN_R)        # ganancia de Kalman
        x += k * (g - x)              # actualizar hacia la observación
        p = (1 - k) * p               # la incertidumbre baja al observar
    return x, p


# =========================
# FORM CON FILTRO DE KALMAN
# =========================

def get_team_form(team, venue: str = None, cutoff_date=None):
    """
    Calcula las métricas de forma de un equipo con filtro de Kalman.

    Args:
        team:         nombre del equipo
        venue:        "home"  → solo partidos en casa
                      "away"  → solo partidos fuera
                      None    → combinado (comportamiento original)
        cutoff_date:  opcional. Si se pasa, excluye partidos en date >= cutoff.
                      CRÍTICO para backtest: evita temporal leakage al usar
                      partidos que aún no habían ocurrido al momento de la bet.

    Returns:
        dict con métricas de forma o None si DB vacía. attack_rating y
        defense_rating están en escala de GOLES POR PARTIDO (1.35 = promedio).
    """
    team = normalize_team(team)

    # ── Construir filtro SQL según venue ──────────────────────────────────
    if venue == "home":
        where_clause = "LOWER(home_team) = LOWER(:team)"
        window       = FORM_WINDOW_VENUE
    elif venue == "away":
        where_clause = "LOWER(away_team) = LOWER(:team)"
        window       = FORM_WINDOW_VENUE
    else:
        where_clause = "(LOWER(home_team) = LOWER(:team) OR LOWER(away_team) = LOWER(:team))"
        window       = FORM_WINDOW

    # Cutoff temporal para evitar leakage en backtesting
    params = {"team": team, "window": window}
    if cutoff_date is not None:
        where_clause += " AND date < :cutoff"
        params["cutoff"] = pd.to_datetime(cutoff_date).strftime("%Y-%m-%d")

    df = pd.read_sql(
        text(f"""
            SELECT home_team, away_team, home_goals, away_goals, date
            FROM matches
            WHERE {where_clause}
            ORDER BY date DESC
            LIMIT :window
        """),
        engine,
        params=params
    )

    # ── Si usamos venue y hay pocos datos → fallback a forma combinada ────
    if venue and len(df) < FORM_MIN_VENUE:
        return get_team_form(team, venue=None, cutoff_date=cutoff_date)

    # =========================
    # CASO 1: HAY DATOS REALES
    # =========================

    if not df.empty:
        # Kalman necesita orden cronológico (más viejo → más reciente)
        df_chrono = df.iloc[::-1]

        goals_seq, conceded_seq = [], []
        points_w   = 0.0
        total_weight = 0.0

        for i, (_, row) in enumerate(df_chrono.iterrows()):
            if row.home_team.lower() == team.lower():
                gs, gc = row.home_goals, row.away_goals
            else:
                gs, gc = row.away_goals, row.home_goals
            if gs is None or gc is None or pd.isna(gs) or pd.isna(gc):
                continue

            goals_seq.append(float(gs))
            conceded_seq.append(float(gc))

            # métrica de puntos con decay clásico (solo display)
            weight = DECAY ** i
            points_w += (3 if gs > gc else 1 if gs == gc else 0) * weight
            total_weight += weight

        matches = int(len(goals_seq))
        if matches == 0:
            return get_team_form(team, venue=None, cutoff_date=cutoff_date) \
                if venue else None

        att, p_att = _kalman_filter(goals_seq)
        deff, p_def = _kalman_filter(conceded_seq)
        # Incertidumbre expuesta: la usa el pipeline para escalar stakes por
        # confianza del modelo (Constantinou: apostar más cuando estás seguro)
        uncertainty = round((p_att + p_def) / 2, 3)

        return {
            "matches":        matches,
            "points":         round(points_w, 2),
            "goals_scored":   round(sum(goals_seq) / matches, 2),
            "goals_conceded": round(sum(conceded_seq) / matches, 2),
            "attack_rating":  round(att, 3),
            "defense_rating": round(deff, 3),
            "uncertainty":    uncertainty,
            "is_fallback":    False,
            "venue":          venue or "combined",
        }

    # =========================
    # CASO 2: SIN HISTORIAL → FALLBACK HONESTO
    # =========================

    df_all = pd.read_sql(
        "SELECT home_goals, away_goals FROM matches",
        engine
    )

    if df_all.empty:
        return None

    avg_goals = (df_all["home_goals"].mean() + df_all["away_goals"].mean()) / 2

    return {
        "matches":        0,
        "points":         0,
        "goals_scored":   round(avg_goals * FORM_WINDOW, 2),
        "goals_conceded": round(avg_goals * FORM_WINDOW, 2),
        "attack_rating":  round(avg_goals, 3),
        "defense_rating": round(avg_goals, 3),
        "is_fallback":    True,
        "venue":          venue or "combined",
    }
