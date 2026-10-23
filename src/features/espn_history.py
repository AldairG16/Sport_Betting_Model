"""
src/features/espn_history.py
============================
Los partidos que no entran por las ligas (22-oct-26): copas, Champions,
Europa y Conference League, Libertadores, Sudamericana y amistosos, de ESPN.

El dueño, al abrir Stuttgart–Gladbach: "te estás saltando partidos, no son
los últimos 5". `matches` se llena con las ligas (football-data) y con los
resultados de The Odds API de las ligas que predecimos. La Europa League, la
Conference, las copas y los amistosos no llegaban nunca, y de la Champions
faltaban partidos. A Stuttgart le faltaban la DFB-Pokal y dos amistosos de
la fecha FIFA; a Gladbach, lo mismo.

Aquí, del scoreboard público de ESPN (sin llave, una petición por
competición y mes, con pausa): marcador, córners y tarjetas (roja = 1, como
PlayDoit) de cada partido terminado.

Van en su propia tabla, espn_matches: el modelo no la lee para la fuerza de
los equipos (un amistoso o un 4-0 de copa contra uno de cuarta no son su
liga). La usan el partido abierto del dashboard (los últimos 5 de verdad),
Equipos y los días de descanso (fixture_congestion, sin amistosos; medido
contra Pinnacle el 23-oct-26: neutro a levemente mejor).

Nombres: el id de equipo de ESPN se empareja con el nombre de `matches` con
los partidos de liga del mismo scoreboard (misma fecha ±1 día, mismo
marcador, el nombre que más se parece; tabla espn_teams). Así un "Barcelona
SC" de la Libertadores no se confunde con el Barcelona. Sin emparejar, el
nombre pasa por normalize_team.
"""

import time

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

# slug de ESPN → (nombre en el dashboard, tipo)
COMPETITIONS = {
    "uefa.champions": ("Champions League", "europa"),
    "uefa.europa": ("Europa League", "europa"),
    "uefa.europa.conf": ("Conference League", "europa"),
    "conmebol.libertadores": ("Libertadores", "copa"),
    "conmebol.sudamericana": ("Sudamericana", "copa"),
    "concacaf.champions": ("Concachampions", "copa"),
    "ger.dfb_pokal": ("DFB-Pokal", "copa"),
    "eng.fa": ("FA Cup", "copa"),
    "eng.league_cup": ("Carabao Cup", "copa"),
    "esp.copa_del_rey": ("Copa del Rey", "copa"),
    "ita.coppa_italia": ("Coppa Italia", "copa"),
    "fra.coupe_de_france": ("Copa de Francia", "copa"),
    "ned.cup": ("Copa de Holanda", "copa"),
    "por.taca.portugal": ("Taça de Portugal", "copa"),
    "sco.tennents": ("Copa de Escocia", "copa"),
    "sco.cis": ("Copa de la Liga (Escocia)", "copa"),
    "usa.open": ("US Open Cup", "copa"),
    "bra.copa_do_brazil": ("Copa do Brasil", "copa"),
    "arg.copa": ("Copa Argentina", "copa"),
    "mex.copa_mx": ("Copa MX", "copa"),
    "club.friendly": ("Amistoso", "amistoso"),
}
FRIENDLY = "amistoso"
MONTHS = 3                     # el mes en curso y los dos anteriores
KNOWN_TEAMS = 200              # con tantos equipos ya emparejados, las ligas solo del mes en curso

