"""
Motivation Factor
=================
Ajusta la confianza del modelo según la motivación real de cada equipo.

Equipos sin nada que ganar o perder juegan diferente:
  - Campeón asegurado en jornada 35+ → puede rotar plantilla → rendimiento menor
  - Equipo descendido matemáticamente → sin motivación → rendimiento menor
  - Equipo en zona de descenso con 3 jornadas → desesperación → rinden MÁS
  - Equipo buscando clasificación a Europa → rendimiento normal o mayor
  - Playoff/final → máxima motivación para ambos

Fuente de datos: tabla `matches` (calculamos standings desde resultados reales)
Sin llamadas a API externas — todo desde la DB.
"""

import sys
from pathlib import Path
import pandas as pd
from sqlalchemy import text
from datetime import datetime, timedelta

sys.path.append(str(Path(__file__).parent.parent.parent))
from config.database import engine

# ── Umbrales de ajuste ────────────────────────────────────────────────────────
CHAMPION_BOOST       = -0.08   # Campeón asegurado → puede no esforzarse (-8%)
RELEGATED_BOOST      = -0.08   # Descendido matemáticamente → sin motivación (-8%)
SAFE_BOOST           =  0.00   # Zona media tranquila → sin ajuste
EUROPE_BOOST         =  0.04   # Peleando por Europa → ligero extra (+4%)
RELEGATION_FIGHT     =  0.06   # En zona de descenso, fin de temporada → presión (+6%)
TITLE_FIGHT          =  0.05   # Peleando el título, no asegurado → presión (+5%)

# Mínimo de partidos jugados para calcular standings confiables
MIN_MATCHES_SEASON   = 10

# Ligas con formato de temporada claro (no aplica a Copa, playoffs únicos)
LEAGUE_SIZES = {
    "soccer_epl":                         20,
    "soccer_spain_la_liga":               20,
    "soccer_germany_bundesliga":          18,
    "soccer_italy_serie_a":               20,
    "soccer_france_ligue_one":            18,
    "soccer_efl_champ":                   24,
    "soccer_netherlands_eredivisie":      18,
    "soccer_portugal_primeira_liga":      18,
    "soccer_brazil_campeonato":           20,
    "soccer_argentina_primera_division":  30,
    "soccer_mexico_ligamx":               18,
}

# Número de equipos que descienden por liga
RELEGATION_SPOTS = {
    "soccer_epl":                         3,
    "soccer_spain_la_liga":               3,
    "soccer_germany_bundesliga":          3,
    "soccer_italy_serie_a":               3,
    "soccer_france_ligue_one":            3,
    "soccer_efl_champ":                   3,
    "soccer_netherlands_eredivisie":      2,
    "soccer_portugal_primeira_liga":      2,
    "soccer_brazil_campeonato":           4,
    "soccer_argentina_primera_division":  0,  # no hay descenso directo
    "soccer_mexico_ligamx":               0,  # sistema de playoffs
}

# Spots de clasificación a torneos europeos/continentales
EUROPE_SPOTS = {
    "soccer_epl":               6,
    "soccer_spain_la_liga":     6,
    "soccer_germany_bundesliga":6,
    "soccer_italy_serie_a":     7,
    "soccer_france_ligue_one":  5,
    "soccer_netherlands_eredivisie": 3,
    "soccer_portugal_primeira_liga": 3,
}


