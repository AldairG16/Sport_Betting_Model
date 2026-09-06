"""
Line Movement Tracker
=====================
Detecta movimiento de línea comparando las odds de apertura
(primera vez que se vio el partido) vs las odds actuales.

Teoría del sharp money:
  - Las casas abren líneas con su mejor estimación
  - El "public money" no mueve mucho la línea (apuestas pequeñas, muchos)
  - El "sharp money" SÍ la mueve: son pocos apostadores pero con mucho capital
    y track record positivo → las casas ajustan rápido

Señal:
  - Odds HOME bajan  (2.10 → 1.85) = dinero entrando en HOME  = señal sharp HOME
  - Odds AWAY bajan  (3.50 → 2.90) = dinero entrando en AWAY  = señal sharp AWAY
  - Odds DRAW suben  mientras otras bajan = mercado polarizando

Integración al pipeline:
  - Si señal sharp CONFIRMA nuestra value bet → +10% confianza
  - Si señal sharp CONTRADICE nuestra value bet → -10% confianza (o skip)
  - Sin señal → neutral (sin cambio)
"""

import pandas as pd
from sqlalchemy import text
from config.database import engine

# =========================
# UMBRALES
# =========================

MOVEMENT_THRESHOLD  = 0.05   # Mínimo 5% de cambio para considerar movimiento
STRONG_MOVEMENT     = 0.10   # Más de 10% = movimiento fuerte (probable sharp)
CONFIDENCE_BOOST    = 0.10   # +10% confianza si sharp confirma
CONFIDENCE_PENALTY  = 0.10   # -10% confianza si sharp contradice

# Filtro duro: si la línea cayó MÁS de este % en contra de nuestra apuesta
# el edge ya fue detectado por el mercado y probablemente desapareció → skip
# 04-may-26: bajamos de 0.08 → 0.05 después del audit que detectó CLV invertido.
# El modelo pierde dinero cuando la línea se mueve contra él → ser más estricto
# evita esas pérdidas (157 bets CLV+ → -23u sugiere edge falso post-movimiento).
HARD_SKIP_THRESHOLD = 0.05   # 5% de caída = descartar (era 8%)
MIN_BOOKMAKERS      = 4      # Menos de 4 casas = mercado poco líquido → skip


# =========================
# HELPERS
# =========================

def _pct_change(opening: float, current: float) -> float:
    """Cambio porcentual de odds. Negativo = odds bajaron (shortening)."""
    if not opening or not current or opening <= 0:
        return 0.0
    return (current - opening) / opening


def _detect_sharp_side(home_mv: float, draw_mv: float, away_mv: float) -> str:
    """
    Determina hacia dónde fue el dinero sharp.

    Lógica:
      - La línea que más bajó = lado que recibió dinero
      - Solo reporta señal si el movimiento supera MOVEMENT_THRESHOLD
    """
    movements = {
        "home_win": home_mv,
        "draw":     draw_mv,
        "away_win": away_mv
    }

    # Filtramos los que bajaron (odds más cortas = dinero entrando)
    shortenings = {k: v for k, v in movements.items() if v < -MOVEMENT_THRESHOLD}

    if not shortenings:
        return "none"

    # El que más bajó es el lado sharp
    sharp_side = min(shortenings, key=shortenings.get)
    return sharp_side


# =========================
# MAIN FUNCTION
# =========================