DDL = ("""CREATE TABLE IF NOT EXISTS espn_matches (
              espn_id TEXT PRIMARY KEY, kickoff TIMESTAMPTZ NOT NULL, slug TEXT, competition TEXT, kind TEXT,
              home TEXT, away TEXT, home_name TEXT, away_name TEXT, home_goals INT, away_goals INT,
              home_corners INT, away_corners INT, home_cards INT, away_cards INT,
              fetched_at TIMESTAMPTZ DEFAULT NOW())""",
       """CREATE TABLE IF NOT EXISTS espn_teams (
              espn_id TEXT PRIMARY KEY, team TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_events(data: dict | None, slug: str) -> list:
    """Partidos terminados de un scoreboard: ids, nombres, marcador, córners
    y tarjetas por equipo (amarilla o roja = 1 cada una)."""
    out = []
    for ev in (data or {}).get("events", []):
        c = (ev.get("competitions") or [{}])[0]
        if not c.get("status", {}).get("type", {}).get("completed"):
            continue
        side = {t.get("homeAway"): t for t in c.get("competitors", [])}
        if "home" not in side or "away" not in side:
            continue
        cards = {}
        details = c.get("details") or []          # los amistosos no traen tarjetas: sin dato, no 0
        for d in details:
            kind = str(d.get("type", {}).get("text", ""))
            tid = str(d.get("team", {}).get("id", ""))
            if "Yellow" in kind or "Red" in kind:
                cards[tid] = cards.get(tid, 0) + 1
        row = {"espn_id": str(ev.get("id")), "kickoff": ev.get("date"), "slug": slug}
        for s in ("home", "away"):
            t = side[s]
            stats = {x.get("name"): x.get("displayValue") for x in t.get("statistics", []) or []}
            tid = str(t.get("team", {}).get("id", ""))
            score = t.get("score")
            row.update({f"{s}_id": tid, f"{s}_name": t.get("team", {}).get("displayName", ""),
                        f"{s}_goals": _int(score.get("value") if isinstance(score, dict) else score),
                        f"{s}_corners": _int(stats.get("wonCorners")),
                        f"{s}_cards": cards.get(tid, 0) if details else None})
        if row["home_goals"] is None or row["away_goals"] is None:
            continue
        out.append(row)
    return out


def learn_team_ids(events: list, matches: list) -> dict:
    """{id de ESPN: nombre de `matches`} con los partidos de liga: misma fecha
    ±1 día, mismo marcador y el mejor parecido de los dos nombres. Si dos
    candidatos se parecen casi igual, no se adivina."""
    from src.features.kalshi import _name_score
    from src.utils.team_normalizer import normalize_team
    by_score = {}
    for m in matches:
        by_score.setdefault((int(m["home_goals"]), int(m["away_goals"])), []).append(m)
    out = {}
    for ev in events:
        day = pd.Timestamp(ev["kickoff"]).tz_convert("UTC").date() if pd.Timestamp(ev["kickoff"]).tzinfo \
            else pd.Timestamp(ev["kickoff"]).date()
        h, a = normalize_team(ev["home_name"]), normalize_team(ev["away_name"])
        scored = sorted(((_name_score(h, m["h"]) + _name_score(a, m["a"]), m["h"], m["a"])
                         for m in by_score.get((ev["home_goals"], ev["away_goals"]), [])
                         if abs((pd.Timestamp(m["date"]).date() - day).days) <= 1), reverse=True)
        if not scored or scored[0][0] < 1.2 or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.3):
            continue
        out[ev["home_id"]], out[ev["away_id"]] = scored[0][1], scored[0][2]
    return out


def resolve(rows: list, ids: dict) -> list:
    """Las filas de las competiciones extra con nuestros nombres de equipo."""
    from src.utils.team_normalizer import normalize_team
    out = []
    for r in rows:
        label, kind = COMPETITIONS[r["slug"]]
        out.append({**{k: r[k] for k in ("espn_id", "kickoff", "slug", "home_name", "away_name", "home_goals",
                                         "away_goals", "home_corners", "away_corners", "home_cards", "away_cards")},
                    "competition": label, "kind": kind,
                    "home": ids.get(r["home_id"]) or normalize_team(r["home_name"]),
                    "away": ids.get(r["away_id"]) or normalize_team(r["away_name"])})
    return out


def months_back(today=None, n: int = MONTHS) -> list:
    """['YYYYMM', …] del mes en curso hacia atrás."""
    t = pd.Timestamp(today or pd.Timestamp.now(tz="UTC")).replace(day=1)
    return [(t - pd.DateOffset(months=k)).strftime("%Y%m") for k in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# Red y base
# ─────────────────────────────────────────────────────────────────────────────

def _league_slugs() -> dict:
    from scripts.backfill_espn_results import ESPN_LEAGUES
    return {k: v for k, v in ESPN_LEAGUES.items() if v not in COMPETITIONS}


def refresh_espn_history(engine=None, months: int = MONTHS, get=None, pause: float = 0.3, today=None) -> int:
    """Guarda los partidos de las competiciones extra de los últimos `months`
    meses. Una competición que falla se salta; si no se pudo leer ninguna
    es ERROR (llega a Telegram)."""
    from src.features.espn_lineups import _get
    if engine is None:
        from config.database import engine
    yms = months_back(today, months)

    def board(slug, ym):
        data = _get(slug, "scoreboard", f"dates={ym}&limit=500", get)
        if pause:
            time.sleep(pause)
        return data

    with engine.begin() as conn:
        for d in DDL:
            conn.execute(text(d))
        known = dict(conn.execute(text("SELECT espn_id, team FROM espn_teams")).fetchall())
    # los ids ya emparejados se guardan: las ligas solo hacen falta para los
    # nuevos; y tras la primera carga, de las copas basta el mes en curso y el
    # anterior (un mes viejo ya no cambia)
    warm = len(known) >= KNOWN_TEAMS
    league_months = yms[:1] if warm else yms
    comp_months = yms[:2] if warm else yms
    league_events = []
    for slug in _league_slugs().values():
        for ym in league_months:
            league_events += parse_events(board(slug, ym), slug)
    extra, read = [], 0
    for slug in COMPETITIONS:
        for ym in comp_months:
            data = board(slug, ym)
            if data is not None:
                read += 1
            extra += parse_events(data, slug)
    if not read:
        log.error("❌ Historial ESPN: no se pudo leer ninguna competición — el dashboard se queda sin copas, "
                  "Europa League ni amistosos")
        return 0
    since = pd.Timestamp(league_months[-1] + "01") - pd.Timedelta(days=2)
    ms = pd.read_sql(text("""SELECT date, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals
                             FROM matches WHERE date >= :s AND home_goals IS NOT NULL"""),
                     engine, params={"s": since.to_pydatetime()})
    ids = {**known, **learn_team_ids(league_events, ms.to_dict("records"))}
    rows = resolve(extra, ids)
    new_ids = [{"i": k, "t": v} for k, v in ids.items() if known.get(k) != v]
    with engine.begin() as conn:
        if new_ids:
            conn.execute(text("""INSERT INTO espn_teams (espn_id, team) VALUES (:i, :t)
                                 ON CONFLICT (espn_id) DO UPDATE SET team = EXCLUDED.team, updated_at = NOW()"""),
                         new_ids)
        if rows:
            cols = ("espn_id", "kickoff", "slug", "competition", "kind", "home", "away", "home_name", "away_name",
                    "home_goals", "away_goals", "home_corners", "away_corners", "home_cards", "away_cards")
            conn.execute(text(f"""INSERT INTO espn_matches ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})
                                  ON CONFLICT (espn_id) DO UPDATE SET
                                  {", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])}, fetched_at = NOW()"""),
                         rows)
    print(f"📚 Historial ESPN: {len(rows)} partidos de copas, Europa y amistosos ({read} lecturas) · "
          f"{len(ids)} equipos emparejados")
    return len(rows)
