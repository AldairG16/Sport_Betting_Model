"""
src/models/program_parlays.py
=============================
El parlay del día, guardado y medido (3-nov-26).

El dueño: "quiero agregar la función parley, donde seleccione los partidos más
seguros, de las mejores ligas, de los que más se ganan y los agrupe en una
pestaña". v2.6.0 tomaba la apuesta más segura de cada partido de las ligas
donde los picks iban ganando; v2.7.0 (4-nov-26) hizo un parlay por horario
para que viera las alineaciones; v2.8.0 (5-nov-26) lo deja fijo también a la
hora de inicio de ESPN si es más temprana (PP.started_early).

El parlay se rehace en cada snapshot (morning, evening, cada closing) hasta
que empieza su primera pierna; desde ahí queda fijo (es la apuesta que se
pudo hacer). El closing guarda la alineación de 80 a 5 min antes de cada
partido y rehace el parlay con ella.

6-nov-26 (v2.9.0), el parlay con bono. La extensión de Claude revisó PlayDoit
el 5-nov (sin apostar): el boleto paga un bono sobre la GANANCIA del parlay,
3 selecciones 5%, 4 7.5%, 5 10%, 6 15%, 7 20%, 8 25%, 9 35% (la barra llega
a 40%), cada selección a −500 o más largo; y el Pago Anticipado (2 goles de
ventaja) también vale dentro del parlay (src/models/early_payout.py). El
favorito de 65%+ en Pinnacle sin margen sale ≈ 0% al precio promedio (el
sesgo del favorito, 2012-2026) y ≈ +1-2% con el pago anticipado: el bono
queda casi entero de ganancia. En el historial (Europa 1ª división, ligas
con pago anticipado, precio = promedio de las casas, semana por semana) el
de 4 dejó +17%, el de 5 +21% y el de 6 +33%, positivos en 2012-17, 2018-22
y 2023-26; si PlayDoit pagara 3% menos que el promedio, +4% a +10%. El parlay
anterior (la apuesta más segura de cada partido) esperaba ≈ −19% con 3
piernas y con 2 no tiene bono. El dueño eligió (1b, 2c, 4a):
  - las piernas: el ganador de 65%+ (Pinnacle sin margen) en una liga con
    pago anticipado, a −500 o más largo (PP.strong_favourite), sin 4+
    titulares fuera;
  - el parlay del día: los más fuertes del día, de 4 a 6 (DAY_MIN_LEGS,
    MAX_LEGS); si sobran 4 o más, otro con esos. Se fija 5 min antes de su
    primer partido: solo los de ese horario llevan alineación (con alineaciones
    para todos casi nunca se juntan 4: 0 horarios en 3 semanas);
  - además, el parlay de un horario: 3 o más que empiezan juntos (hasta
    BLOCK_MINUTES), con las alineaciones de todos (bono 5%: +10% en el
    historial). Repite partidos del del día; si son los mismos, es uno solo;
  - reemplaza al anterior: lo guardado sigue en la tabla (rule 'seguro').
Escocia no tiene pago anticipado y la Europa League no la baja el sistema:
no entran.
"""

import math
from datetime import timedelta

import pandas as pd
from sqlalchemy import text

from src.models import early_payout as EP
from src.models import program_picks as PP
from src.utils.log import get_logger

log = get_logger(__name__)

RULE = "bono"                 # v2.9.0; lo de antes queda como 'seguro'
STRONG_FAV = PP.STRONG_FAV
PRICE_BAND = PP.PRICE_BAND     # por debajo, la pierna exige que su momio llegue al mínimo (12-nov-26)
MIN_LEG_ODDS = PP.MIN_FAV_ODDS
# El bono del boleto de PlayDoit sobre la ganancia, por número de selecciones (5-nov-26)
BONUS = {3: 0.05, 4: 0.075, 5: 0.10, 6: 0.15, 7: 0.20, 8: 0.25, 9: 0.35}
MAX_LEGS = 6                  # el dueño: de 4 a 6
DAY_MIN_LEGS = 4
SLOT_MIN_LEGS = 3
MIN_BLOCK_LEGS = SLOT_MIN_LEGS
LEAGUES = EP.PA_LEAGUES
FIXED_SINCE = "2026-10-22"   # el modelo corregido (= dashboard.board.MODEL_FIXED)
LOCK_MINUTES = 5             # la misma ventana de snapshot_picks: un partido a 5 min ya no se toca
BLOCK_MINUTES = 30           # mismo horario: hasta 30 min después del primero (el dueño, 4-nov-26)
MX_OFFSET = pd.Timedelta(hours=6)    # México, UTC-6 fijo
SETTLED = ("win", "loss", "push", "half_win", "half_loss")
LINEUP_OK = "confirmada"     # = program_picks.LINEUP_OK: la pierna se eligió con la alineación
LEAGUE_MIN_N = 10            # (v2.6.0) picks liquidados para juzgar una liga en winning_leagues

