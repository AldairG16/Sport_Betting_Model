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
la 01:05, y se jugó a la 01:05. Si ESPN no lo tiene, la de la fila escrita
más recientemente en cualquiera de las tablas (el feed se vuelve a leer en
cada corrida y el closing reescribe el pick cada 30 min); en empate, la más
tardía. Las filas del mismo partido en otra fecha a menos de WINDOW_DAYS días
son de un horario que no se jugó:
  - picks, sombra y propuestas → result 'stale' (como "sin datos": fuera del
    acierto, las unidades y el aprendizaje);
  - model_vs_sharp → skipped_at (fuera del sesgo aprendido y del dashboard).
bets_history no se toca: dos apuestas al mismo partido a distinta hora son
dos apuestas, y el bankroll las contó así.
"""

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

WINDOW_DAYS = 3        # más lejos ya es otro partido (la vuelta de una copa, la segunda vuelta)

# tabla: columna que dice cuándo se escribió la fila (se refresca al reescribirla)
WRITTEN = {"program_picks": "picked_at", "shadow_bets": "created_at",
           "model_proposals": "evaluated_at", "model_vs_sharp": "created_at"}
VOID_SQL = {
    "program_picks": "UPDATE program_picks SET result = 'stale', profit = NULL, resolved_at = NOW() "
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


def phantom_dates(rows: list, espn: dict | None = None, window_days: int = WINDOW_DAYS) -> set:
    """Función pura. rows: [{"match", "match_date", "written"}] de todas las
    tablas; espn: {match: [kickoff, ...]} de live_scores. Devuelve los
    (match, match_date) de un horario que ya no vale: el partido tiene otra
    fecha que vale, a window_days días o menos."""
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
        cur = min(near)[1] if near else latest.get(match, (None, None))[1]
        if cur is None:
            continue
        out |= {(match, d) for d in ds if d != cur and abs(d - cur) <= window}
    return out


def void_rescheduled(engine=None, days: int | None = 30) -> dict:
    """Anula en cada tabla las filas de un horario que no se jugó. days=None
    revisa toda la historia. Corre antes de liquidar (evening y weekly)."""
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
        rows += df.to_dict("records")
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
    phantoms = phantom_dates(rows, espn)
    out = {t: 0 for t in present}
    new = set()                                     # los que tenían algo por anular en esta corrida
    if phantoms:
        with engine.begin() as conn:
            for m, d in sorted(phantoms):
                p = {"match": m, "match_date": d.to_pydatetime()}
                for table in present:
                    n = conn.execute(text(VOID_SQL[table]), p).rowcount or 0
                    out[table] += n
                    if n:
                        new.add(m)
    names = sorted({m for m, _ in phantoms})
    # los de corridas anteriores se vuelven a ver 30 días; no son nuevos
    print(f"🗓️ Partidos reprogramados: {len(new)} nuevos"
          + (f" ({', '.join(sorted(new)[:3])}) · filas de la fecha vieja anuladas: "
             + ", ".join(f"{t} {n}" for t, n in out.items()) if new else "")
          + (f" · {len(names) - len(new)} ya anulados antes" if len(names) > len(new) else ""))
    return {"matches": names, "new": sorted(new), **out}
