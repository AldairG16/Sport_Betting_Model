"""
src/features/espn_lineups.py
============================
Alineaciones de ESPN (API pública, gratis, sin key) → tabla `lineups`
(20-oct-26).

ESPN publica los 11 titulares, la formación y, al terminar, los goles de cada
jugador (`/summary?event=`). Para un partido por jugar aparecen ~1 h antes del
kickoff. Se usan para dos cosas:
  - historial (partidos terminados): quién es titular habitual y quién es el
    goleador de cada equipo (`backfill_lineups`, weekly);
  - partidos por empezar (`capture_upcoming`, closing cada 30 min): con quién
    sale cada equipo, para las alertas de src/models/lineup_alerts.py.

Medido el 20-oct en 4,389 partidos de 2024-26: el momio de CIERRE ya cuenta
las bajas (al local con 2+ titulares fuera la casa le dio 3 pt de más). Lo que
queda por medir es si una casa como PlayDoit tarda en ajustar después de que
sale la alineación.

Los nombres se guardan con normalize_team; en un partido por jugar, con los
nombres y la hora del sistema (mismo emparejamiento que Kalshi).
"""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/{what}"
HEADERS = {"User-Agent": "SportBettingModel/1.0"}
TABLE = "lineups"
DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id BIGSERIAL PRIMARY KEY,
        sport_key TEXT NOT NULL,
        kickoff TIMESTAMPTZ NOT NULL,
        team TEXT NOT NULL,
        opponent TEXT NOT NULL,
        is_home BOOLEAN NOT NULL,
        event_id TEXT,
        formation TEXT,
        starters JSONB NOT NULL,
        names JSONB,
        goals JSONB,
        completed BOOLEAN NOT NULL DEFAULT FALSE,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (sport_key, kickoff, team)
    )