def _get_current_standings(league: str, season_start: str = None) -> pd.DataFrame:
    """
    Calcula la tabla de posiciones actual desde la tabla matches.

    Returns:
        DataFrame con columnas: team, pts, played, won, drawn, lost, gf, ga, gd, position
        Vacío si no hay datos suficientes.
    """
    if season_start is None:
        # Asumir temporada actual: si estamos antes de julio, la temporada empezó
        # en agosto del año anterior
        now = datetime.now()
        if now.month < 7:
            season_start = f"{now.year - 1}-07-01"
        else:
            season_start = f"{now.year}-07-01"

    try:
        df = pd.read_sql(
            text("""
                SELECT home_team, away_team, home_goals, away_goals, date, league
                FROM matches
                WHERE league = :league
                  AND date >= :since
                  AND home_goals IS NOT NULL
                  AND away_goals IS NOT NULL
                ORDER BY date
            """),
            engine,
            params={"league": league, "since": season_start}
        )
    except Exception:
        return pd.DataFrame()

    if len(df) < MIN_MATCHES_SEASON:
        return pd.DataFrame()

    # Construir tabla desde resultados
    teams = set(df["home_team"].tolist() + df["away_team"].tolist())
    records = []

    for team in teams:
        home = df[df["home_team"] == team]
        away = df[df["away_team"] == team]

        hw  = int((home["home_goals"] > home["away_goals"]).sum())
        hd  = int((home["home_goals"] == home["away_goals"]).sum())
        hl  = int((home["home_goals"] < home["away_goals"]).sum())

        aw  = int((away["away_goals"] > away["home_goals"]).sum())
        ad  = int((away["away_goals"] == away["home_goals"]).sum())
        al  = int((away["away_goals"] < away["home_goals"]).sum())

        won    = hw + aw
        drawn  = hd + ad
        lost   = hl + al
        played = won + drawn + lost
        pts    = won * 3 + drawn
        gf     = int(home["home_goals"].sum()) + int(away["away_goals"].sum())
        ga     = int(home["away_goals"].sum()) + int(away["home_goals"].sum())

        records.append({
            "team": team, "pts": pts, "played": played,
            "won": won, "drawn": drawn, "lost": lost,
            "gf": gf, "ga": ga, "gd": gf - ga
        })

    standings = pd.DataFrame(records)
    if standings.empty:
        return standings

    standings = standings.sort_values(
        ["pts", "gd", "gf"], ascending=False
    ).reset_index(drop=True)
    standings["position"] = standings.index + 1

    return standings


def get_motivation_factor(team: str, league: str) -> float:
    """
    Retorna un factor de ajuste de confianza para el equipo.

    Valores:
      > 0.0  → equipo más motivado de lo normal  (boost)
      < 0.0  → equipo menos motivado de lo normal (penalty)
      = 0.0  → sin ajuste

    Args:
        team:   nombre del equipo
        league: sport_key de la liga
    """
    if league not in LEAGUE_SIZES:
        return 0.0  # No aplica a copas/torneos sin liga regular

    standings = _get_current_standings(league)
    if standings.empty:
        return 0.0

    n_teams     = LEAGUE_SIZES[league]
    n_relegated = RELEGATION_SPOTS.get(league, 3)
    n_europe    = EUROPE_SPOTS.get(league, 6)

    # Buscar el equipo (normalización flexible)
    team_lower = team.lower()
    match = standings[standings["team"].str.lower().str.contains(
        team_lower[:8], na=False, regex=False
    )]

    if match.empty:
        return 0.0

    row      = match.iloc[0]
    position = int(row["position"])
    played   = int(row["played"])
    pts      = int(row["pts"])

    if played < MIN_MATCHES_SEASON:
        return 0.0

    # Calcular partidos restantes aproximados
    games_remaining = n_teams - 1 - played  # ligas de vuelta doble
    if games_remaining < 0:
        games_remaining = 0

    # Máximo puntos posibles
    max_pts_possible = pts + games_remaining * 3

    # ── Líder con título asegurado ─────────────────────────────────────────
    if position == 1 and games_remaining <= 5:
        second_pts = int(standings[standings["position"] == 2].iloc[0]["pts"]) if len(standings) > 1 else 0
        if pts - second_pts > games_remaining * 3:
            return CHAMPION_BOOST  # -8%

    # ── Peleando el título (top 2, últimas jornadas) ───────────────────────
    if position <= 2 and games_remaining <= 8:
        return TITLE_FIGHT  # +5%

    # ── Descendido matemáticamente ─────────────────────────────────────────
    if n_relegated > 0 and games_remaining <= 5:
        safe_zone_pts = int(standings[standings["position"] == n_teams - n_relegated].iloc[0]["pts"]) \
                        if len(standings) >= n_teams - n_relegated else 0
        if max_pts_possible < safe_zone_pts:
            return RELEGATED_BOOST  # -8%

    # ── En zona de descenso con pocas jornadas ────────────────────────────
    if n_relegated > 0 and position > n_teams - n_relegated - 2:
        if games_remaining <= 6:
            return RELEGATION_FIGHT  # +6%

    # ── Peleando por Europa ────────────────────────────────────────────────
    if position <= n_europe + 2 and games_remaining <= 8:
        return EUROPE_BOOST  # +4%

    return 0.0  # zona media, sin ajuste