PARLAYS_DDL = """
    CREATE TABLE IF NOT EXISTS program_parlays (
        id SERIAL PRIMARY KEY,
        day DATE NOT NULL, slot TIMESTAMPTZ, rank INT NOT NULL,
        match TEXT NOT NULL, match_date TIMESTAMPTZ NOT NULL, league TEXT, display TEXT,
        market TEXT NOT NULL, label TEXT, family TEXT,
        model_prob NUMERIC, market_prob NUMERIC, safety NUMERIC, playdoit_odds NUMERIC NOT NULL,
        lineup TEXT, picked_at TIMESTAMPTZ DEFAULT NOW(),
        result TEXT, profit NUMERIC, resolved_at TIMESTAMPTZ,
        rule TEXT, kind TEXT, seq INT, pa BOOLEAN, pa_paid BOOLEAN,
        CONSTRAINT program_parlays_parlay_rank_key UNIQUE (slot, rule, kind, seq, rank)
    )
"""
# v2.6.0 → v2.7.0 (4-nov-26): de un parlay por día a uno por horario. Corre una vez (sin la columna).
MIGRATE_SQL = (
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS slot TIMESTAMPTZ",
    """UPDATE program_parlays p SET slot = f.first
       FROM (SELECT day, MIN(match_date) AS first FROM program_parlays GROUP BY day) f
       WHERE p.slot IS NULL AND p.day = f.day""",
    "ALTER TABLE program_parlays DROP CONSTRAINT IF EXISTS program_parlays_day_rank_key",
    "ALTER TABLE program_parlays ADD CONSTRAINT program_parlays_slot_rank_key UNIQUE (slot, rank)",
)
# v2.7.0 → v2.9.0 (6-nov-26): varios parlays por horario (del día y del horario).
# Lo guardado queda como la regla anterior ('seguro'). Corre una vez (sin la columna rule).
MIGRATE_BONO_SQL = (
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS rule TEXT",
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS kind TEXT",
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS seq INT",
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS pa BOOLEAN",
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS pa_paid BOOLEAN",
    "UPDATE program_parlays SET rule = 'seguro', kind = 'horario', seq = 1 WHERE rule IS NULL",
    "ALTER TABLE program_parlays DROP CONSTRAINT IF EXISTS program_parlays_slot_rank_key",
    "ALTER TABLE program_parlays ADD CONSTRAINT program_parlays_parlay_rank_key UNIQUE (slot, rule, kind, seq, rank)",
)
_COLS = ("day", "slot", "rank", "match", "match_date", "league", "display", "market", "label", "family",
         "model_prob", "market_prob", "safety", "playdoit_odds", "lineup", "rule", "kind", "seq", "pa")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mx_day(t) -> pd.Timestamp:
    """El día de México (UTC-6) de una hora de partido."""
    return (PP._utc(t) - MX_OFFSET).tz_localize(None).normalize()


def bonus_for(n: int) -> float:
    """El bono de PlayDoit sobre la ganancia de un parlay de n selecciones."""
    if n < min(BONUS):
        return 0.0
    return BONUS.get(n, max(BONUS.values()))


def payout(odds: list) -> dict:
    """Momio del parlay (decimal) sin y con el bono, que se paga sobre la ganancia."""
    dec = math.prod(odds) if odds else None
    if not dec:
        return {"odds": None, "bonus": 0.0, "with_bonus": None}
    b = bonus_for(len(odds))
    return {"odds": dec, "bonus": b, "with_bonus": 1 + (dec - 1) * (1 + b)}


def lineup_ready(lineup, league, covered=None) -> bool:
    """¿La pierna ya se eligió con la alineación? Sí si la vio ("confirmada…")
    o si su liga no la tiene en ESPN (covered: las ligas que sí; ahí nunca
    llega y esperarla dejaría el parlay sin aviso)."""
    if isinstance(lineup, str) and lineup.startswith(LINEUP_OK):
        return True
    return covered is not None and league not in covered


