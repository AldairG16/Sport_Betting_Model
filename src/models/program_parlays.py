"""
src/models/program_parlays.py
=============================
El parlay del día, guardado y medido (3-nov-26).

El dueño: "quiero agregar la función parley, donde seleccione los partidos más
seguros, de las mejores ligas, de los que más se ganan y los agrupe en una
pestaña". Lo que eligió:
  - mejores ligas = donde más ganan los picks del programa: unidades a favor
    desde el modelo corregido (22-oct) con LEAGUE_MIN_N picks o más. La lista
    se actualiza sola con cada pick liquidado;
  - de cada partido, la apuesta más segura aunque no sea el pick;
  - 2, 3 o 4 partidos, a elegir en la pestaña (se guardan las MAX_LEGS
    piernas más seguras; el parlay de k son las k primeras).

Segura = la probabilidad más baja entre el programa y el mercado sin margen:
los dos tienen que creerlo (MIN_SAFE). Entre los mercados con momio real de
PlayDoit que paguen MIN_LEG_ODDS o más: sin piso, la más segura siempre es un
más de 1.5 a −1400 que no suma nada al parlay (Galatasaray–Stuttgart del
3-nov pagaba 1.07).

Medido antes de hacerlo (3-nov-26, 12 días de picks del modelo corregido, con
los picks de cada partido en vez de la apuesta más segura, 3 piernas): en las
ligas que iban ganando habría salido parlay 7 días, 5 ganados (+8u); en las
ligas grandes, 9 días y 2 ganados (−1.8u). Muestra chica: no prueba nada. Los
partidos de 60%+ sueltos dejaron −3.4% y el parlay multiplica el margen de
cada pierna; por eso se guarda y se mide como los picks.

El parlay se rehace en cada snapshot (morning, evening, cada closing) hasta
que empieza su primera pierna; desde ahí queda fijo (es la apuesta que se
pudo hacer). No se reconstruyen días pasados: odds_history no guarda las
líneas 1.5/3.5 ni córners y tarjetas, y la regla saldría otra.

4-nov-26 (v2.7.0), un parlay por horario. El dueño: "en los parlays necesito
que se tomen en cuenta ya que se hagan las alineaciones… si son al mismo
horario sería mejor porque las alineaciones ya estarían para todos los picks
del parlay". Eligió: mismo horario = los partidos que empiezan hasta
BLOCK_MINUTES después del primero (1a); un parlay por cada horario, con su
récord (2a); aviso de Telegram cuando ya vio las alineaciones de todos (3a,
src/models/parlay_alerts.py). Antes era uno por día y quedaba fijo 5 min
antes de su primer partido: el del 6-nov juntaba 12:00, 13:00 y 21:00, y el
de las 21:00 se elegía 9 horas antes de su alineación. El closing guarda la
alineación de 80 a 5 min antes de cada partido y rehace el parlay con ella:
las de un horario ya están cuando se fija (program_parlays.slot = la hora de
su primer partido; un horario con menos de MIN_BLOCK_LEGS no es parlay).
Medido (28-oct a 7-nov, 9 días): 2+ partidos en un horario 7 días, 3+ en 3,
4 en 1; uno por día juntaba 4 casi siempre.
"""

import math
from datetime import timedelta

import pandas as pd
from sqlalchemy import text

from src.models import program_picks as PP
from src.utils.log import get_logger

log = get_logger(__name__)

MIN_SAFE = 0.55
MIN_LEG_ODDS = 1.25          # −400 en PlayDoit
MAX_LEGS = 4
LEAGUE_MIN_N = 10            # picks liquidados para juzgar una liga
FIXED_SINCE = "2026-10-22"   # el modelo corregido (= dashboard.board.MODEL_FIXED)
LOCK_MINUTES = 5             # la misma ventana de snapshot_picks: un partido a 5 min ya no se toca
BLOCK_MINUTES = 30           # mismo horario: hasta 30 min después del primero (el dueño, 4-nov-26)
MIN_BLOCK_LEGS = 2           # un horario con un solo partido no es parlay
MX_OFFSET = pd.Timedelta(hours=6)    # México, UTC-6 fijo
SETTLED = ("win", "loss", "push", "half_win", "half_loss")
LINEUP_OK = "confirmada"     # = program_picks.LINEUP_OK: la pierna se eligió con la alineación

