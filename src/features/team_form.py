import pandas as pd
import numpy as np
from config.database import engine
from src.utils.team_normalizer import normalize_team
from sqlalchemy import text

# =========================
# CONFIGURACION DE FORMA
# =========================

# Factor de decay: partido mas reciente = peso 1.0
# El partido de hace N partidos tiene peso DECAY^N
# DECAY=0.85 → partido hace 5 partidos vale 0.85^5 ≈ 44% del mas reciente
DECAY = 0.85

# Ventana de partidos a consultar
FORM_WINDOW      = 12   # combinado (home + away)
FORM_WINDOW_VENUE = 8   # solo local o solo visitante (menos partidos disponibles)
FORM_MIN_VENUE   = 4    # mínimo para usar forma de venue (si no, usa combinada)


# =========================
# FORM CON DECAY EXPONENCIAL
# =========================

def get_team_form(team, venue: str = None, cutoff_date=None):
    """
    Calcula las métricas de forma de un equipo con decay exponencial.

    Args:
        team:         nombre del equipo
        venue:        "home"  → solo partidos en casa
                      "away"  → solo partidos fuera
                      None    → combinado (comportamiento original)
        cutoff_date:  opcional. Si se pasa, excluye partidos en date >= cutoff.
                      CRÍTICO para backtest: evita temporal leakage al usar
                      partidos que aún no habían ocurrido al momento de la bet.

    Mejoras vs versión anterior:
      - Parámetro venue: permite separar forma local vs visitante
      - home_attack/home_defense más precisos para el pipeline
      - Fallback a forma combinada si hay < FORM_MIN_VENUE partidos de venue
      - Parámetro cutoff_date: evita look-ahead bias en backtesting.

    Returns:
        dict con métricas de forma o None si DB vacía
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

        goals_scored_w   = 0.0
        goals_conceded_w = 0.0
        points_w         = 0.0
        total_weight     = 0.0

        for i, (_, row) in enumerate(df.iterrows()):

            weight = DECAY ** i

            if row.home_team.lower() == team.lower():
                gs = row.home_goals
                gc = row.away_goals
            else:
                gs = row.away_goals
                gc = row.home_goals

            goals_scored_w   += gs * weight
            goals_conceded_w += gc * weight
            total_weight     += weight

            if gs > gc:
                points_w += 3 * weight
            elif gs == gc:
                points_w += 1 * weight

        matches    = len(df)
        eff_weight = total_weight if total_weight > 0 else 1.0

        return {
            "matches":        matches,
            "points":         round(points_w, 2),
            "goals_scored":   round(goals_scored_w, 2),
            "goals_conceded": round(goals_conceded_w, 2),
            "attack_rating":  round(goals_scored_w / eff_weight, 3),
            "defense_rating": round(goals_conceded_w / eff_weight, 3),
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
