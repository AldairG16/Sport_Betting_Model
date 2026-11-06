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

# Respaldo (6-nov-26): Understat está caído desde el 2-nov ("We will come back
# soon") y el xG del 26-oct vencía el 16-nov. FotMob publica el xG de la
# temporada por equipo (a favor y en contra) y por jugador en JSON estático,
# sin bloqueo para scripts. Se usa solo para las ligas que Understat no dio.
FOTMOB_LEAGUES = {
    "soccer_epl": 47,
    "soccer_spain_la_liga": 87,
    "soccer_germany_bundesliga": 54,
    "soccer_italy_serie_a": 55,
    "soccer_france_ligue_one": 53,
}
FOTMOB_LEAGUE_URL = "https://www.fotmob.com/api/data/leagues?id={league}"
FOTMOB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


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


def parse_fotmob(xg_for: list, xg_against: list, goals: list | None = None,
                 player_xg: list | None = None) -> tuple[list[dict], list[dict]]:
    """
    Función pura. Las listas StatList de FotMob (expected_goals_team,
    expected_goals_conceded_team y, de jugadores, goals y expected_goals) →
    (equipos, jugadores) con las mismas columnas que parse_understat: el xG
    de la temporada (no por partido) y los partidos jugados.
    """
    against = {x.get("TeamId"): _num(x.get("StatValue")) for x in xg_against or []}
    teams = []
    for x in xg_for or []:
        name, n = str(x.get("ParticipantName") or "").strip(), _num(x.get("MatchesPlayed"), int)
        xf, xa = _num(x.get("StatValue")), against.get(x.get("TeamId"))
        if not name or not n or xf is None or xa is None:
            continue
        teams.append({"team": name, "xg_for": round(xf, 4), "xg_against": round(xa, 4), "matches": n})
    pxg = {x.get("ParticiantId"): _num(x.get("StatValue")) for x in player_xg or []}
    # el equipo del jugador con el nombre de la lista de equipos (por TeamId): en
    # la de jugadores FotMob usa el largo ("1. FC Union Berlin", "Bayer 04
    # Leverkusen") y no cuadraba con matches (system_audit, 6-nov-26)
    team_of = {x.get("TeamId"): str(x.get("ParticipantName") or "").strip() for x in xg_for or []}
    players = []
    for x in goals or []:
        name, g = str(x.get("ParticipantName") or "").strip(), _num(x.get("StatValue"), int)
        team = team_of.get(x.get("TeamId")) or str(x.get("TeamName") or "").strip()
        if not name or not team or not g:
            continue
        players.append({"player": name, "team": team, "goals": g,
                        "matches": _num(x.get("MatchesPlayed"), int), "xg": pxg.get(x.get("ParticiantId"))})
    return teams, players


def fetch_fotmob(league_id: int, season: str, get=None) -> tuple[list[dict], list[dict]]:
    """xG de la temporada `season` ('2026') de una liga de FotMob → (equipos,
    jugadores). Lanza si falla o si FotMob muestra otra temporada."""
    import requests
    get = get or (lambda url: requests.get(url, headers=FOTMOB_HEADERS, timeout=30))

    def _json(url):
        r = get(url)
        r.raise_for_status()
        return r.json()
    data = _json(FOTMOB_LEAGUE_URL.format(league=league_id))
    shown = str((data.get("details") or {}).get("selectedSeason") or "")
    if not shown.startswith(season):
        raise ValueError(f"FotMob muestra la temporada {shown or '?'}")
    stats = data.get("stats") or {}
    urls = {s.get("name"): s.get("fetchAllUrl") for s in (stats.get("teams") or []) + (stats.get("players") or [])}

    def stat(name, required=True):
        if not urls.get(name):
            if required:
                raise ValueError(f"FotMob sin {name}")
            return []
        return (_json(urls[name]).get("TopLists") or [{}])[0].get("StatList") or []
    return parse_fotmob(stat("expected_goals_team"), stat("expected_goals_conceded_team"),
                        stat("goals", required=False), stat("expected_goals", required=False))


def _save(sport_key: str, season: str, teams: list, players: list, source: str) -> tuple[int, int]:
    """Guarda una liga en team_xg_real y player_club_goals."""
    with engine.begin() as conn:
        _ensure_tables(conn)
        for t in teams:
            conn.execute(text("""
                INSERT INTO team_xg_real
                    (team, league, season, xg_for, xg_against, matches, source)
                VALUES (:team, :league, :season, :xg_for, :xg_against, :matches, :source)
                ON CONFLICT (team, league, season) DO UPDATE SET
                    xg_for = EXCLUDED.xg_for,
                    xg_against = EXCLUDED.xg_against,
                    matches = EXCLUDED.matches,
                    source = EXCLUDED.source,
                    updated_at = NOW()
            """), {**t, "league": sport_key, "season": season, "source": source})
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
    return len(teams), len(players)