PARLAYS_DDL = """
    CREATE TABLE IF NOT EXISTS program_parlays (
        id SERIAL PRIMARY KEY,
        day DATE NOT NULL, slot TIMESTAMPTZ, rank INT NOT NULL,
        match TEXT NOT NULL, match_date TIMESTAMPTZ NOT NULL, league TEXT, display TEXT,
        market TEXT NOT NULL, label TEXT, family TEXT,
        model_prob NUMERIC, market_prob NUMERIC, safety NUMERIC, playdoit_odds NUMERIC NOT NULL,
        lineup TEXT, picked_at TIMESTAMPTZ DEFAULT NOW(),
        result TEXT, profit NUMERIC, resolved_at TIMESTAMPTZ,
        UNIQUE (slot, rank)
    )
"""
# v2.6.0 → v2.7.0 (4-nov-26): de un parlay por día a uno por horario. Lo guardado
# toma como horario el primer partido de su día. Corre una vez (sin la columna).
MIGRATE_SQL = (
    "ALTER TABLE program_parlays ADD COLUMN IF NOT EXISTS slot TIMESTAMPTZ",
    """UPDATE program_parlays p SET slot = f.first
       FROM (SELECT day, MIN(match_date) AS first FROM program_parlays GROUP BY day) f
       WHERE p.slot IS NULL AND p.day = f.day""",
    "ALTER TABLE program_parlays DROP CONSTRAINT IF EXISTS program_parlays_day_rank_key",
    "ALTER TABLE program_parlays ADD CONSTRAINT program_parlays_slot_rank_key UNIQUE (slot, rank)",
)
_COLS = ("day", "slot", "rank", "match", "match_date", "league", "display", "market", "label", "family",
         "model_prob", "market_prob", "safety", "playdoit_odds", "lineup")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mx_day(t) -> pd.Timestamp:
    """El día de México (UTC-6) de una hora de partido."""
    return (PP._utc(t) - MX_OFFSET).tz_localize(None).normalize()


def safety(c: dict) -> float:
    return min(float(c["model"]), float(c["market_prob"]))


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
    """Las ligas donde los picks del programa van ganando: unidades a favor
    con LEAGUE_MIN_N picks liquidados o más, del modelo corregido y sin los de
    poca historia, de la que más gana a la que menos. before: solo los
    partidos anteriores a ese momento. picks: program_picks (league,
    match_date, result, profit, reason)."""
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


def safest_leg(r: dict, u, props=None, ratio=None, bias=None, weak=None) -> dict | None:
    """La apuesta más segura de un partido de model_vs_sharp con los momios de
    ahora (u): con momio real de PlayDoit de MIN_LEG_ODDS o más y MIN_SAFE o
    más para el programa y para el mercado. Fuera: los análisis que ya no
    valen, los de poca historia, los partidos entre ligas sin el ajuste por
    fuerza de liga (el programa ahí no sabe) y el ganador al que le faltan 4+
    titulares (weak, PP.lineup_check)."""
    from src.models.side_bias import is_skipped
    low = r.get("low_history")
    if is_skipped(r) or (low and not (isinstance(low, float) and pd.isna(low))):
        return None
    if PP.cross_league(r.get("league")) and _num(r.get("league_factor")) is None:
        return None
    weak = weak or {}
    ok = [c for c in (PP._candidates_of(r, u, props, ratio, bias) or [])
          if c["playdoit_odds"] and c["playdoit_odds"] >= MIN_LEG_ODDS and c["market"] not in weak
          and safety(c) >= MIN_SAFE]
    return max(ok, key=lambda c: (safety(c), c["playdoit_odds"])) if ok else None