"""


def espn_leagues() -> dict:
    """sport_key → slug de ESPN (una sola fuente: el backfill de resultados)."""
    from scripts.backfill_espn_results import ESPN_LEAGUES
    return dict(ESPN_LEAGUES)


def _get(slug: str, what: str, query: str, get=None):
    """JSON de ESPN o None. `get` inyectable para los tests."""
    if get is None:
        import requests
        get = requests.get
    try:
        r = get(BASE.format(slug=slug, what=what) + "?" + query, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.warning(f"⚠️  ESPN {slug} {what}: {type(e).__name__}: {str(e)[:120]}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def parse_scoreboard(data: dict | None) -> list:
    """Eventos del scoreboard: id, inicio, local, visitante, terminado."""
    out = []
    for e in (data or {}).get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c.get("team", {}).get("displayName") for c in comp.get("competitors", [])}
        if not teams.get("home") or not teams.get("away") or not e.get("date"):
            continue
        # el partido de estrellas (MLS vs Liga MX) no es de ningún equipo nuestro
        if any("all star" in str(teams[s]).lower() or "all-star" in str(teams[s]).lower() for s in ("home", "away")):
            continue
        out.append({"event": str(e.get("id")), "kick": pd.Timestamp(e["date"]).to_pydatetime(),
                    "home": teams["home"], "away": teams["away"],
                    "completed": bool(e.get("status", {}).get("type", {}).get("completed"))})
    return out


def parse_summary(data: dict | None) -> dict | None:
    """{"home"|"away": {"starters", "names", "goals", "formation"}} o None si
    todavía no hay 11 titulares de cada lado (alineación sin publicar)."""
    out = {}
    for r in (data or {}).get("rosters", []):
        side = r.get("homeAway")
        roster = r.get("roster") or []
        starters = [str(p["athlete"]["id"]) for p in roster if p.get("starter") and p.get("athlete")]
        if side not in ("home", "away") or len(starters) < 11:
            return None
        goals = {}
        for p in roster:
            g = next((s.get("value") for s in p.get("stats", []) if s.get("name") == "totalGoals"), 0)
            if p.get("athlete") and g:
                goals[str(p["athlete"]["id"])] = int(g)
        out[side] = {"starters": starters,
                     "names": {str(p["athlete"]["id"]): p["athlete"].get("displayName")
                               for p in roster if p.get("athlete")},
                     "goals": goals, "formation": r.get("formation")}
    return out if {"home", "away"} <= set(out) else None


def system_names(event: dict, candidates: list) -> tuple | None:
    """(local, visitante) con los nombres de `matches` del partido de ESPN, o
    None. candidates: [(fecha, local, visitante)] de la liga. Misma fecha ±1
    día y el mejor parecido de los dos nombres (como Kalshi); si dos partidos
    se parecen casi igual, no se adivina."""
    from src.features.kalshi import _name_score
    from src.utils.team_normalizer import normalize_team
    h, a = normalize_team(event["home"]), normalize_team(event["away"])
    day = pd.Timestamp(event["kick"]).date()
    scored = sorted(((_name_score(h, ch) + _name_score(a, ca), ch, ca) for d, ch, ca in candidates
                     if abs((pd.Timestamp(d).date() - day).days) <= 1), reverse=True)
    if not scored or scored[0][0] < 1.2 or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.3):
        return None
    return scored[0][1], scored[0][2]


def lineup_rows(sport_key: str, event: dict, summary: dict, home: str, away: str, kick) -> list:
    """Dos filas (una por equipo) para la tabla."""
    rows = []
    for side, team, opp in (("home", home, away), ("away", away, home)):
        s = summary[side]
        rows.append({"sport_key": sport_key, "kickoff": kick, "team": team, "opponent": opp,
                     "is_home": side == "home", "event_id": event["event"], "formation": s["formation"],
                     "starters": json.dumps(s["starters"]), "names": json.dumps(s["names"], ensure_ascii=False),
                     "goals": json.dumps(s["goals"]), "completed": bool(event["completed"])})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

def save_lineups(engine, rows: list) -> int:
    """Upsert: una alineación por partido y equipo; la del partido terminado
    (con goles) reemplaza a la previa."""
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(text(DDL))
        for r in rows:
            conn.execute(text(f"""
                INSERT INTO {TABLE} (sport_key, kickoff, team, opponent, is_home, event_id, formation,
                                     starters, names, goals, completed)
                VALUES (:sport_key, :kickoff, :team, :opponent, :is_home, :event_id, :formation,
                        CAST(:starters AS jsonb), CAST(:names AS jsonb), CAST(:goals AS jsonb), :completed)
                ON CONFLICT (sport_key, kickoff, team) DO UPDATE SET
                    starters = EXCLUDED.starters, names = EXCLUDED.names, goals = EXCLUDED.goals,
                    formation = EXCLUDED.formation, completed = EXCLUDED.completed,
                    event_id = EXCLUDED.event_id, captured_at = NOW()
                WHERE NOT {TABLE}.completed
            """), r)
    return len(rows)


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def backfill_lineups(days: int = 10, leagues: dict | None = None, engine=None, get=None,
                     verbose: bool = True) -> int:
    """Partidos TERMINADOS de los últimos `days` días: titulares y goles, para
    saber quién es titular habitual y quién es el goleador. Se salta los que
    ya están guardados como terminados."""
    from src.utils.team_normalizer import normalize_team
    if engine is None:
        from config.database import engine
    leagues = leagues or espn_leagues()
    today = datetime.now(timezone.utc).date()
    with engine.begin() as conn:
        conn.execute(text(DDL))
        have = {r[0] for r in conn.execute(text(
            f"SELECT DISTINCT event_id FROM {TABLE} WHERE completed AND event_id IS NOT NULL"))}
    months = sorted({d.strftime("%Y%m") for d in _days(today - timedelta(days=days), today)})
    saved, unnamed = 0, 0
    for sport_key, slug in leagues.items():
        # nombres del sistema: los de `matches` (canónicos), para que el
        # historial de un equipo se encuentre con el nombre de upcoming_matches
        with engine.connect() as conn:
            cands = [(r[0], r[1], r[2]) for r in conn.execute(text("""
                SELECT date, LOWER(home_team), LOWER(away_team) FROM matches
                WHERE league = :lg AND date >= CURRENT_DATE - :d
            """), {"lg": sport_key, "d": days + 2}).fetchall()]
        rows = []
        for ym in months:
            for ev in parse_scoreboard(_get(slug, "scoreboard", f"dates={ym}", get)):
                if (not ev["completed"] or ev["event"] in have
                        or ev["kick"].date() < today - timedelta(days=days)):
                    continue
                names = system_names(ev, cands)
                if names is None:
                    unnamed += 1
                    names = (normalize_team(ev["home"]), normalize_team(ev["away"]))
                summ = parse_summary(_get(slug, "summary", f"event={ev['event']}", get))
                if summ:
                    rows += lineup_rows(sport_key, ev, summ, names[0], names[1], ev["kick"])
        saved += save_lineups(engine, rows)
    if verbose:
        print(f"📋 Alineaciones (historial ESPN): {saved // 2} partidos nuevos en {len(leagues)} ligas"
              + (f" ({unnamed} sin su partido en matches: nombre de ESPN)" if unnamed else ""))
    return saved


def capture_upcoming(window_min: tuple = (5, 80), engine=None, get=None) -> list:
    """Alineaciones publicadas de los partidos del sistema que arrancan en la
    ventana (minutos). Devuelve las filas guardadas."""
    from src.features.kalshi import match_fixture
    from src.utils.team_normalizer import normalize_team
    if engine is None:
        from config.database import engine
    leagues = espn_leagues()
    now = datetime.now(timezone.utc)
    lo, hi = now + timedelta(minutes=window_min[0]), now + timedelta(minutes=window_min[1])
    with engine.connect() as conn:
        fixtures = [(r[0], r[1], r[2], r[3]) for r in conn.execute(text("""
            SELECT DISTINCT sport_key, home_team_norm, away_team_norm, match_date
            FROM upcoming_matches WHERE match_date BETWEEN :lo AND :hi
        """), {"lo": lo, "hi": hi}).fetchall() if r[0] in leagues]
    rows = []
    for sport_key in sorted({f[0] for f in fixtures}):
        slug = leagues[sport_key]
        events = []
        for day in {lo.date(), hi.date(), (lo - timedelta(days=1)).date()}:   # ESPN usa su propia fecha
            events += parse_scoreboard(_get(slug, "scoreboard", f"dates={day:%Y%m%d}", get))
        seen = set()
        for ev in events:
            if ev["event"] in seen or not (lo - timedelta(minutes=90) <= ev["kick"] <= hi + timedelta(minutes=90)):
                continue
            seen.add(ev["event"])
            m = match_fixture({"league": sport_key, "home": normalize_team(ev["home"]),
                               "away": normalize_team(ev["away"]), "kick": ev["kick"]}, fixtures)
            if not m:
                continue
            summ = parse_summary(_get(slug, "summary", f"event={ev['event']}", get))
            if summ:
                rows += lineup_rows(sport_key, ev, summ, m[0], m[1], m[2])
    save_lineups(engine, rows)
    return rows
