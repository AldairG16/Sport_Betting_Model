"""
src/models/big_names.py
=======================
Grandes de nombre que vienen mal (13-nov-26).

El dueño: "quiero implementar la lista negra, en donde hay partidos en donde un
equipo es muy superior por nombre pero en realidad viene en decadencia, como es
el caso del Chelsea, Juventus, ManU… ¿de qué manera podremos utilizar esto como
una ventaja?". Se midió antes de armarla (137 mil partidos, 2012-26; muy
favoritos de 65%+ en ligas con pago anticipado, al precio promedio):

    grande que viene bien (20+ puntos de 30)  4,500  gana 80 (Pinnacle 76)  +1.4 por cada 100
    grande normal (16 a 19)                   1,255  gana 77 (74)           −0.1
    grande que viene mal (15 o menos)           577  gana 73 (72)           −2.2
    sin gran nombre                           1,467  gana 71 (71)           −3.0

La ventaja del 🎯 vive en el grande que viene bien (+1.5, +1.3 y +1.3 en las
tres épocas). El que viene mal no la tiene, pero eso salió de 2012-23: en
2023-26 ganó 78 contra 72 (110 partidos) y la diferencia con el que viene bien
todavía puede ser azar (z −1.6). Y una lista POR NOMBRE habría señalado mal:
Chelsea ganó 20 de 26 viniendo mal y Juventus 12 de 15; fallaron Dortmund (11
de 23) y Liverpool (7 de 16). Apostar en contra pierde (empate −14 a −22).

El dueño eligió (2): **medir primero y avisar sin decidir**. Aquí:

- "Grande de nombre" es una regla, no una lista a mano: los TOP_N equipos con
  más probabilidad media de ganar según Pinnacle en su liga en las REP_SEASONS
  temporadas anteriores (historical_odds). Se recalcula cada semana.
- "Viene mal": POOR_POINTS puntos o menos en sus últimos FORM_GAMES partidos de
  liga (matches). Se recalcula cada día.
- El partido de un grande que viene mal lleva un aviso (program_picks.form_note)
  que se ve en el dashboard y en el pick final de Telegram. **El pick y el 🎯
  no cambian.**
- La medición vive en favourite_watch (los partidos nuevos) y en edge_finder
  (todo el historial, cada lunes). Si la evidencia llega, cambiar la regla es
  decisión del dueño.
"""

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

STATE_KEY = "big_names"
TOP_N = 4                      # los grandes de cada liga
REP_SEASONS = 2                # temporadas anteriores que hacen el nombre
REP_MIN_GAMES = 10             # partidos con precio de Pinnacle para que una temporada cuente
MIN_LEAGUE_TEAMS = 8           # con menos equipos con las dos temporadas no hay ranking que valga
FORM_GAMES = 10
POOR_POINTS = 15               # de 30: "viene mal"
GOOD_POINTS = 20               # de 30: "viene bien"
FORM_DAYS = 250                # cuánto hacia atrás se leen los partidos para esos 10
BIG_MAX_AGE_DAYS = 7           # el ranking de grandes se rehace cada semana
FORM_NOTE = ("{team} viene mal: {pts} puntos de sus últimos 30 en liga. Es de los equipos grandes de su liga, pero "
             "hoy no está ganando como grande. Es un aviso: el pick no cambia")

# La probabilidad de ganar de cada partido sale de Pinnacle y, si falta, del promedio de las casas: el
# historial (football-data) dejó de traer el precio de Pinnacle a principios de 2026 (0 partidos desde
# febrero; visto el 13-nov-26, cuando la primera corrida de esto devolvió cero grandes). Solo con
# Pinnacle, el ranking se quedaba vacío a partir de la temporada 2027.
REPUTATION_SQL = """
    WITH o AS (
        SELECT sport_key, season, home, away,
               CASE WHEN pin_home > 1 AND pin_draw > 1 AND pin_away > 1 THEN 1 / pin_home ELSE 1 / avg_home END AS ih,
               CASE WHEN pin_home > 1 AND pin_draw > 1 AND pin_away > 1 THEN 1 / pin_draw ELSE 1 / avg_draw END AS idr,
               CASE WHEN pin_home > 1 AND pin_draw > 1 AND pin_away > 1 THEN 1 / pin_away ELSE 1 / avg_away END AS ia
        FROM historical_odds
        WHERE (pin_home > 1 AND pin_draw > 1 AND pin_away > 1) OR (avg_home > 1 AND avg_draw > 1 AND avg_away > 1)),
    s AS (
        SELECT sport_key, season, home AS team, ih / (ih + idr + ia) AS p FROM o
        UNION ALL
        SELECT sport_key, season, away, ia / (ih + idr + ia) FROM o),
    last AS (SELECT sport_key, MAX(season) AS season FROM historical_odds GROUP BY 1),
    cur AS (
        SELECT h.sport_key, h.season, t.team
        FROM historical_odds h JOIN last l ON l.sport_key = h.sport_key AND h.season = l.season
        CROSS JOIN LATERAL (VALUES (h.home), (h.away)) AS t(team))
    SELECT s.sport_key, s.season, l.season AS last_season, s.team, AVG(s.p) AS p, COUNT(*) AS n
    FROM s JOIN last l ON l.sport_key = s.sport_key AND s.season BETWEEN l.season - :back AND l.season - 1
    GROUP BY 1, 2, 3, 4
    UNION ALL
    SELECT sport_key, season, season, team, NULL, COUNT(*) FROM cur GROUP BY 1, 2, 4
"""