def build_parlays(mvs: pd.DataFrame, upcoming: pd.DataFrame, leagues, ratio=None, normalize=None,
                  props_for=None, bias=None, lineups_for=None, exclude=None) -> dict:
    """Función pura: {horario (hora UTC de su primer partido): [piernas]} con
    las MAX_LEGS piernas más seguras de cada horario (blocks_of), una por
    partido, de los partidos de `leagues`; solo los horarios con
    MIN_BLOCK_LEGS o más. mvs: model_vs_sharp de los partidos que todavía no
    empiezan (la fila más reciente de cada uno cuenta); upcoming: sus momios
    de ahora; exclude: partidos (match en minúsculas) que ya están en un
    parlay fijo."""
    leagues = set(leagues)
    exclude = {str(m).lower() for m in (exclude or ())}
    up = {PP.match_key(r.home_team_norm, r.away_team_norm, normalize): r for r in upcoming.itertuples(index=False)}
    latest = {}
    order = mvs.sort_values("created_at") if "created_at" in mvs.columns else mvs
    for r in order.to_dict("records"):
        if r.get("league") not in leagues:
            continue
        h, _, a = str(r["match"]).partition(" vs ")
        latest[(PP.match_key(h, a, normalize), str(r["match_date"]))] = r
    found = []
    for (key, _), r in latest.items():
        u = up.get(key)
        if u is None or str(r["match"]).lower() in exclude:
            continue
        h, _, a = key.partition(" vs ")
        props = props_for(h, a, r.get("league"), r["match_date"]) if props_for else None
        weak, lineup = lineups_for(h, a, r["match_date"]) if lineups_for else ({}, None)
        leg = safest_leg(r, u, props, ratio, bias, weak)
        if not leg:
            continue
        found.append({
            "match": str(r["match"]).lower(), "match_date": PP._utc(r["match_date"]).to_pydatetime(),
            "league": r.get("league"), "display": f"{u.home_team} vs {u.away_team}",
            "market": leg["market"], "label": leg["label"], "family": leg["family"],
            "model_prob": round(float(leg["model"]), 3), "market_prob": round(float(leg["market_prob"]), 3),
            "safety": round(safety(leg), 3), "playdoit_odds": round(float(leg["playdoit_odds"]), 4),
            "lineup": lineup})
    out = {}
    for block in blocks_of(found):
        if len(block) < MIN_BLOCK_LEGS:
            continue
        legs = sorted(block, key=lambda x: (-x["safety"], -x["playdoit_odds"], x["match_date"]))[:MAX_LEGS]
        slot = min(PP._utc(x["match_date"]) for x in legs)        # se fija 5 min antes de su primer partido
        out[slot] = [{**x, "day": mx_day(slot).date(), "slot": slot.to_pydatetime(), "rank": i + 1}
                     for i, x in enumerate(legs)]
    return out


def _ensure(conn) -> None:
    conn.execute(text(PARLAYS_DDL))
    has_slot = conn.execute(text("""SELECT 1 FROM information_schema.columns
                                    WHERE table_name = 'program_parlays' AND column_name = 'slot'""")).first()
    if not has_slot:
        for sql in MIGRATE_SQL:
            conn.execute(text(sql))


