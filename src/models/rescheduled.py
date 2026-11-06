"""
src/models/rescheduled.py
=========================
Partidos reprogramados (27-oct-26).

El feed a veces cambia el horario de un partido: PSG vs Lyon pasó del 25 al
26 de octubre, América vs Pachuca de las 23:00 a la 01:00 y luego a la 01:05.
Cada tabla guarda por (partido, fecha), así que la fecha vieja se quedaba
con su pick, sus candidatas sombra y su análisis contra Pinnacle. La
liquidación busca el resultado a ±1 día y le daba el del partido real: el
mismo partido contaba dos o tres veces en los picks, la sombra y el sesgo
aprendido. (v2.4.3 solo lo escondía del dashboard.)

La fecha que vale es la de ESPN (live_scores) si la tiene: el feed de momios
llegó a traer América vs Pachuca dos veces en la misma foto, a la 01:00 y a
la 01:05, y se jugó a la 01:05. Si ESPN no lo tiene, la del feed de momios
de ahora si trae una sola (upcoming_matches, desde el 6-nov-26); si tampoco y
ya pasó la primera de las fechas, la de la fila escrita más recientemente en
cualquiera de las tablas (en empate, la más tardía). Antes de eso se espera.
Las filas del mismo partido en otra fecha a menos de WINDOW_DAYS días son de
un horario que no se jugó:
  - picks, sombra, propuestas y piernas del parlay → result 'stale' (como
    "sin datos": fuera del acierto, las unidades y el aprendizaje; en el
    parlay la pierna se anula y sigue con las demás);
  - model_vs_sharp → skipped_at (fuera del sesgo aprendido y del dashboard).
bets_history no se toca: dos apuestas al mismo partido a distinta hora son
dos apuestas, y el bankroll las contó así.

6-nov-26: lo mismo con el nombre de antes de un alias (alias_twins). Un
partido analizado antes y después de que se aprendiera el alias de un club
quedaba dos veces a la misma hora ("pumas unam" y "unam pumas"): se anula la
fila del nombre viejo.
"""

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

WINDOW_DAYS = 3        # más lejos ya es otro partido (la vuelta de una copa, la segunda vuelta)

# tabla: columna que dice cuándo se escribió la fila (se refresca al reescribirla)
WRITTEN = {"program_picks": "picked_at", "shadow_bets": "created_at",
           "model_proposals": "evaluated_at", "model_vs_sharp": "created_at", "program_parlays": "picked_at"}
VOID_SQL = {
    "program_picks": "UPDATE program_picks SET result = 'stale', profit = NULL, resolved_at = NOW() "
                     "WHERE match = :match AND match_date = :match_date AND result IS DISTINCT FROM 'stale'",
    "program_parlays": "UPDATE program_parlays SET result = 'stale', profit = NULL, resolved_at = NOW() "
                       "WHERE match = :match AND match_date = :match_date AND result IS DISTINCT FROM 'stale'",
    "shadow_bets": "UPDATE shadow_bets SET result = 'stale', profit = NULL, resolved_at = NOW() "
                   "WHERE match = :match AND match_date = :match_date AND result IS DISTINCT FROM 'stale'",
    "model_proposals": "UPDATE model_proposals SET result = 'stale', profit = NULL, resolved_at = NOW() "
                       "WHERE match = :match AND match_date = :match_date AND result IS DISTINCT FROM 'stale'",
    "model_vs_sharp": "UPDATE model_vs_sharp SET skipped_at = NOW() "
                      "WHERE match = :match AND match_date = :match_date AND skipped_at IS NULL",
}


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def phantom_dates(rows: list, espn: dict | None = None, window_days: int = WINDOW_DAYS,
                  feed: dict | None = None, now=None) -> set:
    """Función pura. rows: [{"match", "match_date", "written"}] de todas las
    tablas; espn: {match: [kickoff, ...]} de live_scores; feed: {match:
    [match_date, ...]} del feed de momios de ahora (upcoming_matches).
    Devuelve los (match, match_date) de un horario que ya no vale: el partido
    tiene otra fecha que vale, a window_days días o menos. Vale la de ESPN; si
    ESPN no lo tiene, la del feed si trae una sola; si tampoco y ya pasó la
    primera de las fechas, la escrita más recientemente. Antes de eso no se
    decide nada: anular no tiene vuelta.

    El feed va antes que lo escrito desde el 6-nov-26: el closing reescribe
    cada 30 min el pick de las dos fechas a la vez (mismo NOW()), así que antes
    del partido empataban y ganaba la más tardía. Un partido adelantado habría
    anulado su horario real y le habría dado su resultado al viejo. Un feed con
    dos horarios del mismo partido (América vs Pachuca a la 01:00 y a la 01:05
    en la misma foto) no decide."""
    now = _ts(now) if now is not None else pd.Timestamp.now(tz="UTC")
    window = pd.Timedelta(days=window_days)
    dates, latest = {}, {}
    for r in rows:
        d = _ts(r["match_date"])
        dates.setdefault(r["match"], set()).add(d)
        w = r.get("written")
        if w is None or pd.isna(w):
            continue
        key = (_ts(w), d)                                   # en empate, la fecha más tardía
        if r["match"] not in latest or key > latest[r["match"]]:
            latest[r["match"]] = key
    out = set()
    for match, ds in dates.items():
        if len(ds) < 2:
            continue
        kicks = [_ts(k) for k in (espn or {}).get(match, [])]
        near = [(abs(d - k), d) for d in ds for k in kicks if abs(d - k) <= window]
        listed = {f for f in (_ts(x) for x in (feed or {}).get(match, [])) if any(abs(d - f) <= window for d in ds)}
        if near:
            cur = min(near)[1]
        elif len(listed) == 1:
            f = next(iter(listed))
            cur = min((abs(d - f), d) for d in ds)[1]
        elif now < min(ds):
            continue                     # ningún horario empezó: espera a ESPN o a un feed sin dudas
        else:
            cur = latest.get(match, (None, None))[1]
        if cur is None:
            continue
        out |= {(match, d) for d in ds if d != cur and abs(d - cur) <= window}
    return out