def _norm(normalize):
    return (lambda t: normalize(str(t)).lower()) if normalize else (lambda t: str(t).lower())


def big_by_league(rows: list, normalize=None) -> dict:
    """Función pura. rows: [{sport_key, season, last_season, team, p, n}] con la
    probabilidad media de ganar de cada equipo por temporada y la temporada en
    curso de su liga (last_season: la última que hay en el historial, con precio
    o sin él). Devuelve {liga: {"season": temporada en curso, "teams": [los
    TOP_N grandes]}}: los que más ganaban según las casas en las REP_SEASONS
    temporadas ANTERIORES a la que está en curso (esa no cuenta: el nombre es lo
    de antes). Un equipo sin las dos temporadas en la liga (un ascendido) no
    entra al ranking, y tampoco el que ya no juega en ella: las filas de la
    temporada en curso (season == last_season) dicen quién sigue ahí."""
    norm = _norm(normalize)
    last, rep, present = {}, {}, {}
    for r in rows:
        lg, se = r["sport_key"], int(r["season"])
        last[lg] = int(r["last_season"])
        if se == last[lg]:
            present.setdefault(lg, set()).add(norm(r["team"]))
        elif 1 <= last[lg] - se <= REP_SEASONS and int(r["n"]) >= REP_MIN_GAMES:
            rep.setdefault(lg, {}).setdefault(norm(r["team"]), {})[se] = float(r["p"])
    out = {}
    for lg, teams in rep.items():
        here = present.get(lg)
        ranked = sorted(((sum(v.values()) / len(v), t) for t, v in teams.items()
                         if len(v) == REP_SEASONS and (not here or t in here)), reverse=True)
        if len(ranked) >= MIN_LEAGUE_TEAMS:
            out[lg] = {"season": last[lg], "teams": sorted(t for _, t in ranked[:TOP_N])}
    return out


def points_index(matches: list, normalize=None) -> dict:
    """{(liga, equipo): [(día, puntos del partido)]} en orden de fecha, con los
    partidos de liga ya jugados (matches: league, date, home, away y goles)."""
    norm = _norm(normalize)
    idx: dict = {}
    for r in matches:
        try:
            hg, ag = float(r["home_goals"]), float(r["away_goals"])
            day = pd.Timestamp(r["date"]).tz_localize(None).normalize()
        except (TypeError, ValueError):
            continue
        if hg != hg or ag != ag:                           # NaN: sin marcador
            continue
        idx.setdefault((r["league"], norm(r["home"])), []).append((day, 3 if hg > ag else 1 if hg == ag else 0))
        idx.setdefault((r["league"], norm(r["away"])), []).append((day, 3 if ag > hg else 1 if hg == ag else 0))
    for games in idx.values():
        games.sort(key=lambda g: g[0])
    return idx


def points_last(idx: dict, league, team, before=None, normalize=None) -> int | None:
    """Los puntos de un equipo en sus últimos FORM_GAMES partidos de liga
    ANTERIORES al día `before` (todos si es None). None con menos de FORM_GAMES."""
    games = idx.get((league, _norm(normalize)(team)), [])
    if before is not None:
        day = pd.Timestamp(before)
        day = (day.tz_convert("UTC").tz_localize(None) if day.tzinfo else day).normalize()
        games = [g for g in games if g[0] < day]
    last = games[-FORM_GAMES:]
    return int(sum(p for _, p in last)) if len(last) == FORM_GAMES else None


def group_of(is_big, points) -> str | None:
    """El grupo de un favorito para medirlo: "poor" (grande que viene mal),
    "normal", "good" (grande que viene bien) o "rest" (sin gran nombre). None si
    no se sabe si es grande o, siéndolo, cómo viene."""
    if is_big is None:
        return None
    if not is_big:
        return "rest"
    if points is None:
        return None
    return "poor" if points <= POOR_POINTS else "good" if points >= GOOD_POINTS else "normal"


def is_big(state: dict | None, league, team, normalize=None) -> bool | None:
    """¿Es de los grandes de su liga? None si de esa liga no hay ranking."""
    entry = ((state or {}).get("big") or {}).get(str(league or ""))
    if not entry:
        return None
    return _norm(normalize)(team) in set(entry.get("teams") or [])


def form_note(state: dict | None, league, home, away, names=None, normalize=None) -> str | None:
    """El aviso del partido: qué grande de nombre viene mal (o None). names:
    (local, visitante) como se ven. Solo informa: nadie decide con esto."""
    form = ((state or {}).get("form") or {}).get(str(league or "")) or {}
    norm = _norm(normalize)
    notes = []
    for team, shown in zip((home, away), names or (home, away)):
        pts = form.get(norm(team))
        if is_big(state, league, team, normalize) and pts is not None and pts <= POOR_POINTS:
            notes.append(FORM_NOTE.format(team=shown, pts=int(pts)))
    return " · ".join(notes) or None