def snapshot_parlays(hours: int = PP.SNAPSHOT_HOURS, engine=None) -> int:
    """Rehace el parlay de cada horario de las próximas `hours` horas con los
    momios y las alineaciones de ahora. El de un horario cuyo primer partido
    ya empezó (o empieza en LOCK_MINUTES) no se toca: quedó fijo, y sus
    partidos no entran a otro. Devuelve cuántas piernas guardó."""
    if engine is None:
        from config.database import engine
    from src.utils.team_normalizer import normalize_team
    try:
        now = pd.Timestamp.now(tz="UTC")
        lock = now + pd.Timedelta(minutes=LOCK_MINUTES)
        mvs = pd.read_sql(text("""
            SELECT * FROM model_vs_sharp
            WHERE match_date BETWEEN NOW() + make_interval(mins => :m) AND NOW() + make_interval(hours => :h)
        """), engine, params={"h": hours, "m": LOCK_MINUTES})
        picks = pd.read_sql(text("""SELECT league, match_date, result, profit, reason FROM program_picks
                                    WHERE match_date >= CAST(:s AS date)"""), engine, params={"s": FIXED_SINCE})
        leagues = [x["league"] for x in winning_leagues(picks, before=now)]
        with engine.begin() as conn:
            _ensure(conn)
        stored = pd.read_sql(text("""SELECT slot, match FROM program_parlays
                                     WHERE slot >= NOW() - INTERVAL '1 day'"""), engine)
        fixed = [r for r in stored.to_dict("records") if pd.notna(r.get("slot")) and PP._utc(r["slot"]) <= lock]
        locked = {PP._utc(r["slot"]) for r in fixed}
        built = {}
        if not mvs.empty and leagues:
            up = pd.read_sql(text(PP._UPCOMING_SQL), engine, params={"h": hours})
            built = build_parlays(mvs, up, leagues, PP._ratio(engine), normalize_team, PP._props_for(engine),
                                  PP.load_bias(engine), PP._lineups_for(engine, normalize_team),
                                  exclude={r["match"] for r in fixed})
        rows = [leg for slot in sorted(built) if slot > lock for leg in built[slot]]
        with engine.begin() as conn:
            # un parlay sin empezar es un borrador: se reemplazan todos
            conn.execute(text("DELETE FROM program_parlays WHERE result IS NULL AND slot > :lock"),
                         {"lock": lock.to_pydatetime()})
            if rows:
                values, params = [], {}
                for k, r in enumerate(rows):
                    values.append("(" + ", ".join(f":{c}{k}" for c in _COLS) + ")")
                    params.update({f"{c}{k}": r[c] for c in _COLS})
                conn.execute(text(f"""INSERT INTO program_parlays ({", ".join(_COLS)}) VALUES {", ".join(values)}
                                      ON CONFLICT (slot, rank) DO NOTHING"""), params)
    except Exception as e:
        log.error(f"❌ Parlays no guardados: {type(e).__name__}: {str(e)[:200]}")
        return 0
    slots = {r["slot"] for r in rows}
    ready = sum(1 for s in slots if all(str(r.get("lineup") or "").startswith(LINEUP_OK)
                                        for r in rows if r["slot"] == s))
    print(f"🧩 Parlays: {len(rows)} piernas en {len(slots)} horarios"
          + (f", {ready} ya con las alineaciones de todos" if ready else "")
          + (f" ({len(locked)} ya fijos, sin tocar)" if locked else "")
          + ("" if leagues else " · ninguna liga va ganando todavía"))
    return len(rows)


def resolve_parlays(engine=None) -> dict:
    """Liquida cada pierna ya jugada con la misma función que los picks
    (stake 1, al momio estimado de PlayDoit). El parlay sale de sus piernas
    (dashboard/parlay.py). Sin datos a los SHADOW_STALE_DAYS días: 'stale'."""
    if engine is None:
        from config.database import engine
    from src.models.save_bets import SHADOW_STALE_DAYS, _preload_matches, plan_shadow_resolutions
    with engine.begin() as conn:
        _ensure(conn)
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, playdoit_odds FROM program_parlays
        WHERE result IS NULL AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)
    if pending.empty:
        return {"pending": 0, "resolved": 0}
    plan = plan_shadow_resolutions(pending.assign(odds=pending["playdoit_odds"].astype(float)),
                                   _preload_matches(pending["match_date"]))
    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE program_parlays SET result = :result, profit = :profit, resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE program_parlays SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount
    print(f"🧩 Piernas del parlay liquidadas: {len(plan)}/{len(pending)}"
          + (f", {stale} sin datos → stale" if stale else ""))
    return {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}
