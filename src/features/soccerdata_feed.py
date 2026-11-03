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
  - La descarga corre en el weekly y guarda en tablas de Neon; si el weekly
    no pudo, el morning reintenta cada día (refresh_if_stale).
  - El pipeline jamás descarga: lee las tablas (lectura barata).
  - Si la fuente falla o el dato está viejo (>21 días), las lecturas
    devuelven None y el pipeline usa sus proxies — y el weekly lo AVISA.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.utils.log import get_logger

log = get_logger(__name__)

STALE_DAYS = 21
WARN_DAYS_LEFT = 7          # avisa (ERROR) una semana antes de que el xG venza
RETRY_AFTER_DAYS = 6        # el morning reintenta si el último xG tiene más que esto

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
    Si la primera liga no conecta, el sitio no responde y no se piden las
    demás: van al mismo servidor (2-nov-26: cinco esperas de 30 s).
    """
    import requests
    seasons = seasons or [current_season()]
    wanted = [(sk, lg, s) for sk, lg in UNDERSTAT_LEAGUES.items() for s in seasons]
    teams_up, players_up, loaded = 0, 0, 0
    errors = []

    for sport_key, league, season in wanted:
        try:
            teams, players = parse_understat(fetch_understat(league, season))
        except Exception as e:
            errors.append(f"{league}/{season}: {type(e).__name__}")
            if not loaded and isinstance(e, requests.exceptions.ConnectionError):
                errors.append("sin conexión: no se pidieron las demás ligas")
                break
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
        loaded += 1

    status = "ok" if loaded == len(wanted) else ("failed" if not loaded else "partial")
    result = {"status": status, "team_rows": teams_up, "player_rows": players_up,
              "errors": errors[:5]}
    if verbose:
        print(f"   📊 Understat: {teams_up} equipos, {players_up} jugadores · "
              f"errores: {len(errors)}" + (f" ({'; '.join(errors[:3])})" if errors else ""))
    return result


def last_refresh() -> datetime | None:
    """Cuándo se guardó el último xG de Understat (UTC), o None."""
    try:
        t = pd.read_sql(text("SELECT MAX(updated_at) AS t FROM team_xg_real"), engine).iloc[0]["t"]
    except Exception:
        return None                                 # la tabla nace con la primera carga
    if t is None or pd.isna(t):
        return None
    t = pd.Timestamp(t)
    return (t if t.tzinfo else t.tz_localize("UTC")).to_pydatetime()


def is_expired(now: datetime | None = None) -> bool:
    """¿Ya no sirve el último xG de Understat (más de STALE_DAYS días, o no
    hay)? Entonces get_real_xg devuelve None para todos y el modelo usa el xG
    estimado con tiros: lo avisan este módulo (report_stale) y el chequeo de
    datos (output_checks.FEED_MAX_AGE_DAYS), no la cobertura de la corrida."""
    last = last_refresh()
    return last is None or (now or datetime.now(timezone.utc)) - last > timedelta(days=STALE_DAYS)


def report_stale(res: dict, last: datetime | None, now: datetime) -> None:
    """Sin xG nuevo: WARNING mientras el último sirve todavía más de
    WARN_DAYS_LEFT días; si vence antes o no hay, ERROR (llega a Telegram).
    Hasta el 2-nov-26 el weekly decía siempre "el modelo usa el xG sustituto
    esta semana", y no era cierto: el del lunes anterior sirve STALE_DAYS días."""
    why = "; ".join(res.get("errors") or []) or "sin datos"
    if res.get("status") == "partial":
        log.warning(f"xG real incompleto (Understat): {why}")
        return
    if last is not None:
        expires = last + timedelta(days=STALE_DAYS)
        left = (expires - now).days
        if left > WARN_DAYS_LEFT:
            log.warning(f"⚠️  xG real sin actualizar (Understat: {why}) — sigue el del {last:%d-%b}, "
                        f"que sirve hasta el {expires:%d-%b}; el morning reintenta cada día")
            return
        if left >= 0:
            log.error(f"❌ xG real sin actualizar (Understat: {why}) — el del {last:%d-%b} vence el "
                      f"{expires:%d-%b}: desde ahí el modelo usa el xG sustituto")
            return
    log.error(f"❌ xG real NO disponible (Understat: {why}): el modelo usa el xG sustituto")


def refresh_if_stale(now: datetime | None = None, force: bool = False,
                     max_age_days: int = RETRY_AFTER_DAYS) -> dict | None:
    """El weekly lee Understat cada lunes (force). Si no pudo (2-nov-26: el
    sitio no respondía ni desde GitHub ni desde la PC), el morning reintenta
    cada día hasta lograrlo; antes la siguiente oportunidad era el lunes
    siguiente. None si el xG está al día."""
    now = now or datetime.now(timezone.utc)
    last = last_refresh()
    if not force and last is not None and now - last <= timedelta(days=max_age_days):
        print(f"   📊 xG real al día (Understat, {last:%d-%b})")
        return None
    res = refresh_understat(verbose=True)
    if res["status"] != "ok":
        report_stale(res, last, now)
    return res


# xG por partido más alto que esto es un dato roto, no un equipo.
MAX_XG_PER_MATCH = 4.0


def get_real_xg(team: str) -> dict | None:
    """
    xG real del equipo POR PARTIDO desde team_xg_real (fuente: Understat).
    None si no hay dato fresco (<= STALE_DAYS) o si no es creíble.

    La tabla guarda el total de la temporada (parse_understat suma el
    historial). Hasta el 21-oct-26 se devolvía ese total como si fuera por
    partido: desde el weekly del 28-sep, el Real Madrid tenía 23 xG "por
    partido", el pipeline lo mezclaba al 40% en el ataque y los goles
    esperados chocaban con el tope (3.5 y 3.5) en las 5 grandes ligas y en
    la Champions. El 1X2 del modelo salía ~40/17/43 en todos esos partidos.
    """
    from src.utils.team_normalizer import normalize_team
    team = normalize_team(team)
    try:
        df = pd.read_sql(text("SELECT team, season, xg_for, xg_against, matches, updated_at FROM team_xg_real"),
                         engine)
    except Exception:
        return None
    # La tabla guarda el nombre de Understat ("Bayern Munich", "VfB Stuttgart")
    # y el pipeline pregunta con el canónico ("bayern munchen", "stuttgart"):
    # se comparan los dos normalizados. Hasta el 23-oct-26 se comparaban tal
    # cual y 24 de los 96 equipos de las 5 grandes (Bayern, Atlético, Inter,
    # Leipzig, Newcastle…) caían sin aviso al xG estimado.
    df = df[df["team"].map(normalize_team) == team]
    if df.empty:
        return None
    row = df.sort_values("season", ascending=False).iloc[0]
    updated = row["updated_at"]
    if pd.notna(updated) and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(updated)
                               > pd.Timedelta(days=STALE_DAYS)):
        return None
    if pd.isna(row["xg_for"]) or pd.isna(row["xg_against"]):
        return None
    n = int(row["matches"]) if pd.notna(row["matches"]) else 0
    if n <= 0:
        return None
    xg_for, xg_against = float(row["xg_for"]) / n, float(row["xg_against"]) / n
    if not (0 <= xg_for <= MAX_XG_PER_MATCH and 0 <= xg_against <= MAX_XG_PER_MATCH):
        return None
    return {
        "xg_for": round(xg_for, 4),
        "xg_against": round(xg_against, 4),
        "matches": n,
        "source": "understat",
    }


if __name__ == "__main__":
    print(refresh_if_stale(force=True))