def alias_twins(rows: list, normalize) -> set:
    """Función pura (6-nov-26): el mismo partido a la misma hora guardado con
    el nombre de antes de un alias y con el de ahora ("santos laguna vs pumas
    unam" y "santos laguna vs unam pumas", 22-oct; "sc paderborn" y
    "paderborn", 10-oct): contaba dos veces en los picks, la sombra y el sesgo
    aprendido. rows: [{"table", "match", "match_date"}]. Devuelve los (tabla,
    match, match_date) con el nombre viejo cuando la fila con el de ahora
    existe EN LA MISMA TABLA: si solo está el viejo (las propuestas de Santos
    vs Pumas), es la única y se queda."""
    def canon(m):
        h, sep, a = str(m).partition(" vs ")
        return f"{normalize(h)} vs {normalize(a)}" if sep else str(m)
    have = {(r.get("table"), r["match"], _ts(r["match_date"])) for r in rows}
    return {(t, m, d) for t, m, d in have if canon(m) != m and (t, canon(m), d) in have}


def void_rescheduled(engine=None, days: int | None = 30) -> dict:
    """Anula en cada tabla las filas de un horario que no se jugó y las del
    nombre de antes de un alias. days=None revisa toda la historia. Corre
    antes de liquidar (evening, resultados y weekly) y, desde el 6-nov-26,
    antes de guardar los picks en cada closing."""
    if engine is None:
        from config.database import engine
    since = "" if days is None else f"WHERE match_date >= NOW() - INTERVAL '{int(days)} days'"
    rows, present = [], []
    for table, col in WRITTEN.items():
        try:
            df = pd.read_sql(text(f"SELECT match, match_date, MAX({col}) AS written FROM {table} {since} "
                                  f"GROUP BY match, match_date"), engine)
        except Exception as e:
            if "does not exist" in str(e):          # instalación nueva: la tabla nace después
                continue
            raise
        present.append(table)
        rows += [{**r, "table": table} for r in df.to_dict("records")]
    espn = {}
    multi = {m for m, n in pd.Series([r["match"] for r in rows]).value_counts().items() if n > 1}
    if multi:
        try:
            ls = pd.read_sql(text("SELECT home, away, kickoff FROM live_scores"
                                  + (f" WHERE kickoff >= NOW() - INTERVAL '{int(days) + WINDOW_DAYS} days'"
                                     if days is not None else "")), engine)
            for r in ls.to_dict("records"):
                m = f"{r['home']} vs {r['away']}"
                if m in multi:
                    espn.setdefault(m, []).append(r["kickoff"])
        except Exception as e:
            if "does not exist" not in str(e):
                raise
    feed = {}
    if multi:
        try:
            up = pd.read_sql(text("""SELECT home_team_norm || ' vs ' || away_team_norm AS match, match_date
                                     FROM upcoming_matches"""), engine)
            for r in up.to_dict("records"):
                if r["match"] in multi:
                    feed.setdefault(r["match"], []).append(r["match_date"])
        except Exception as e:
            if "does not exist" not in str(e):
                raise
    from src.utils.team_normalizer import normalize_team
    phantoms = phantom_dates(rows, espn, feed=feed)
    twins = alias_twins(rows, normalize_team)
    # el horario viejo se anula en todas las tablas; el nombre viejo, solo donde está el nuevo
    todo = sorted({(t, m, d) for m, d in phantoms for t in present} | twins, key=lambda x: (x[1], x[2], x[0]))
    out = {t: 0 for t in present}
    new = set()                                     # los que tenían algo por anular en esta corrida
    if todo:
        with engine.begin() as conn:
            for table, m, d in todo:
                n = conn.execute(text(VOID_SQL[table]), {"match": m, "match_date": d.to_pydatetime()}).rowcount or 0
                out[table] += n
                if n:
                    new.add(m)
    names = sorted({m for m, _ in phantoms} | {m for _, m, _ in twins})
    # los de corridas anteriores se vuelven a ver 30 días; no son nuevos
    print(f"🗓️ Partidos reprogramados: {len(new)} nuevos"
          + (f" ({', '.join(sorted(new)[:3])}) · filas de la fecha vieja anuladas: "
             + ", ".join(f"{t} {n}" for t, n in out.items()) if new else "")
          + (f" · {len(names) - len(new)} ya anulados antes" if len(names) > len(new) else "")
          + (f" · {tn} con el nombre de antes de un alias" if (tn := len({m for _, m, _ in twins if m in new})) else ""))
    return {"matches": names, "new": sorted(new), "twins": sorted({m for _, m, _ in twins}), **out}
