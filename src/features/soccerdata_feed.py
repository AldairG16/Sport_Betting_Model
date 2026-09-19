"""
src/features/soccerdata_feed.py
===============================
Puente con la librería `soccerdata` (scraping de FBref, Understat, ClubElo).

QUÉ APORTA (v1, 17-sep-26):
  - xG REAL por equipo (Understat, 5 grandes ligas) → sustituye al proxy de
    tiros en el blend del pipeline cuando está disponible.
  - Goles por jugador en CLUBES (Understat) → alimenta el modelo de
    goleadores (antes solo selecciones).
  - Ratings Club Elo → prior externo de validación.

DISEÑO:
  - El scraping corre SOLO en el weekly (CI, Python 3.11 donde las
    dependencias de Understat funcionan) y guarda en tablas de Neon.
  - El pipeline jamás scrapea: lee las tablas (lectura barata).
  - Todo defensivo: si soccerdata no está instalado, la fuente falla o el
    dato está viejo (>21 días), las lecturas devuelven None y el pipeline
    usa sus proxies habituales.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

STALE_DAYS = 21

# Mapeo sport_key (The Odds API) → liga de Understat (soccerdata)
UNDERSTAT_LEAGUES = {
    "soccer_epl": "ENG-Premier League",
    "soccer_spain_la_liga": "ESP-La Liga",
    "soccer_germany_bundesliga": "GER-Bundesliga",
    "soccer_italy_serie_a": "ITA-Serie A",
    "soccer_france_ligue_one": "FRA-Ligue 1",
}


def current_season() -> str:
    """Temporada en curso formato soccerdata (año de inicio): 2026-27 → '2026'."""
    today = datetime.now(timezone.utc)
    return str(today.year if today.month >= 8 else today.year - 1)


def _ensure_tables(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS team_xg_real (
            team TEXT NOT NULL, league TEXT NOT NULL, season TEXT NOT NULL,
            xg_for NUMERIC, xg_against NUMERIC, matches INT,
            source TEXT, updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (team, league, season)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS player_club_goals (
            player TEXT NOT NULL, team TEXT NOT NULL,
            league TEXT NOT NULL, season TEXT NOT NULL,
            goals INT, matches INT, xg NUMERIC,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (player, team, season)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS club_elo (
            club TEXT PRIMARY KEY, elo NUMERIC, rank INT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))


def _pick_col(df: pd.DataFrame, *candidates: str) -> str | None:
    """Primera columna existente de entre las candidatas (case-insensitive)."""
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def refresh_understat(seasons: list[str] | None = None, verbose: bool = True) -> dict:
    """
    Scraping de Understat: xG por equipo y goles por jugador de las 5 grandes
    ligas → tablas team_xg_real y player_club_goals en Neon.
    Requiere `soccerdata` instalado. Solo corre en CI (weekly).
    """
    try:
        import soccerdata as sd
    except ImportError:
        if verbose:
            print("   soccerdata no instalado — omitido")
        return {"status": "no_library"}

    seasons = seasons or [current_season()]
    teams_up, players_up = 0, 0
    errors = []

    for sport_key, league in UNDERSTAT_LEAGUES.items():
        for season in seasons:
            try:
                us = sd.Understat(league, season)
                ts = us.read_team_season_stats()
                ps = us.read_player_season_stats()
            except Exception as e:
                errors.append(f"{league}/{season}: {type(e).__name__}")
                continue

            team_col = _pick_col(ts, "team")
            xgf_col = _pick_col(ts, "xg_for", "xG", "xg")
            xga_col = _pick_col(ts, "xg_against", "xGA", "xga")
            m_col = _pick_col(ts, "matches", "n_matches")
            if not (team_col and xgf_col and xga_col):
                errors.append(f"{league}: columnas de equipo no encontradas")
                continue

            with engine.begin() as conn:
                _ensure_tables(conn)
                for _, r in ts.iterrows():
                    conn.execute(text("""
                        INSERT INTO team_xg_real
                            (team, league, season, xg_for, xg_against, matches, source)
                        VALUES (:team, :league, :season, :xgf, :xga, :m, 'understat')
                        ON CONFLICT (team, league, season) DO UPDATE SET
                            xg_for = EXCLUDED.xg_for,
                            xg_against = EXCLUDED.xg_against,
                            matches = EXCLUDED.matches,
                            source = 'understat',
                            updated_at = NOW()
                    """), {
                        "team": str(r[team_col]).strip(), "league": sport_key,
                        "season": season,
                        "xgf": float(r[xgf_col]) if pd.notna(r[xgf_col]) else None,
                        "xga": float(r[xga_col]) if pd.notna(r[xga_col]) else None,
                        "m": int(r[m_col]) if m_col and pd.notna(r[m_col]) else None,
                    })
                    teams_up += 1

            # Goles por jugador (para el scorer model de clubes)
            p_col = _pick_col(ps, "player")
            pteam_col = _pick_col(ps, "team")
            g_col = _pick_col(ps, "goals")
            pm_col = _pick_col(ps, "matches", "n_matches")
            xg_col = _pick_col(ps, "xG", "xg")
            if not (p_col and pteam_col and g_col):
                continue
            with engine.begin() as conn:
                _ensure_tables(conn)
                for _, r in ps.iterrows():
                    if pd.isna(r[g_col]):
                        continue
                    conn.execute(text("""
                        INSERT INTO player_club_goals
                            (player, team, league, season, goals, matches, xg)
                        VALUES (:p, :t, :lg, :s, :g, :m, :x)
                        ON CONFLICT (player, team, season) DO UPDATE SET
                            goals = EXCLUDED.goals,
                            matches = EXCLUDED.matches,
                            xg = EXCLUDED.xg,
                            updated_at = NOW()
                    """), {
                        "p": str(r[p_col]).strip(), "t": str(r[pteam_col]).strip(),
                        "lg": sport_key, "s": season,
                        "g": int(r[g_col]),
                        "m": int(r[pm_col]) if pm_col and pd.notna(r[pm_col]) else None,
                        "x": float(r[xg_col]) if xg_col and pd.notna(r[xg_col]) else None,
                    })
                    players_up += 1

    result = {
        "status": "ok" if not errors else "partial",
        "team_rows": teams_up, "player_rows": players_up, "errors": errors[:3],
    }
    if verbose:
        print(f"   📊 Understat: {teams_up} equipos, {players_up} jugadores · "
              f"errores: {len(errors)}")
    return result


def refresh_club_elo(verbose: bool = True) -> int:
    """Ratings Club Elo de todos los clubes → tabla club_elo."""
    try:
        import soccerdata as sd
        elo = sd.ClubElo().read_by_date()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Club Elo falló: {type(e).__name__}: {str(e)[:100]}")
        return 0
    n = 0
    with engine.begin() as conn:
        _ensure_tables(conn)
        elo = elo.reset_index() if elo.index.name else elo
        club_col = "club" if "club" in elo.columns else elo.columns[0]
        elo_col = "elo" if "elo" in elo.columns else _pick_col(elo, "Elo", "elo")
        for _, r in elo.iterrows():
            conn.execute(text("""
                INSERT INTO club_elo (club, elo, rank, updated_at)
                VALUES (:c, :e, NULL, NOW())
                ON CONFLICT (club) DO UPDATE SET
                    elo = EXCLUDED.elo, updated_at = NOW()
            """), {"c": str(r[club_col]).strip(), "e": float(r[elo_col])})
            n += 1
    if verbose:
        print(f"   🏆 Club Elo actualizado: {n} clubes")
    return n


def get_real_xg(team: str) -> dict | None:
    """
    xG real del equipo desde team_xg_real (fuente: Understat).
    None si no hay dato fresco (<= STALE_DAYS).
    """
    team = team.strip().lower()
    try:
        df = pd.read_sql(text("""
            SELECT xg_for, xg_against, matches, updated_at
            FROM team_xg_real
            WHERE LOWER(team) = LOWER(:team)
            ORDER BY season DESC LIMIT 1
        """), engine, params={"team": team})
    except Exception:
        return None
    if df.empty:
        return None
    row = df.iloc[0]
    updated = row["updated_at"]
    if pd.notna(updated) and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(updated)
                               > pd.Timedelta(days=STALE_DAYS)):
        return None
    if pd.isna(row["xg_for"]) or pd.isna(row["xg_against"]):
        return None
    return {
        "xg_for": float(row["xg_for"]),
        "xg_against": float(row["xg_against"]),
        "matches": int(row["matches"]) if pd.notna(row["matches"]) else None,
        "source": "understat",
    }


if __name__ == "__main__":
    print("Módulo de integración soccerdata — se ejecuta vía orchestrator weekly")