def get_line_movement(match_key: str) -> dict:
    """
    Calcula el movimiento de línea para un partido.

    Returns:
        {
            "home_movement":      float  — cambio % en odds home (neg = shortening)
            "draw_movement":      float  — cambio % en odds draw
            "away_movement":      float  — cambio % en odds away
            "over25_movement":    float  — cambio % en over 2.5
            "sharp_signal":       str    — "home_win" | "away_win" | "draw" | "none"
            "movement_strength":  str    — "strong" | "moderate" | "none"
            "has_movement":       bool   — True si hay movimiento significativo
        }
    """
    try:
        df = pd.read_sql(
            text("""
                SELECT
                    opening_home_odds, opening_draw_odds, opening_away_odds,
                    opening_over25_odds,
                    home_odds, draw_odds, away_odds, over25_odds
                FROM upcoming_matches
                WHERE match_key = :key
                LIMIT 1
            """),
            engine,
            params={"key": match_key}
        )
    except Exception:
        return _neutral_signal()

    if df.empty:
        return _neutral_signal()

    row = df.iloc[0]

    home_mv    = _pct_change(row.opening_home_odds,   row.home_odds)
    draw_mv    = _pct_change(row.opening_draw_odds,   row.draw_odds)
    away_mv    = _pct_change(row.opening_away_odds,   row.away_odds)
    over25_mv  = _pct_change(row.opening_over25_odds, row.over25_odds)

    sharp_signal = _detect_sharp_side(home_mv, draw_mv, away_mv)

    max_abs_mv = max(abs(home_mv), abs(draw_mv), abs(away_mv))

    if max_abs_mv >= STRONG_MOVEMENT:
        strength = "strong"
    elif max_abs_mv >= MOVEMENT_THRESHOLD:
        strength = "moderate"
    else:
        strength = "none"

    return {
        "home_movement":     round(home_mv, 4),
        "draw_movement":     round(draw_mv, 4),
        "away_movement":     round(away_mv, 4),
        "over25_movement":   round(over25_mv, 4),
        "sharp_signal":      sharp_signal,
        "movement_strength": strength,
        "has_movement":      strength != "none"
    }


def _neutral_signal() -> dict:
    return {
        "home_movement":     0.0,
        "draw_movement":     0.0,
        "away_movement":     0.0,
        "over25_movement":   0.0,
        "sharp_signal":      "none",
        "movement_strength": "none",
        "has_movement":      False
    }


# =========================
# APLICAR SEÑAL AL PIPELINE
# =========================

def apply_line_movement_signal(
    bet_market: str,
    base_edge: float,
    base_confidence: float,
    line: dict
) -> tuple[float, float]:
    """
    Ajusta edge y confianza de una apuesta según la señal de línea.

    Args:
        bet_market:       mercado de la apuesta (ej. "home_win", "away_win")
        base_edge:        edge calculado por el modelo
        base_confidence:  confianza calculada por el pipeline
        line:             resultado de get_line_movement()

    Returns:
        (adjusted_edge, adjusted_confidence)
    """
    if not line["has_movement"]:
        return base_edge, base_confidence

    sharp = line["sharp_signal"]
    strength = line["movement_strength"]
    boost = CONFIDENCE_BOOST if strength == "strong" else CONFIDENCE_BOOST * 0.5

    if sharp == bet_market:
        # Sharp money CONFIRMA nuestra apuesta
        new_confidence = min(1.0, base_confidence + boost)
        new_edge = base_edge * (1 + boost * 0.5)
        return round(new_edge, 4), round(new_confidence, 4)

    elif sharp != "none" and sharp != bet_market:
        # Sharp money CONTRADICE nuestra apuesta
        new_confidence = max(0.0, base_confidence - boost)
        new_edge = base_edge * (1 - boost * 0.5)
        return round(new_edge, 4), round(new_confidence, 4)

    return base_edge, base_confidence


def line_moved_against(bet_market: str, line: dict) -> bool:
    """
    Filtro DURO: True si la línea cayó más de HARD_SKIP_THRESHOLD
    en la dirección de nuestra apuesta.

    Si queremos home_win pero las odds del local bajaron >8% desde la
    apertura, el mercado sharp ya absorbió ese edge — no apostar.

    Args:
        bet_market: "home_win" | "away_win" | "draw" | "over25" | "under25"
        line:       resultado de get_line_movement()

    Returns:
        True → descartar la apuesta
    """
    market_to_movement = {
        "home_win": line["home_movement"],
        "draw":     line["draw_movement"],
        "away_win": line["away_movement"],
        "over25":   line["over25_movement"],
        "under25":  -line["over25_movement"],  # under sube cuando over baja
    }
    movement = market_to_movement.get(bet_market, 0.0)
    # Odds cayeron en nuestra dirección más del umbral → edge absorbido
    return movement < -HARD_SKIP_THRESHOLD


def should_skip_low_liquidity(bk_count) -> bool:
    """
    Filtro DURO: True si hay menos de MIN_BOOKMAKERS casas de apuestas.
    Mercados poco líquidos tienen odds poco fiables.

    Returns:
        True → descartar el partido completo
    """
    if bk_count is None:
        return False
    return int(bk_count) < MIN_BOOKMAKERS