def blocks_of(legs: list) -> list:
    """Los partidos que empiezan juntos: cada horario arranca con el primer
    partido que queda y junta los que empiezan hasta BLOCK_MINUTES después."""
    out, cur = [], []
    for x in sorted(legs, key=lambda x: PP._utc(x["match_date"])):
        if cur and PP._utc(x["match_date"]) - PP._utc(cur[0]["match_date"]) > timedelta(minutes=BLOCK_MINUTES):
            out.append(cur)
            cur = []
        cur.append(x)
    if cur:
        out.append(cur)
    return out


def winning_leagues(picks: pd.DataFrame, before=None) -> list:
    """(v2.6.0) Las ligas donde los picks del programa van ganando: unidades a
    favor con LEAGUE_MIN_N picks liquidados o más, del modelo corregido y sin
    los de poca historia. Desde v2.9.0 el parlay ya no las usa."""
    if picks is None or picks.empty:
        return []
    p = picks[picks["result"].isin(SETTLED) & picks["league"].notna()].copy()
    when = pd.to_datetime(p["match_date"], utc=True)
    keep = when >= pd.Timestamp(FIXED_SINCE, tz="UTC")
    if before is not None:
        keep &= when < PP._utc(before)
    p = p[keep & ~p["reason"].fillna("").astype(str).str.startswith(PP.LOW_HISTORY)]
    out = []
    for league, g in p.groupby("league"):
        units = round(float(pd.to_numeric(g["profit"], errors="coerce").fillna(0).sum()), 2)
        won = int(g["result"].isin(("win", "half_win")).sum())
        lost = int(g["result"].isin(("loss", "half_loss")).sum())
        if len(g) >= LEAGUE_MIN_N and units > 0:
            out.append({"league": league, "n": int(len(g)), "won": won, "lost": lost, "units": units})
    return sorted(out, key=lambda x: (-x["units"], -x["n"]))


def strong_leg(r: dict, u, ratio=None, bias=None, weak=None) -> dict | None:
    """El favorito fuerte de un partido de model_vs_sharp con los momios de
    ahora (u): 65%+ en Pinnacle sin margen, liga con pago anticipado, −500 o
    más largo (PP.strong_favourite) y sin 4+ titulares fuera (weak)."""
    if str(r.get("league") or "") not in LEAGUES:
        return None
    fav = PP.strong_favourite(PP._candidates_of(r, u, None, ratio, bias), r.get("league"))
    if fav is None or fav["market"] in (weak or {}):
        return None
    return fav


def _ranked(legs: list) -> list:
    return sorted(legs, key=lambda x: (-x["market_prob"], PP._utc(x["match_date"]), x["match"]))


def _parlay(kind: str, seq: int, day, legs: list) -> dict:
    slot = min(PP._utc(x["match_date"]) for x in legs)        # se fija 5 min antes de su primer partido
    return {"kind": kind, "seq": seq, "slot": slot,
            "legs": [{**x, "day": day, "slot": slot.to_pydatetime(), "rank": i + 1, "rule": RULE, "kind": kind,
                      "seq": seq} for i, x in enumerate(_ranked(legs))]}


def group_parlays(found: list, exclude: dict | None = None, fixed_seq: dict | None = None) -> list:
    """Función pura: los parlays de las piernas encontradas.
    - del día ('dia'): por día de México, los más fuertes, de DAY_MIN_LEGS a
      MAX_LEGS; si sobran DAY_MIN_LEGS o más, otro;
    - del horario ('horario'): SLOT_MIN_LEGS o más que empiezan juntos
      (blocks_of), hasta MAX_LEGS; si son los mismos partidos que un parlay
      del día, no se repite.
    exclude: {tipo: partidos ya en un parlay fijo de ese tipo}; fixed_seq:
    {día: cuántos parlays del día ya quedaron fijos} (para numerarlos).
    Cada parlay: {kind, seq, slot (hora UTC de su primer partido), legs con rank}."""
    exclude, fixed_seq = exclude or {}, fixed_seq or {}
    out, by_day = [], {}
    for x in found:
        by_day.setdefault(mx_day(x["match_date"]).date(), []).append(x)
    for day in sorted(by_day):
        legs = by_day[day]
        rest = _ranked([x for x in legs if x["match"] not in exclude.get("dia", set())])
        daily, seq = [], fixed_seq.get(day, 0)
        while len(rest) >= DAY_MIN_LEGS:
            seq += 1
            daily.append({x["match"] for x in rest[:MAX_LEGS]})
            out.append(_parlay("dia", seq, day, rest[:MAX_LEGS]))
            rest = rest[MAX_LEGS:]
        free = [x for x in legs if x["match"] not in exclude.get("horario", set())]
        for block in blocks_of(free):
            use = _ranked(block)[:MAX_LEGS]
            if len(use) < SLOT_MIN_LEGS or {x["match"] for x in use} in daily:
                continue
            out.append(_parlay("horario", 1, day, use))
    return out