def refresh_understat(seasons: list[str] | None = None, verbose: bool = True,
                      fotmob=None) -> dict:
    """
    xG por equipo y goles por jugador de las 5 grandes ligas → tablas
    team_xg_real y player_club_goals en Neon. Corre en el weekly.
    status: "ok" (todas las ligas), "partial" o "failed" (ninguna).
    Si la primera liga no conecta, el sitio no responde y no se piden las
    demás: van al mismo servidor (2-nov-26: cinco esperas de 30 s).
    Desde el 6-nov-26, la liga que Understat no dio se pide a FotMob (solo
    la temporada en curso); status cuenta las dos fuentes y "via_fotmob" dice
    cuáles salieron de ahí. fotmob(league_id, season): para las pruebas.
    """
    import requests
    seasons = seasons or [current_season()]
    wanted = [(sk, lg, s) for sk, lg in UNDERSTAT_LEAGUES.items() for s in seasons]
    teams_up, players_up, loaded = 0, 0, 0
    errors, missing = [], []
    down = False

    for sport_key, league, season in wanted:
        if down:
            missing.append((sport_key, season))
            continue
        try:
            teams, players = parse_understat(fetch_understat(league, season))
        except Exception as e:
            errors.append(f"{league}/{season}: {type(e).__name__}")
            missing.append((sport_key, season))
            if not loaded and isinstance(e, requests.exceptions.ConnectionError):
                errors.append("sin conexión: no se pidieron las demás ligas")
                down = True
            continue
        if not teams:
            errors.append(f"{league}/{season}: sin equipos")
            missing.append((sport_key, season))
            continue
        t, p = _save(sport_key, season, teams, players, "understat")
        teams_up, players_up, loaded = teams_up + t, players_up + p, loaded + 1

    via_fotmob = []
    fotmob = fotmob or fetch_fotmob
    for sport_key, season in missing:
        if sport_key not in FOTMOB_LEAGUES or season != current_season():
            continue
        try:
            teams, players = fotmob(FOTMOB_LEAGUES[sport_key], season)
        except Exception as e:
            errors.append(f"FotMob {sport_key}: {type(e).__name__}")
            if not via_fotmob and isinstance(e, (ConnectionError, requests.exceptions.ConnectionError,
                                                 requests.exceptions.Timeout)):
                break                                # tampoco responde: no se espera por cada liga
            continue
        if not teams:
            errors.append(f"FotMob {sport_key}: sin equipos")
            continue
        t, p = _save(sport_key, season, teams, players, "fotmob")
        teams_up, players_up, loaded = teams_up + t, players_up + p, loaded + 1
        via_fotmob.append(sport_key)

    status = "ok" if loaded == len(wanted) else ("failed" if not loaded else "partial")
    result = {"status": status, "team_rows": teams_up, "player_rows": players_up,
              "errors": errors[:5], "via_fotmob": via_fotmob}
    if verbose:
        print(f"   📊 Understat: {teams_up} equipos, {players_up} jugadores · "
              f"errores: {len(errors)}" + (f" ({'; '.join(errors[:3])})" if errors else "")
              + (f" · {len(via_fotmob)} liga(s) con el respaldo de FotMob" if via_fotmob else ""))
    if via_fotmob:
        # se ve en "Avisos de la corrida": la fuente principal sigue caída
        log.warning(f"⚠️  xG real de FotMob (respaldo) en {len(via_fotmob)} liga(s): Understat no respondió "
                    f"({'; '.join(errors[:2])})")
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
        df = pd.read_sql(text("SELECT team, season, xg_for, xg_against, matches, updated_at, source FROM team_xg_real"),
                         engine)
    except Exception:
        return None
    return real_xg_of(df, team, normalize_team)


def real_xg_of(df: pd.DataFrame, team: str, normalize_team, now=None) -> dict | None:
    """Función pura de get_real_xg sobre las filas de team_xg_real. Con dos
    fuentes el mismo equipo puede tener dos filas ("Deportivo La Coruna" de
    Understat y "Deportivo A Coruña" de FotMob): vale la más reciente de la
    temporada más nueva (6-nov-26; antes, la primera que saliera, y si era la
    vieja el equipo se quedaba sin xG aunque hubiera uno al día)."""
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    # La tabla guarda el nombre de Understat ("Bayern Munich", "VfB Stuttgart")
    # y el pipeline pregunta con el canónico ("bayern munchen", "stuttgart"):
    # se comparan los dos normalizados. Hasta el 23-oct-26 se comparaban tal
    # cual y 24 de los 96 equipos de las 5 grandes (Bayern, Atlético, Inter,
    # Leipzig, Newcastle…) caían sin aviso al xG estimado.
    df = df[df["team"].map(normalize_team) == team]
    if df.empty:
        return None
    df = df.assign(_at=pd.to_datetime(df["updated_at"], utc=True))
    row = df.sort_values(["season", "_at"], ascending=False, na_position="last").iloc[0]
    updated = row["_at"]
    if pd.notna(updated) and now - updated > pd.Timedelta(days=STALE_DAYS):
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
        "source": str(row.get("source") or "understat"),
    }


if __name__ == "__main__":
    print(refresh_if_stale(force=True))
