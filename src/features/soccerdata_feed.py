"""
src/features/soccerdata_feed.py
===============================
xG real de Understat (5 grandes ligas), leído directo de su endpoint de
datos. Ya no depende de la librería `soccerdata`.

QUÉ APORTA:
  - xG REAL por equipo → sustituye al proxy de tiros en el blend del
    pipeline cuando está disponible (get_real_xg).
  - Goles por jugador en CLUBES → alimenta el modelo de goleadores.

Por qué directo (25-sep-26): el weekly instalaba `soccerdata --no-deps` y
el `import` fallaba por una dependencia ausente (seleniumbase). El paso
imprimía "soccerdata no instalado — omitido" y reportaba ✅ OK: desde el
17-sep NUNCA hubo xG real y el modelo usó siempre el sustituto sin que
nada lo dijera. Understat publica sus datos por liga y temporada en
/getLeagueData/<liga>/<temporada> (JSON); basta requests.
Club Elo se retiró: se descargaba pero nada leía su tabla.

DISEÑO:
  - La descarga corre SOLO en el weekly y guarda en tablas de Neon.
  - El pipeline jamás descarga: lee las tablas (lectura barata).
  - Si la fuente falla o el dato está viejo (>21 días), las lecturas
    devuelven None y el pipeline usa sus proxies — y el weekly lo AVISA.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

STALE_DAYS = 21

# Mapeo sport_key (The Odds API) → liga en Understat
UNDERSTAT_LEAGUES = {
    "soccer_epl": "EPL",
    "soccer_spain_la_liga": "La_liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie_A",
    "soccer_france_ligue_one": "Ligue_1",
}
UNDERSTAT_URL = "https://understat.com/getLeagueData/{league}/{season}"
UNDERSTAT_HEADERS = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}


def current_season() -> str:
    """Temporada en curso por su año de inicio: 2026-27 → '2026'."""
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


def _num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def parse_understat(data: dict) -> tuple[list[dict], list[dict]]:
    """
    Función pura. JSON de /getLeagueData → (equipos, jugadores).
    equipos: {team, xg_for, xg_against, matches} sumando su historial de la
    temporada; jugadores: {player, team, goals, matches, xg} (solo con goles).
    """
    teams = []
    for t in (data.get("teams") or {}).values():
        hist = t.get("history") or []
        if not t.get("title") or not hist:
            continue
        teams.append({
            "team": str(t["title"]).strip(),
            "xg_for": round(sum(_num(h.get("xG")) or 0.0 for h in hist), 4),
            "xg_against": round(sum(_num(h.get("xGA")) or 0.0 for h in hist), 4),
            "matches": len(hist),
        })
    players = []
    for p in data.get("players") or []:
        goals = _num(p.get("goals"), int)
        if not p.get("player_name") or not p.get("team_title") or not goals:
            continue
        players.append({
            "player": str(p["player_name"]).strip(),
            "team": str(p["team_title"]).strip(),
            "goals": goals,
            "matches": _num(p.get("games"), int),
            "xg": _num(p.get("xG")),
        })
    return teams, players


def fetch_understat(league: str, season: str) -> dict:
    """Descarga una liga/temporada de Understat (lanza si falla)."""
    import requests
    r = requests.get(UNDERSTAT_URL.format(league=league, season=season),
                     headers={**UNDERSTAT_HEADERS,
                              "Referer": f"https://understat.com/league/{league}/{season}"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def refresh_understat(seasons: list[str] | None = None, verbose: bool = True) -> dict:
    """
    xG por equipo y goles por jugador de las 5 grandes ligas → tablas
    team_xg_real y player_club_goals en Neon. Corre en el weekly.
    status: "ok" (todas las ligas), "partial" o "failed" (ninguna).
    """
    seasons = seasons or [current_season()]
    teams_up, players_up = 0, 0
    errors = []

    for sport_key, league in UNDERSTAT_LEAGUES.items():
        for season in seasons:
            try:
                teams, players = parse_understat(fetch_understat(league, season))
            except Exception as e:
                errors.append(f"{league}/{season}: {type(e).__name__}")
                continue
            if not teams:
                errors.append(f"{league}/{season}: sin equipos")
                continue

            with engine.begin() as conn:
                _ensure_tables(conn)
                for t in teams:
                    conn.execute(text("""
                        INSERT INTO team_xg_real
                            (team, league, season, xg_for, xg_against, matches, source)
                        VALUES (:team, :league, :season, :xg_for, :xg_against, :matches, 'understat')
                        ON CONFLICT (team, league, season) DO UPDATE SET
                            xg_for = EXCLUDED.xg_for,
                            xg_against = EXCLUDED.xg_against,
                            matches = EXCLUDED.matches,
                            source = 'understat',
                            updated_at = NOW()
                    """), {**t, "league": sport_key, "season": season})
                    teams_up += 1
                for p in players:
                    conn.execute(text("""
                        INSERT INTO player_club_goals
                            (player, team, league, season, goals, matches, xg)
                        VALUES (:player, :team, :league, :season, :goals, :matches, :xg)
                        ON CONFLICT (player, team, season) DO UPDATE SET
                            goals = EXCLUDED.goals,
                            matches = EXCLUDED.matches,
                            xg = EXCLUDED.xg,
                            updated_at = NOW()
                    """), {**p, "league": sport_key, "season": season})
                    players_up += 1

    wanted = len(UNDERSTAT_LEAGUES) * len(seasons)
    status = "ok" if not errors else ("failed" if len(errors) >= wanted else "partial")
    result = {"status": status, "team_rows": teams_up, "player_rows": players_up,
              "errors": errors[:5]}
    if verbose:
        print(f"   📊 Understat: {teams_up} equipos, {players_up} jugadores · "
              f"errores: {len(errors)}" + (f" ({'; '.join(errors[:3])})" if errors else ""))
    return result


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
    print(refresh_understat())