def build_parlays(mvs: pd.DataFrame, upcoming: pd.DataFrame, ratio=None, normalize=None, bias=None,
                  lineups_for=None, exclude: dict | None = None, fixed_seq: dict | None = None) -> list:
    """Función pura: los parlays con bono (group_parlays) de los partidos de
    model_vs_sharp que todavía no empiezan (la fila más reciente de cada uno)
    con sus momios de ahora (upcoming)."""
    from src.models.side_bias import is_skipped
    up = {PP.match_key(r.home_team_norm, r.away_team_norm, normalize): r for r in upcoming.itertuples(index=False)}
    latest = {}
    order = mvs.sort_values("created_at") if "created_at" in mvs.columns else mvs
    for r in order.to_dict("records"):
        # un análisis que ya no vale (skipped_at: la última corrida no lo
        # evaluó, o es el horario viejo de un reprogramado) no es pierna: su
        # pick sale sin 🎯 (decide) y el mismo partido entraba dos veces
        if r.get("league") not in LEAGUES or is_skipped(r):
            continue
        h, _, a = str(r["match"]).partition(" vs ")
        latest[(PP.match_key(h, a, normalize), str(r["match_date"]))] = r
    found = []
    for (key, _), r in latest.items():
        u = up.get(key)
        if u is None:
            continue
        h, _, a = key.partition(" vs ")
        weak, lineup = lineups_for(h, a, r["match_date"]) if lineups_for else ({}, None)
        leg = strong_leg(r, u, ratio, bias, weak)
        if not leg:
            continue
        team = u.home_team if leg["market"] == "home_win" else u.away_team
        found.append({
            "match": str(r["match"]).lower(), "match_date": PP._utc(r["match_date"]).to_pydatetime(),
            "league": r.get("league"), "display": f"{u.home_team} vs {u.away_team}",
            "market": leg["market"], "label": f"Gana {team}", "family": "Ganador",
            "model_prob": round(float(leg["model"]), 3) if leg.get("model") is not None else None,
            "market_prob": round(float(leg["market_prob"]), 3), "safety": round(float(leg["market_prob"]), 3),
            "playdoit_odds": round(float(leg["playdoit_odds"]), 4), "lineup": lineup,
            "pa": EP.applies(r.get("league"), leg["market"])})
    return group_parlays(found, exclude, fixed_seq)


def _ensure(conn) -> None:
    conn.execute(text(PARLAYS_DDL))
    cols = {r[0] for r in conn.execute(text("""SELECT column_name FROM information_schema.columns
                                               WHERE table_name = 'program_parlays'""")).fetchall()}
    if "slot" not in cols:
        for sql in MIGRATE_SQL:
            conn.execute(text(sql))
    if "rule" not in cols:
        for sql in MIGRATE_BONO_SQL:
            conn.execute(text(sql))