def load_big_names() -> dict:
    from src.utils.model_state import load_state
    return load_state(STATE_KEY) or {}


def notes_for(engine=None, normalize=None):
    """fn(local, visitante, liga, nombres) → aviso, con el estado guardado (una
    lectura). Si no se puede leer, no hay aviso y se dice en el log: el pick
    sale igual."""
    import json
    if engine is None:
        from config.database import engine
    state = None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM model_state WHERE key = :k"), {"k": STATE_KEY}).first()
        if row and row[0] is not None:
            state = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception as e:
        log.warning(f"⚠️  Avisos de grandes que vienen mal: no se pudo leer su estado ({type(e).__name__})")
    return lambda home, away, league, names=None: form_note(state, league, home, away, names, normalize)


# ─────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────

def _read_matches(engine, since, leagues) -> list:
    leagues = sorted({str(x) for x in leagues if x})
    if not leagues:
        return []
    marks = ", ".join(f":l{i}" for i in range(len(leagues)))
    rows = pd.read_sql(text(f"""
        SELECT league, date, LOWER(home_team) AS home, LOWER(away_team) AS away, home_goals, away_goals
        FROM matches
        WHERE date >= :s AND league IN ({marks}) AND home_goals IS NOT NULL AND away_goals IS NOT NULL"""),
        engine, params={"s": pd.Timestamp(since).strftime("%Y-%m-%d"), **{f"l{i}": x for i, x in enumerate(leagues)}})
    return rows.to_dict("records")


def run_big_names(engine=None, now=None, verbose: bool = True) -> dict:
    """Paso diario: rehace el ranking de grandes si tiene más de BIG_MAX_AGE_DAYS
    (una consulta agregada a historical_odds) y, siempre, cómo viene cada grande
    (sus últimos 10 de liga en matches). Guarda en model_state."""
    if engine is None:
        from config.database import engine
    from src.utils.model_state import load_state, save_state
    from src.utils.team_normalizer import normalize_team
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    prev = load_state(STATE_KEY) or {}
    big, big_at = prev.get("big") or {}, prev.get("big_at")
    age = (now - pd.Timestamp(big_at)).days if big_at else None
    if not big or age is None or age >= BIG_MAX_AGE_DAYS:
        rep = pd.read_sql(text(REPUTATION_SQL), engine, params={"back": REP_SEASONS})
        big, big_at = big_by_league(rep.to_dict("records"), normalize_team), now.isoformat()
    idx = points_index(_read_matches(engine, now - pd.Timedelta(days=FORM_DAYS), big.keys()), normalize_team)
    followed = {lg for lg, _ in idx}                       # ligas con partidos en la base (las que sigue el programa)
    form, missing = {}, []
    for lg, entry in big.items():
        for team in entry["teams"]:
            pts = points_last(idx, lg, team)
            if pts is not None:
                form.setdefault(lg, {})[team] = pts
            elif lg in followed:                           # su liga sí está y él no: otro nombre, o le faltan partidos
                missing.append(f"{team} ({lg})")
    state = {"big": big, "form": form, "big_at": big_at, "missing": missing,
             "policy": {"top_n": TOP_N, "rep_seasons": REP_SEASONS, "form_games": FORM_GAMES,
                        "poor_points": POOR_POINTS, "good_points": GOOD_POINTS},
             "updated_at": now.isoformat()}
    save_state(STATE_KEY, state, history=False)
    if verbose:
        print(format_report(state))
    return state


def poor_teams(state: dict) -> list:
    """[(liga, equipo, puntos)] de los grandes que hoy vienen mal."""
    out = []
    for lg, teams in (state.get("form") or {}).items():
        out += [(lg, t, p) for t, p in teams.items() if p <= POOR_POINTS]
    return sorted(out, key=lambda x: (x[2], x[0], x[1]))


def format_report(state: dict) -> str:
    """Para el log: cuántos grandes hay, cuáles vienen mal y de cuáles no hay partidos."""
    n = sum(len(e["teams"]) for e in (state.get("big") or {}).values())
    poor = poor_teams(state)
    lines = [f"🏷️  Grandes de nombre: {n} equipos en {len(state.get('big') or {})} ligas "
             f"(los {TOP_N} que más ganaban según las casas en las {REP_SEASONS} temporadas anteriores)."]
    if poor:
        lines.append(f"   Vienen mal ({POOR_POINTS} puntos o menos de sus últimos 30 en liga): "
                     + ", ".join(f"{t} {p}" for _, t, p in poor))
    else:
        lines.append("   Hoy ninguno viene mal.")
    if state.get("missing"):
        lines.append(f"   ⚠️ Sin sus últimos {FORM_GAMES} partidos en la base (¿otro nombre?): "
                     + ", ".join(state["missing"][:12]))
    return "\n".join(lines)
