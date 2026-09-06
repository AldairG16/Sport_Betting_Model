"""
Fixture Congestion
==================
Detecta fatiga por acumulación de partidos recientes.

Evidencia empírica (Lago-Peñas et al., 2011 / Dupont et al., 2010):
  - 3 días de descanso vs 4+:  -8% rendimiento físico
  - 3 partidos en 8 días:     -12% rendimiento general
  - Descanso > 7 días:        +2% respecto a la media (equipo "fresco")

Aplica multiplicadores sobre attack_rating y defense_rating ANTES de
calcular lambda, para que la corrección se propague al modelo Poisson.

Nota: el impacto en defensa es menor que en ataque (≈50% del efecto)
porque la fatiga afecta más la capacidad ofensiva (pressing, sprints)
que la organización defensiva.
"""

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import text

from config.database import engine

# ─── Umbrales de descanso (días entre el último partido y el próximo) ──────
FRESH_DAYS   = 7    # >= 7 días → equipo fresco
NORMAL_DAYS  = 4    # 4-6 días → normal
TIRED_DAYS   = 3    # exactamente 3 días → cansado
FATIGUED_DAYS = 2   # <= 2 días → muy fatigado

# ─── Multiplicadores de ataque según descanso ────────────────────────────
MULT_FRESH    = 1.02   # bien descansado → ligero boost
MULT_NORMAL   = 1.00   # sin ajuste
MULT_TIRED    = 0.93   # 3 días
MULT_FATIGUED = 0.86   # 2 días o menos

# ─── Penalización extra por triple congestion (3+ partidos en 10 días) ───
TRIPLE_PENALTY = 0.94

# ─── Proporción del efecto que aplica a defensa ──────────────────────────
DEFENSE_EFFECT_RATIO = 0.50


def get_fixture_congestion(team: str, match_date) -> dict:
    """
    Calcula el multiplicador de fatiga para un equipo antes de su próximo partido.

    Args:
        team:       nombre del equipo (normalizado o sin normalizar)
        match_date: fecha del próximo partido (datetime, Timestamp, o str ISO)

    Returns:
        {
            "attack_multiplier":  float  — [0.78, 1.02]
            "defense_multiplier": float  — [0.90, 1.01]
            "days_rest":          int    — días desde el último partido
            "matches_in_10d":     int    — partidos jugados en los últimos 10 días
            "is_fatigued":        bool   — True si days_rest < 4 o triple congestion
        }
    """
    team_lower = team.lower().strip()

    # ── Normalizar fecha ──────────────────────────────────────────────────
    # `matches.date` es TIMESTAMP tz-naive, pero `upcoming_matches.match_date`
    # es ahora TIMESTAMPTZ (tz-aware UTC). Forzamos TODO a naive-UTC para
    # evitar "Cannot subtract tz-naive and tz-aware datetime-like objects".
    ts = pd.Timestamp(match_date)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    match_date = ts.to_pydatetime()

    # ── Consultar últimos 5 partidos del equipo antes del partido en cuestión
    try:
        df = pd.read_sql(
            text("""
                SELECT date
                FROM matches
                WHERE (LOWER(home_team) = :team OR LOWER(away_team) = :team)
                  AND date < :match_date
                ORDER BY date DESC
                LIMIT 5
            """),
            engine,
            params={"team": team_lower, "match_date": match_date},
        )
    except Exception:
        return _default_congestion()

    if df.empty:
        return _default_congestion()

    # Forzar ambos lados de la resta a naive (matches.date ya es naive, pero
    # por defensa contra un futuro cambio a TIMESTAMPTZ en matches).
    df["date"] = pd.to_datetime(df["date"], utc=False)
    if getattr(df["date"].dt, "tz", None) is not None:
        df["date"] = df["date"].dt.tz_convert("UTC").dt.tz_localize(None)

    # ── Días de descanso ─────────────────────────────────────────────────
    last_match = df["date"].iloc[0]
    md_naive = pd.Timestamp(match_date)  # ya es naive tras el bloque anterior
    days_rest  = max(0, (md_naive - last_match).days)

    # ── Partidos en los últimos 10 días ──────────────────────────────────
    cutoff_10d     = md_naive - timedelta(days=10)
    matches_in_10d = int((df["date"] >= cutoff_10d).sum())

    # ── Multiplicador base ───────────────────────────────────────────────
    if days_rest >= FRESH_DAYS:
        attack_mult = MULT_FRESH
    elif days_rest >= NORMAL_DAYS:
        attack_mult = MULT_NORMAL
    elif days_rest >= TIRED_DAYS:
        attack_mult = MULT_TIRED
    else:
        attack_mult = MULT_FATIGUED

    # ── Penalización extra por triple congestion ─────────────────────────
    if matches_in_10d >= 3:
        attack_mult *= TRIPLE_PENALTY

    # ── Multiplicador de defensa (efecto parcial) ─────────────────────────
    defense_delta = (attack_mult - 1.0) * DEFENSE_EFFECT_RATIO
    defense_mult  = 1.0 + defense_delta

    # ── Caps de seguridad ─────────────────────────────────────────────────
    attack_mult  = round(max(0.78, min(attack_mult,  1.05)), 3)
    defense_mult = round(max(0.90, min(defense_mult, 1.03)), 3)

    return {
        "attack_multiplier":  attack_mult,
        "defense_multiplier": defense_mult,
        "days_rest":          days_rest,
        "matches_in_10d":     matches_in_10d,
        "is_fatigued":        days_rest < NORMAL_DAYS or matches_in_10d >= 3,
    }


def _default_congestion() -> dict:
    """Retorna valores neutros cuando no hay datos históricos."""
    return {
        "attack_multiplier":  1.0,
        "defense_multiplier": 1.0,
        "days_rest":          7,
        "matches_in_10d":     0,
        "is_fatigued":        False,
    }