def snapshot_parlays(hours: int = PP.SNAPSHOT_HOURS, engine=None) -> int:
    """Rehace los parlays con bono de las próximas `hours` horas con los momios
    y las alineaciones de ahora. Un parlay cuyo primer partido ya empezó (o
    empieza en LOCK_MINUTES) no se toca: quedó fijo, y sus partidos no entran
    a otro del mismo tipo. Lo mismo si ESPN da por empezado alguno de sus
    partidos antes de la hora del sistema (v2.8.0, PP.started_early).
    Devuelve cuántas piernas guardó."""
    if engine is None:
        from config.database import engine
    from src.utils.team_normalizer import normalize_team
    built, rows, locked = [], [], set()
    try:
        now = pd.Timestamp.now(tz="UTC")
        lock = now + pd.Timedelta(minutes=LOCK_MINUTES)
        mvs = pd.read_sql(text("""
            SELECT * FROM model_vs_sharp
            WHERE match_date BETWEEN NOW() + make_interval(mins => :m) AND NOW() + make_interval(hours => :h)
        """), engine, params={"h": hours, "m": LOCK_MINUTES})
        with engine.begin() as conn:
            _ensure(conn)
        stored = pd.read_sql(text("""SELECT slot, day, match, rule, kind, seq FROM program_parlays
                                     WHERE slot >= NOW() - INTERVAL '1 day'"""), engine)
        # v2.8.0: también queda fijo el horario con un partido que según ESPN
        # ya empezó aunque la hora del sistema diga que falta
        early = PP.started_early(engine, normalize_team, now)
        stored = stored.to_dict("records")
        started = {PP._utc(r["slot"]) for r in stored
                   if pd.notna(r.get("slot")) and PP._row_key(r, normalize_team) in early}
        fixed = [r for r in stored if pd.notna(r.get("slot")) and (PP._utc(r["slot"]) <= lock
                                                                   or PP._utc(r["slot"]) in started)]
        locked = {PP._utc(r["slot"]) for r in fixed}
        mine = [r for r in fixed if r.get("rule") == RULE]
        exclude = {k: {r["match"] for r in mine if r.get("kind") == k} for k in ("dia", "horario")}
        fixed_seq = {}
        for r in mine:
            if r.get("kind") == "dia":
                d = pd.Timestamp(r["day"]).date()
                fixed_seq[d] = max(fixed_seq.get(d, 0), int(r.get("seq") or 1))
        if not mvs.empty:
            up = pd.read_sql(text(PP._UPCOMING_SQL), engine, params={"h": hours})
            gone = {str(m).lower() for m in mvs["match"] if PP._row_key({"match": m}, normalize_team) in early}
            mvs = mvs[~mvs["match"].astype(str).str.lower().isin(gone)]
            built = build_parlays(mvs, up, PP._ratio(engine), normalize_team, PP.load_bias(engine),
                                  PP._lineups_for(engine, normalize_team), exclude, fixed_seq)
        built = [p for p in built if p["slot"] > lock and p["slot"] not in locked]
        rows = [leg for p in built for leg in p["legs"]]
        keep = sorted(s for s in locked if s > lock)
        with engine.begin() as conn:
            # un parlay sin empezar es un borrador: se reemplazan todos (menos los fijos por ESPN)
            conn.execute(text("DELETE FROM program_parlays WHERE result IS NULL AND slot > :lock"
                              + "".join(f" AND slot <> :keep{i}" for i in range(len(keep)))),
                         {"lock": lock.to_pydatetime(), **{f"keep{i}": s.to_pydatetime() for i, s in enumerate(keep)}})
            if rows:
                values, params = [], {}
                for k, r in enumerate(rows):
                    values.append("(" + ", ".join(f":{c}{k}" for c in _COLS) + ")")
                    params.update({f"{c}{k}": r[c] for c in _COLS})
                conn.execute(text(f"""INSERT INTO program_parlays ({", ".join(_COLS)}) VALUES {", ".join(values)}
                                      ON CONFLICT (slot, rule, kind, seq, rank) DO NOTHING"""), params)
    except Exception as e:
        log.error(f"❌ Parlays no guardados: {type(e).__name__}: {str(e)[:200]}")
        return 0
    days = sum(1 for p in built if p["kind"] == "dia")
    print(f"🧩 Parlays con bono: {days} del día y {len(built) - days} de un horario ({len(rows)} piernas)"
          + (f" · {len(locked)} horario(s) ya fijos, sin tocar" if locked else ""))
    return len(rows)


def resolve_parlays(engine=None) -> dict:
    """Liquida cada pierna ya jugada con la misma función que los picks
    (stake 1, al momio estimado de PlayDoit) y el Pago Anticipado (v2.9.0:
    el favorito que fue 2 goles arriba se cobra). El parlay y su bono salen
    de sus piernas (dashboard/parlay.py). Sin datos a los SHADOW_STALE_DAYS
    días: 'stale'."""
    if engine is None:
        from config.database import engine
    from src.models.save_bets import SHADOW_STALE_DAYS, _preload_matches, plan_shadow_resolutions
    with engine.begin() as conn:
        _ensure(conn)
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, league, playdoit_odds FROM program_parlays
        WHERE result IS NULL AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)
    if pending.empty:
        return {"pending": 0, "resolved": 0}
    priced = pending.assign(odds=pending["playdoit_odds"].astype(float))
    plan = plan_shadow_resolutions(priced, _preload_matches(pending["match_date"]))
    plan = PP._with_early_payout(engine, plan, priced)
    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE program_parlays SET result = :result, profit = :profit, pa_paid = :pa_paid,
                                           resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE program_parlays SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount
    print(f"🧩 Piernas del parlay liquidadas: {len(plan)}/{len(pending)}"
          + (f", {stale} sin datos → stale" if stale else ""))
    return {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}
