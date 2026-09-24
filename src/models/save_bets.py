import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.models.bankroll_manager import update_bankroll, ensure_bankroll_schema
from src.utils.log import get_logger

log = get_logger(__name__)


# ============================================================
# MARKET → REQUIRED MATCH FIELDS
# ============================================================
# Mapeo usado por el re-check de bets 'unresolved'. Sin esto el re-check
# sólo validaba que home_goals/away_goals existieran y re-marcaba a
# 'pending' incluso si los campos específicos del market seguían NULL —
# generando un loop pending↔unresolved que disparaba llamadas a Claude
# en cada cron de resolve_pending. Validar el campo correcto rompe el loop.

def _market_required_match_fields(market: str) -> tuple[str, ...]:
    m = (market or "").lower()
    if m.startswith("corners_"):
        return ("home_corners", "away_corners")
    if m.startswith("cards_"):
        return ("home_yellow", "away_yellow")
    if m.startswith("h1_"):
        return ("home_goals_ht", "away_goals_ht")
    if m.startswith("h2_"):
        return ("home_goals_h2", "away_goals_h2")
    if m.startswith("shots_"):
        return ("home_shots_target", "away_shots_target")
    # Mercados de goles (1X2, O/U, BTTS, AH, DC, DNB) → goles finales
    return ("home_goals", "away_goals")


# ============================================================
# COBERTURA DEL RESOLVER (ronda 4, D10)
# ============================================================
# El resolver liquidaba por default TODO mercado sin rama como "loss"
# silencioso, descontando bankroll. Un nombre paramétrico nuevo sin rama
# (ah_home_-1.2, corners_over_10.0...) era una pérdida inventada.
# is_resolvable_market() delimita el default a mercados con rama real.

_RESOLVER_FIXED_MARKETS = frozenset({
    "home_win", "draw", "away_win",
    "over25", "under25", "over_1.5", "under_1.5", "over_3.5", "under_3.5",
    "btts", "btts_no",
    "dc_1x", "dc_x2", "dc_12",
    "dnb_home", "dnb_away",
    "h1_home", "h1_draw", "h1_away", "h2_home", "h2_draw", "h2_away",
})

_RESOLVER_PARAMETRIC_PREFIXES = (
    "ah_home_", "ah_away_",
    "shots_over_", "shots_under_",
    "corners_over_", "corners_under_",
    "cards_over_", "cards_under_",
)


def is_resolvable_market(market) -> bool:
    """True si el resolver tiene rama para este mercado."""
    m = str(market or "").strip().lower()
    return m in _RESOLVER_FIXED_MARKETS or m.startswith(_RESOLVER_PARAMETRIC_PREFIXES)


# ============================================================
# SHADOW BETS (ronda 7, B2)
# ============================================================
# Candidatas con precio real que NO se apostaron (piso de edge, techo de
# desvío, banda de cuotas, max_odds). Se registran para medir CLV por banda
# de desvío sin arriesgar unidades: responde si la ventana apostable está
# en el lado correcto. El paso de closing las rellena igual que bets_history
# (ver update_closing_odds → _update_shadow_closing).

SHADOW_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS shadow_bets (
        id           SERIAL PRIMARY KEY,
        match        TEXT NOT NULL,
        match_date   TIMESTAMPTZ,
        league       TEXT,
        market       TEXT NOT NULL,
        p_final      NUMERIC,
        p_ref        NUMERIC,
        deviation    NUMERIC,
        odds         NUMERIC,
        edge_market  NUMERIC,
        reason       TEXT,
        closing_odds NUMERIC,
        closing_fetched_at TIMESTAMPTZ,
        p_pre_shade  NUMERIC,
        shade_delta  NUMERIC,
        result       TEXT,
        profit       NUMERIC,
        resolved_at  TIMESTAMPTZ,
        created_at   TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (match, market, match_date)
    )
"""

# Columnas agregadas el 22-sep-26 (las tablas creadas antes no las tienen):
#   closing_fetched_at  cuándo se descargó la cuota usada como cierre
#                       (ver closing_quality)
#   p_pre_shade         probabilidad anclada, ANTES de los ajustes manuales
#   shade_delta         cuánto la movieron esos ajustes (FLB, "la tabla
#                       miente", empates; ya acotado por D12, sin escalar)
#   result / profit     liquidación real con stake 1 (resolve_shadow_outcomes)
# Con resultado real se mide si cada regla acierta (src/models/rule_evidence.py).
SHADOW_ALTER_SQL = """
    ALTER TABLE shadow_bets
        ADD COLUMN IF NOT EXISTS closing_fetched_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS p_pre_shade NUMERIC,
        ADD COLUMN IF NOT EXISTS shade_delta NUMERIC,
        ADD COLUMN IF NOT EXISTS result TEXT,
        ADD COLUMN IF NOT EXISTS profit NUMERIC,
        ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ
"""
BETS_CLOSING_ALTER_SQL = "ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS closing_fetched_at TIMESTAMPTZ"


def persist_shadow_bets(records: list):
    """Inserta candidatas shadow (ON CONFLICT DO NOTHING por re-runs).
    Sanea NaN/NaT y aísla fallos por fila — una candidata corrupta no
    tumba el lote (lección de la primera corrida C1, ronda 8)."""
    if not records:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(SHADOW_TABLE_SQL))
            conn.execute(text(SHADOW_ALTER_SQL))
            inserted = 0
            for r in records:
                if not r.get("odds") or r["odds"] <= 1.01:
                    continue   # sin precio real no hay nada que medir
                clean = {}
                for k, v in r.items():
                    if v is None:
                        clean[k] = None
                    elif isinstance(v, pd.Timestamp):
                        clean[k] = None if pd.isna(v) else v.to_pydatetime()
                    else:
                        try:
                            if pd.isna(v):
                                clean[k] = None
                                continue
                        except (TypeError, ValueError):
                            pass
                        # escalares numpy (np.float64/np.int64) → nativos:
                        # psycopg2 no adapta numpy y aborta la transacción
                        if hasattr(v, "item") and not isinstance(v, (str, bytes)):
                            v = v.item()
                        clean[k] = v
                # mercados sin ancla no traen los campos de los shades
                clean.setdefault("p_pre_shade", None)
                clean.setdefault("shade_delta", None)
                try:
                    # savepoint por fila: un fallo no aborta el lote
                    with conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO shadow_bets
                                (match, match_date, league, market, p_final,
                                 p_ref, deviation, odds, edge_market, reason,
                                 p_pre_shade, shade_delta)
                            VALUES
                                (:match, :match_date, :league, :market, :p_final,
                                 :p_ref, :deviation, :odds, :edge_market, :reason,
                                 :p_pre_shade, :shade_delta)
                            ON CONFLICT (match, market, match_date) DO NOTHING
                        """), clean)
                    inserted += 1
                except Exception as row_err:
                    if inserted == 0 and not hasattr(persist_shadow_bets, "_dbg"):
                        persist_shadow_bets._dbg = True
                        log.warning(f"⚠️  shadow primera fila rechazada: {str(row_err)[:200]}")
                    log.warning(f"⚠️  shadow fila rechazada ({r.get('market')}): "
                          f"{type(row_err).__name__}")
        print(f"🌑 Shadow: {inserted}/{len(records)} candidatas registradas")
    except Exception as e:
        log.error(f"⚠️  persist_shadow_bets error: {e}")


# =========================
# SAVE BETS
# =========================

def save_bets(bets):

    if not bets:
        print("No bets to save")
        return

    df = pd.DataFrame(bets)

    df["result"] = "pending"
    df["profit"] = 0.0
    df["closing_odds"] = None
    df["clv"] = None

    df = df.where(pd.notnull(df), None)

    # Asegurar columnas nuevas existen
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE bets_history
            ADD COLUMN IF NOT EXISTS league TEXT
        """))
        # Decision log: snapshot JSON del contexto al momento de la apuesta
        # (lambdas, señales activas, versión de calibración, odds vistas).
        conn.execute(text("""
            ALTER TABLE bets_history
            ADD COLUMN IF NOT EXISTS decision_log JSONB
        """))
        # Slippage: odd a la que la bet fue COLOCADA realmente (vs `odds`,
        # la mejor odd vista al predecir). NULL mientras no se registre.
        conn.execute(text("""
            ALTER TABLE bets_history
            ADD COLUMN IF NOT EXISTS odds_placed NUMERIC
        """))
        conn.execute(text(BETS_CLOSING_ALTER_SQL))

    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(text("""
                INSERT INTO bets_history (
                    match,
                    match_date,
                    league,
                    market,
                    probability,
                    odds,
                    edge,
                    stake,
                    result,
                    profit,
                    closing_odds,
                    clv,
                    decision_log,
                    odds_placed
                )
                VALUES (
                    :match,
                    :match_date,
                    :league,
                    :market,
                    :probability,
                    :odds,
                    :edge,
                    :stake,
                    :result,
                    :profit,
                    :closing_odds,
                    :clv,
                    :decision_log,
                    :odds_placed
                )
                ON CONFLICT (match, market, match_date) DO NOTHING
            """), {
                **row.to_dict(),
                "odds_placed": row.get("odds_placed"),
            })

    print(f"✅ {len(df)} bets saved to bets_history")


def record_placed_odds(match: str, market: str, match_date, odds_placed: float) -> int:
    """
    Registra la odd REAL a la que se colocó una bet (slippage tracking).

    `odds` guarda la mejor odd vista al generar la predicción; la odd de
    ejecución suele ser peor (la línea se mueve). La diferencia
    (1/odds_placed − 1/odds) es el slippage: cuánto edge teórico muere
    en la ejecución.

    Returns: número de rows actualizadas (0 si la bet no existe).

    Compara por DÍA: match_date es un TIMESTAMP con hora de kickoff y se
    comparaba contra 'YYYY-MM-DD' (medianoche) — nunca coincidía (24-sep-26).
    El dashboard registra por id (POST /api/bets/<id>/placed).
    """
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE bets_history
            SET odds_placed = :odds_placed
            WHERE match = :match
              AND market = :market
              AND CAST(match_date AS date) = CAST(:match_date AS date)
        """), {
            "match": match, "market": market,
            "match_date": str(match_date)[:10], "odds_placed": odds_placed,
        })
        return result.rowcount


def slippage_report() -> dict:
    """
    Slippage medio: pérdida de edge entre la odd vista (predicción) y la
    odd colocada (ejecución). Solo opina sobre bets con odds_placed registrado.
    """
    df = pd.read_sql(text("""
        SELECT odds, odds_placed
        FROM bets_history
        WHERE odds_placed IS NOT NULL
          AND odds > 1 AND odds_placed > 1
    """), engine)
    if df.empty:
        return {"status": "no_data", "n": 0}
    slip = 1.0 / df["odds_placed"] - 1.0 / df["odds"]
    return {
        "status": "ok",
        "n": int(len(df)),
        "avg_slippage_prob": round(float(slip.mean()), 5),
        "avg_slippage_odds_pct": round(float((df["odds_placed"] / df["odds"] - 1).mean()) * 100, 2),
    }


# =========================
# UPDATE RESULTS (PRO)
# =========================

def _has(v) -> bool:
    """¿Hay dato? Un NULL de la base llega como None o como NaN según el
    dtype que pandas le dé a la columna: los dos son "falta el dato"."""
    return v is not None and not pd.isna(v)


def _over_under(total: float, market: str) -> str:
    """Resultado de un over/under de córners, tarjetas o tiros. Con línea
    ENTERA (cards_under_4.0) y el total justo en la línea, la casa devuelve
    el stake: push. Hasta el 22-sep-26 el under la daba ganada y el over
    perdida."""
    line = float(market.split("_")[-1])
    if abs(total - line) < 1e-9:
        return "push"
    return "win" if (total > line) == ("_over_" in market) else "loss"


def resolve_market(market: str, _mrow, odds: float, stake: float) -> tuple[str, float]:
    """
    (outcome, profit) de una bet dado el resultado del partido — función
    PURA (22-sep-26): es la lógica de liquidación de update_bet_results,
    extraída sin cambios para usarla también con las candidatas shadow
    (medir las reglas contra resultados reales). `_mrow` es la fila de
    matches (goles, córners, tarjetas, 1T/2T, tiros); los goles deben
    venir no nulos (lo valida quien llama).
    outcome ∈ win | loss | push | half_win | half_loss | unresolved.
    """
    hg = _mrow["home_goals"]
    ag = _mrow["away_goals"]

    # Default PÉRDIDA solo para mercados con rama en el resolver.
    # Un nombre sin rama se marca unresolved sin tocar bankroll —
    # antes era una pérdida silenciosa inventada (ronda 4, D10).
    if is_resolvable_market(market):
        outcome = "loss"
        profit = -stake
    else:
        outcome = "unresolved"
        profit = 0.0
        log.warning(f"  ⚠️ Mercado sin rama en el resolver: {market!r} → unresolved")

    # =========================
    # EVALUACIÓN MERCADOS
    # =========================

    if market == "home_win" and hg > ag:
        outcome = "win"

    elif market == "away_win" and ag > hg:
        outcome = "win"

    elif market == "draw" and hg == ag:
        outcome = "win"

    elif market == "over25" and (hg + ag) > 2:
        outcome = "win"

    elif market == "under25" and (hg + ag) <= 2:
        outcome = "win"

    elif market == "btts" and hg > 0 and ag > 0:
        outcome = "win"

    elif market == "btts_no" and (hg == 0 or ag == 0):
        outcome = "win"

    elif market == "over_1.5" and (hg + ag) > 1:
        outcome = "win"

    elif market == "under_1.5" and (hg + ag) <= 1:
        outcome = "win"

    elif market == "over_3.5" and (hg + ag) > 3:
        outcome = "win"

    elif market == "under_3.5" and (hg + ag) <= 3:
        outcome = "win"

    elif market == "dc_1x" and hg >= ag:   # local gana o empate
        outcome = "win"

    elif market == "dc_x2" and ag >= hg:   # visitante gana o empate
        outcome = "win"

    elif market == "dc_12" and hg != ag:   # cualquiera gana (no empate)
        outcome = "win"

    elif market.startswith("shots_over_") or market.startswith("shots_under_"):
        hs = _mrow.get("home_shots_target") if _mrow is not None else None
        as_ = _mrow.get("away_shots_target") if _mrow is not None else None
        # _has, no `is not None`: un NULL leído por pandas llega como NaN, y
        # con NaN toda comparación es False → over Y under salían "loss"
        # (se liquidaban como perdidas apuestas sin datos; fix 22-sep-26)
        if _has(hs) and _has(as_):
            outcome = _over_under(float(hs) + float(as_), market)
            if outcome == "push":
                profit = 0.0
        else:
            outcome = "unresolved"
            profit  = 0.0

    elif market == "dnb_home":
        if hg > ag:
            outcome = "win"
        elif hg == ag:
            outcome = "push"   # reembolso
            profit = 0.0

    elif market == "dnb_away":
        if ag > hg:
            outcome = "win"
        elif hg == ag:
            outcome = "push"   # reembolso
            profit = 0.0

    elif market.startswith("ah_home_") or market.startswith("ah_away_"):
        # Formato: "ah_home_-1.5" o "ah_away_-1.5"
        # La línea embebida es SIEMPRE la handicap del local
        try:
            parts     = market.split("_", 2)        # ["ah", "home", "-1.5"]
            side      = parts[1]                    # "home" o "away"
            home_line = float(parts[2])             # -1.5, -1.0, +0.5, -0.25, +0.75 ...
            margin    = hg - ag                     # positivo = local gana

            # Detectar quarter-ball (-0.25, +0.75, -1.25, +1.75, etc.)
            # Pinnacle/Asian books resuelven dividiendo el stake 50/50
            # entre las dos medias líneas adyacentes.
            frac = abs(home_line - round(home_line * 2) / 2)  # distancia a la media más cercana
            is_quarter = abs(home_line * 4 - round(home_line * 4)) < 1e-6 and frac > 1e-6

            def _resolve_half(line_val):
                """Devuelve (outcome, profit) para stake/2 en una línea de media o entera."""
                d = margin + line_val
                half_stake = stake / 2.0
                if abs(d) < 1e-9:
                    return ("push", 0.0)
                home_covers = d > 0
                won = (home_covers and side == "home") or (not home_covers and side == "away")
                if won:
                    return ("win", half_stake * (odds - 1))
                return ("loss", -half_stake)

            if is_quarter:
                lo_out, lo_prof = _resolve_half(home_line - 0.25)
                hi_out, hi_prof = _resolve_half(home_line + 0.25)
                profit = lo_prof + hi_prof
                if lo_out == "win" and hi_out == "win":
                    outcome = "win"
                elif lo_out == "loss" and hi_out == "loss":
                    outcome = "loss"
                elif "win" in (lo_out, hi_out):
                    outcome = "half_win"
                else:
                    outcome = "half_loss"
            else:
                diff = margin + home_line
                if abs(diff) < 1e-9:                # push (línea entera exacta)
                    outcome = "push"
                    profit  = 0.0
                elif diff > 0:                       # local cubre
                    if side == "home":
                        outcome = "win"
                else:                                # visitante cubre
                    if side == "away":
                        outcome = "win"
        except (IndexError, ValueError) as ah_err:
            log.warning(f"⚠️  AH market mal formateado '{market}': {ah_err}")
            # outcome queda como "loss" por default — mejor loggear
            # que fallar silenciosamente

    elif market.startswith("cards_over_") or market.startswith("cards_under_"):
        hy = _mrow.get("home_yellow") if _mrow is not None else None
        ay = _mrow.get("away_yellow") if _mrow is not None else None
        if _has(hy) and _has(ay):      # NaN = sin dato (ver shots)
            outcome = _over_under(float(hy) + float(ay), market)
            if outcome == "push":
                profit = 0.0
        else:
            outcome = "unresolved"
            profit  = 0.0

    elif market in ("h1_home", "h1_draw", "h1_away",
                    "h2_home", "h2_draw", "h2_away"):
        if _mrow is None:
            outcome = "unresolved"
            profit  = 0.0
        else:
            half = market.split("_")[0]   # "h1" o "h2"
            side = "_".join(market.split("_")[1:])  # "home", "draw", "away"
            col_h = "home_goals_ht" if half == "h1" else "home_goals_h2"
            col_a = "away_goals_ht" if half == "h1" else "away_goals_h2"
            gh = _mrow.get(col_h)
            ga = _mrow.get(col_a)
            if gh is None or ga is None or pd.isna(gh) or pd.isna(ga):
                outcome = "unresolved"
                profit  = 0.0
            else:
                gh, ga = float(gh), float(ga)
                if side == "home" and gh > ga:
                    outcome = "win"
                elif side == "away" and ga > gh:
                    outcome = "win"
                elif side == "draw" and gh == ga:
                    outcome = "win"

    elif market.startswith("corners_over_") or market.startswith("corners_under_"):
        hc = _mrow.get("home_corners") if _mrow is not None else None
        ac = _mrow.get("away_corners") if _mrow is not None else None
        if _has(hc) and _has(ac):      # NaN = sin dato (ver shots)
            outcome = _over_under(float(hc) + float(ac), market)
            if outcome == "push":
                profit = 0.0
        else:
            outcome = "unresolved"
            profit  = 0.0

    if outcome == "win":
        profit = stake * (odds - 1)

    return outcome, profit


def _naive_utc(x):
    """Fecha(s) en UTC y sin zona. matches.date llega sin zona (DATE) y
    shadow_bets.match_date con zona (TIMESTAMPTZ): restarlas tal cual lanza
    TypeError. Las fechas sin zona se toman como UTC, igual que antes."""
    t = pd.to_datetime(x, utc=True)
    return t.tz_convert(None) if isinstance(t, pd.Timestamp) else t.dt.tz_convert(None)


def make_match_lookup(_all_matches: pd.DataFrame):
    """
    Buscador de la fila de matches de un partido sobre un DataFrame
    precargado (sin N+1 queries). Extraído de update_bet_results el
    22-sep-26 para reutilizarlo al resolver las candidatas shadow.
    """
    # Nombres normalizados de TODO el DataFrame, calculados una sola vez y
    # solo si hace falta el fallback: antes se recalculaban en cada búsqueda
    # (bien para decenas de bets, lento para miles de candidatas shadow).
    _norm: dict = {}

    def _normalized():
        if not _norm:
            _norm["home"] = _all_matches["home_team_l"].map(lambda t: normalize_team(str(t)))
            _norm["away"] = _all_matches["away_team_l"].map(lambda t: normalize_team(str(t)))
        return _norm["home"], _norm["away"]

    def _lookup_match(home_l: str, away_l: str, match_date) -> pd.Series | None:
        """Devuelve la fila de matches más cercana a match_date (±1 día).

        Matching en dos pasos: exacto en minúsculas primero; si no hay,
        fallback por normalize_team de AMBOS lados (matches puede tener
        el nombre crudo del CSV, ej. "Nott'm Forest" vs el "nottm forest"
        de la API — y viceversa).
        """
        md = _naive_utc(match_date)
        cands = _all_matches[
            (_all_matches["home_team_l"] == home_l) &
            (_all_matches["away_team_l"] == away_l)
        ].copy()
        if cands.empty:
            norm_home, norm_away = _normalized()
            cands = _all_matches[(norm_home == home_l) & (norm_away == away_l)].copy()
        if cands.empty:
            return None
        cands["_dist"] = (_naive_utc(cands["date"]) - md).abs()
        # ±2 días: las fuentes (API vs CSV) a veces difieren en la fecha
        # registrada del mismo partido (reprogramaciones, zona horaria CSV).
        cands = cands[cands["_dist"] <= pd.Timedelta(days=2)]
        if cands.empty:
            return None
        return cands.sort_values("_dist").iloc[0]

    return _lookup_match


_MATCHES_PRELOAD_SQL = """
    SELECT LOWER(home_team) AS home_team_l,
           LOWER(away_team) AS away_team_l,
           date,
           home_goals, away_goals,
           home_corners, away_corners,
           home_yellow, away_yellow,
           home_goals_ht, away_goals_ht,
           home_goals_h2, away_goals_h2,
           home_shots_target, away_shots_target
    FROM matches
    WHERE date BETWEEN :d_from AND :d_to
"""


def _preload_matches(dates: pd.Series) -> pd.DataFrame:
    """Partidos de `matches` en la ventana de `dates` ±2 días (la misma
    tolerancia del buscador): UNA consulta en vez de una por bet."""
    d = _naive_utc(dates)
    return pd.read_sql(text(_MATCHES_PRELOAD_SQL), engine, params={
        "d_from": (d.min() - pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
        "d_to":   (d.max() + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
    })


def plan_bet_settlements(bets: pd.DataFrame, all_matches: pd.DataFrame) -> list[dict]:
    """
    Función PURA: qué escribir para cada bet 'pending' o 'unresolved'.
      - sin partido en `matches` o sin goles → nada (sigue esperando: fuente
        tardía o el resolver con Claude)
      - con goles pero sin los datos de SU mercado (córners, tarjetas,
        1T/2T, tiros) → 'unresolved' (si no lo estaba ya)
      - con todo → resultado final y profit (resolve_market)
    Cada entrada: {id, match, market, was, result, profit}.

    Las 'unresolved' se liquidan AQUÍ en cuanto llegan sus datos. Hasta el
    22-sep-26 se re-marcaban 'pending' y, en la misma corrida, el timeout de
    3 días las devolvía a 'unresolved' (toda unresolved tiene más de 3
    días): nunca se liquidaban y terminaban 'stale', fuera del bankroll.
    """
    if bets.empty:
        return []
    lookup = make_match_lookup(all_matches)
    found: dict = {}   # una búsqueda por partido (el shadow trae ~10 mercados c/u)
    plan = []
    for _, row in bets.iterrows():
        match = str(row["match"])
        if " vs " not in match:
            continue
        home, away = match.split(" vs ", 1)
        market = str(row["market"])
        try:
            key = (match, str(row["match_date"]))
            if key not in found:
                found[key] = lookup(normalize_team(home).lower(), normalize_team(away).lower(),
                                    row["match_date"])
            mrow = found[key]
            if mrow is None or not (_has(mrow["home_goals"]) and _has(mrow["away_goals"])):
                continue
            if all(_has(mrow.get(f)) for f in _market_required_match_fields(market)):
                outcome, profit = resolve_market(market, mrow, float(row["odds"]),
                                                 float(row["stake"]))
            else:
                outcome, profit = "unresolved", 0.0
        except Exception as e:
            log.error(f"❌ Liquidación {match} | {market}: {type(e).__name__}: {e}")
            continue
        if outcome == "unresolved" and row["result"] == "unresolved":
            continue    # sigue esperando sus datos: nada que escribir
        plan.append({"id": int(row["id"]), "match": match, "market": market,
                     "was": str(row["result"]), "result": outcome, "profit": float(profit)})
    return plan


def update_bet_results():
    """
    Liquida las bets 'pending' con kickoff hace más de 3 h y las
    'unresolved' cuyos datos de mercado ya llegaron (plan_bet_settlements).
    Bet y bankroll se escriben en el mismo savepoint (N3, ronda 5).
    """
    print("\n📡 UPDATING BET RESULTS...\n")

    # 3 h tras el kickoff (match_date en UTC) para asegurar que terminó.
    df = pd.read_sql("""
        SELECT *
        FROM bets_history
        WHERE (result = 'pending' AND match_date < NOW() - INTERVAL '3 hours')
           OR result = 'unresolved'
    """, engine)

    if df.empty:
        print("No bets to update")
        return

    # El bankroll se escribe en la misma transacción que cada bet (N3, r5):
    # garantizar el esquema una sola vez antes del batch.
    ensure_bankroll_schema()

    # Pre-carga sobre las fechas de TODAS las candidatas: antes solo las de
    # las pending, y las unresolved más viejas no encontraban su partido.
    plan = plan_bet_settlements(df, _preload_matches(df["match_date"]))

    settled = to_unresolved = late = 0
    with engine.begin() as conn:
        for p in plan:
            final = p["result"] != "unresolved"
            try:
                # Ronda 5 (N3): bet + bankroll en el MISMO savepoint — si el
                # bankroll falla, la bet no queda final y reintenta el
                # próximo ciclo, sin abortar el lote. `result = :was` evita
                # aplicar dos veces el bankroll si otra corrida la liquidó
                # entre la lectura y esta escritura.
                with conn.begin_nested():
                    r = conn.execute(text("""
                        UPDATE bets_history
                        SET result = :result, profit = :profit
                        WHERE id = :id AND result = :was
                    """), {"result": p["result"], "profit": p["profit"],
                           "id": p["id"], "was": p["was"]})
                    if r.rowcount != 1:
                        continue
                    # Una bet 'unresolved' (profit 0) no genera fila de
                    # bankroll: 307 filas amount-0 contaminaban el historial.
                    if final:
                        update_bankroll(
                            profit=p["profit"],
                            notes=f"{p['match']} | {p['market']} | {p['result']}",
                            conn=conn,
                        )
                if final:
                    settled += 1
                    late += p["was"] == "unresolved"
                else:
                    to_unresolved += 1
            except Exception as e:
                log.error(f"❌ Error liquidando {p['match']} | {p['market']}: {e}")

    print(f"✅ Updated {settled} bets"
          + (f" ({late} con datos que llegaron tarde)" if late else "")
          + (f" · {to_unresolved} esperan datos de su mercado" if to_unresolved else ""))

    # ── Timeout escalado (2 etapas) ─────────────────────────────────────
    # Etapa 1: pending → unresolved después de 3 días sin resultado en DB.
    #          Valor agresivo: evita acumulación de "zombies" pending y
    #          libera la ventana de re-check para intentar resolverlos.
    # Etapa 2: unresolved → stale después de 7 días. Terminal: ya no
    #          va a llegar resultado (mercado sin fuente de datos, ej.
    #          córners en ligas sin cobertura). No contamina calibración.
    with engine.begin() as conn:
        r1 = conn.execute(text("""
            UPDATE bets_history
            SET result = 'unresolved'
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '3 days'
        """))
        if r1.rowcount > 0:
            log.warning(f"⚠️  {r1.rowcount} bets 'pending' → 'unresolved' (tras 3 días sin resultado)")

        r2 = conn.execute(text("""
            UPDATE bets_history
            SET result = 'stale'
            WHERE result = 'unresolved'
              AND match_date < NOW() - INTERVAL '7 days'
        """))
        if r2.rowcount > 0:
            print(f"🗑️  {r2.rowcount} bets 'unresolved' → 'stale' (tras 7 días sin datos fuente)")


def audit_stat_settlements() -> dict:
    """
    SOLO LECTURA: re-evalúa con los datos actuales de `matches` las bets de
    córners, tarjetas y tiros ya liquidadas. Hasta el 22-sep-26 un NULL
    leído como NaN se liquidaba "loss" (ver _has): aquí se cuentan las que
    se liquidaron SIN datos (hoy seguirían sin ellos) y las que hoy darían
    otro resultado. No corrige nada — eso es decisión del dueño.
    """
    df = pd.read_sql("""
        SELECT id, match, match_date, market, odds, stake, result, profit
        FROM bets_history
        WHERE result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
          AND split_part(market, '_', 1) IN ('corners', 'cards', 'shots')
    """, engine)
    out = {"checked": int(len(df)), "no_data": 0, "no_data_profit": 0.0,
           "different": 0, "different_profit_delta": 0.0, "unverifiable": 0,
           "details": [], "corrections": []}
    if df.empty:
        return out
    matches = _preload_matches(df["match_date"])
    plan = {p["id"]: p for p in plan_bet_settlements(df.assign(result="pending"), matches)}
    lookup = make_match_lookup(matches)
    for _, row in df.iterrows():
        p = plan.get(int(row["id"]))
        if p is None:
            out["unverifiable"] += 1          # sin partido o sin goles en matches
            continue
        recorded = float(row["profit"] or 0.0)
        if p["result"] == "unresolved":
            out["no_data"] += 1
            out["no_data_profit"] += recorded
        elif p["result"] != row["result"]:
            out["different"] += 1
            out["different_profit_delta"] += p["profit"] - recorded
            # con los datos de hoy SÍ hay resultado: la liquidación correcta
            # la aplica scripts/fix_stat_settlements.py (con --apply)
            out["corrections"].append({
                "id": int(row["id"]), "match": str(row["match"]), "market": str(row["market"]),
                "old_result": str(row["result"]), "old_profit": recorded,
                "new_result": p["result"], "new_profit": round(p["profit"], 4)})
        else:
            continue
        if len(out["details"]) < 20:
            home, away = str(row["match"]).split(" vs ", 1)
            m = lookup(normalize_team(home).lower(), normalize_team(away).lower(),
                       row["match_date"])
            kind = str(row["market"]).split("_")[0]
            cols = {"corners": ("home_corners", "away_corners"),
                    "cards": ("home_yellow", "away_yellow"),
                    "shots": ("home_shots_target", "away_shots_target")}[kind]
            data = [None if m is None or not _has(m.get(c)) else float(m.get(c)) for c in cols]
            out["details"].append(
                f"#{int(row['id'])} {str(row['match_date'])[:10]} {row['match']} | "
                f"{row['market']} @{float(row['odds']):.2f}: registrado {row['result']} "
                f"{recorded:+.2f}u → hoy {p['result']} {p['profit']:+.2f}u "
                f"({kind} {data[0]}+{data[1]})")
    out["no_data_profit"] = round(out["no_data_profit"], 2)
    out["different_profit_delta"] = round(out["different_profit_delta"], 2)
    return out


# ============================================================
# RESULTADOS DE LAS CANDIDATAS SHADOW (22-sep-26)
# ============================================================
# El CLV juzga bien el peso del modelo, pero NO los ajustes manuales (FLB,
# "la tabla miente", empates): esas reglas afirman que el precio está
# sesgado incluso al cierre, así que solo el resultado real puede
# confirmarlas o desmentirlas. Las candidatas shadow se liquidan con la
# MISMA función que las apuestas reales (resolve_market) y stake 1: su
# `profit` es la ganancia por unidad. Sin bankroll: aquí no hay dinero.

SHADOW_STALE_DAYS = 10   # sin datos del partido tras 10 días → 'stale'


def plan_shadow_resolutions(pending: pd.DataFrame, all_matches: pd.DataFrame) -> list[dict]:
    """
    Función PURA: {id, result, profit} por candidata que ya se puede
    liquidar, con la MISMA lógica que las bets reales (plan_bet_settlements)
    y stake 1. Queda para la próxima corrida la que no tiene partido,
    goles, los datos propios de su mercado (córners, tarjetas, 1T/2T,
    tiros) o un mercado que el resolver no sabe liquidar.
    """
    if pending.empty:
        return []
    as_bets = pending.assign(stake=1.0, result="pending")
    return [{"id": p["id"], "result": p["result"], "profit": round(p["profit"], 6)}
            for p in plan_bet_settlements(as_bets, all_matches)
            if p["result"] != "unresolved"]


def resolve_shadow_outcomes(dry_run: bool = False) -> dict:
    """
    Liquida las candidatas shadow ya jugadas (evening, results y weekly).
    Solo toca shadow_bets. Las que siguen sin datos a los
    SHADOW_STALE_DAYS días quedan 'stale' y no se vuelven a consultar.
    dry_run: calcula cuántas se liquidarían sin escribir (smoke test).
    """
    with engine.begin() as conn:
        conn.execute(text(SHADOW_TABLE_SQL))
        conn.execute(text(SHADOW_ALTER_SQL))
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, odds
        FROM shadow_bets
        WHERE result IS NULL
          AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)

    plan = []
    if not pending.empty:
        dates = _naive_utc(pending["match_date"])
        all_matches = pd.read_sql(text("""
            SELECT LOWER(home_team) AS home_team_l,
                   LOWER(away_team) AS away_team_l,
                   date,
                   home_goals, away_goals,
                   home_corners, away_corners,
                   home_yellow, away_yellow,
                   home_goals_ht, away_goals_ht,
                   home_goals_h2, away_goals_h2,
                   home_shots_target, away_shots_target
            FROM matches
            WHERE date BETWEEN :d_from AND :d_to
        """), engine, params={
            "d_from": (dates.min() - pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
            "d_to":   (dates.max() + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
        })
        plan = plan_shadow_resolutions(pending, all_matches)

    if dry_run:
        outcomes = pd.Series([p["result"] for p in plan], dtype=object).value_counts().to_dict()
        return {"pending": int(len(pending)), "resolved": len(plan), "stale": 0,
                "outcomes": {str(k): int(v) for k, v in outcomes.items()}, "dry_run": True}

    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE shadow_bets
                SET result = :result, profit = :profit, resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE shadow_bets
            SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL
              AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount

    summary = {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}
    print(f"🌑 Shadow resultados: {summary['resolved']}/{summary['pending']} liquidadas"
          + (f", {summary['stale']} sin datos tras {SHADOW_STALE_DAYS} días → stale"
             if summary["stale"] else ""))
    return summary


def _nearest_market_row(home, away, bet_match_date):
    """Fila de upcoming_matches más cercana al kickoff (±4h) — B2 ronda 7.
    Compartida por el closing de bets_history y de shadow_bets."""
    return pd.read_sql(text("""
        SELECT *
        FROM upcoming_matches
        WHERE home_team_norm = :home
        AND away_team_norm = :away
        AND match_date BETWEEN CAST(:date_from AS timestamptz) AND CAST(:date_to AS timestamptz)
        ORDER BY ABS(EXTRACT(EPOCH FROM (match_date - CAST(:exact_date AS timestamptz)))) ASC
        LIMIT 1
    """), engine, params={
        "home":       normalize_team(home).lower().strip(),
        "away":       normalize_team(away).lower().strip(),
        "date_from":  (bet_match_date - pd.Timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S+00:00"),
        "date_to":    (bet_match_date + pd.Timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S+00:00"),
        "exact_date": bet_match_date.strftime("%Y-%m-%d %H:%M:%S+00:00"),
    })


def _line_of(market: str):
    """Línea embebida en un mercado paramétrico ('ah_home_-0.50' → -0.5)."""
    try:
        return float(str(market).rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return None


def _same_line(market: str, row_line) -> bool:
    """
    ¿La línea del cierre es la MISMA que la apostada? Si la línea principal
    se movió (AH -0.5 → -0.75, córners 9.5 → 10.5), la cuota de la fila ya
    es de otro mercado: compararla daría un CLV inventado. Sin línea
    comparable no hay cierre (None), que es la respuesta honesta.
    """
    bet_line = _line_of(market)
    try:
        rl = float(row_line)
    except (TypeError, ValueError):
        return False
    if bet_line is None or rl != rl:   # rl != rl → NaN
        return False
    return abs(bet_line - rl) < 1e-6


def _closing_odds_for(market, odds_row):
    """Mapeo mercado → cuota de cierre (compartido bets/shadow, ronda 7)."""
    return closing_quote_for(market, odds_row)[0]


# Mercados cuya cuota llega en el fetch "featured" (/odds: h2h, totals,
# spreads). El resto (btts, DNB de la API, doble oportunidad, medio tiempo,
# córners, tarjetas) llega por evento (/events/{id}/odds) y se refresca por
# separado: su frescura es specialty_fetched_at, no odds_fetched_at.
_FEATURED_MARKETS = frozenset({"home_win", "draw", "away_win", "over25", "under25"})


def closing_quote_for(market, odds_row):
    """
    (cuota de cierre, cuándo se descargó) para un mercado, desde una fila
    de upcoming_matches. La hora sale de la columna del fetch que trajo ESA
    cuota: odds_fetched_at (featured) o specialty_fetched_at (por evento).
    La hora es None en filas anteriores al 22-sep-26 (frescura desconocida).
    """
    closing_odds = None
    featured = market in _FEATURED_MARKETS or str(market).startswith(("ah_home_", "ah_away_"))

    # =========================
    # MAPEO MERCADOS
    # =========================

    if market == "home_win":
        closing_odds = odds_row.get("home_odds")

    elif market == "draw":
        closing_odds = odds_row.get("draw_odds")

    elif market == "away_win":
        closing_odds = odds_row.get("away_odds")

    elif market == "over25":
        closing_odds = odds_row.get("over25_odds")

    elif market == "under25":
        closing_odds = odds_row.get("under25_odds")

    elif market == "btts":
        closing_odds = odds_row.get("btts_yes_odds")

    elif market == "btts_no":
        closing_odds = odds_row.get("btts_no_odds")

    elif market in ("dnb_home", "dnb_away"):
        # Misma fuente que la apertura (prediction_pipeline): cuotas DNB de
        # la API si están las dos patas; si no, derivadas del 1X2. Antes el
        # cierre SIEMPRE se derivaba del 1X2 (sin margen) aunque la apuesta
        # se hubiera tomado a la cuota de la API (con margen): el CLV de
        # DNB salía sesgado por la diferencia de márgenes, no por el mercado.
        dh, da = odds_row.get("dnb_home_odds"), odds_row.get("dnb_away_odds")
        if dh and da and dh == dh and da == da and dh > 1 and da > 1:
            closing_odds = dh if market == "dnb_home" else da
        else:
            featured = True   # derivado del 1X2 → frescura del fetch featured
            h = odds_row.get("home_odds")
            a = odds_row.get("away_odds")
            if h and a and h > 1 and a > 1:
                imp_sum = 1/h + 1/a
                own = h if market == "dnb_home" else a
                closing_odds = round(imp_sum / (1/own), 3)

    elif market.startswith("ah_home_") or market.startswith("ah_away_"):
        # La línea embebida es la del local, igual que ah_line de la fila.
        if _same_line(market, odds_row.get("ah_line")):
            side = market.split("_")[1]   # "home" o "away"
            if side == "home":
                closing_odds = odds_row.get("ah_home_odds")
            else:
                closing_odds = odds_row.get("ah_away_odds")

    elif market == "dc_1x":
        closing_odds = odds_row.get("dc_1x_odds")
    elif market == "dc_x2":
        closing_odds = odds_row.get("dc_x2_odds")
    elif market == "dc_12":
        closing_odds = odds_row.get("dc_12_odds")

    elif market == "h1_home":
        closing_odds = odds_row.get("h1_home_odds")
    elif market == "h1_draw":
        closing_odds = odds_row.get("h1_draw_odds")
    elif market == "h1_away":
        closing_odds = odds_row.get("h1_away_odds")
    elif market == "h2_home":
        closing_odds = odds_row.get("h2_home_odds")
    elif market == "h2_draw":
        closing_odds = odds_row.get("h2_draw_odds")
    elif market == "h2_away":
        closing_odds = odds_row.get("h2_away_odds")

    elif market.startswith("corners_over_") or market.startswith("corners_under_"):
        if _same_line(market, odds_row.get("corners_line")):
            if market.startswith("corners_over_"):
                closing_odds = odds_row.get("corners_over_odds")
            else:
                closing_odds = odds_row.get("corners_under_odds")

    elif market.startswith("cards_over_") or market.startswith("cards_under_"):
        if _same_line(market, odds_row.get("cards_line")):
            if market.startswith("cards_over_"):
                closing_odds = odds_row.get("cards_over_odds")
            else:
                closing_odds = odds_row.get("cards_under_odds")

    # Salida saneada: una celda vacía llega como NaN desde pandas y se
    # escribía tal cual (NaN en NUMERIC pasa el filtro `closing_odds > 1`
    # de Postgres, porque NaN ordena por encima de todo).
    try:
        v = float(closing_odds)
    except (TypeError, ValueError):
        return None, None
    if not (v == v and v > 1):
        return None, None
    fetched = odds_row.get("odds_fetched_at" if featured else "specialty_fetched_at")
    try:
        if fetched is not None and pd.isna(fetched):
            fetched = None
    except (TypeError, ValueError):
        pass
    return v, fetched



def _update_shadow_closing():
    """Rellena closing_odds de shadow_bets con el MISMO lookup y mapeo que
    bets_history (B2, ronda 7) — el CLV por bandas depende de esto.

    Desde el 22-sep-26 guarda closing_fetched_at (cuándo se descargó la
    cuota usada) y REEMPLAZA el cierre si llega uno descargado más cerca del
    kickoff: cada corrida del closing acerca el cierre al precio final en
    vez de quedarse con la primera cuota vista (que solía ser la misma de
    apertura → movimiento 0 falso). Solo el aprendizaje usa los cierres que
    pasan closing_quality.is_valid_closing."""
    from src.utils.closing_quality import should_update_closing
    with engine.begin() as conn:
        conn.execute(text(SHADOW_ALTER_SQL))
    sdf = pd.read_sql(text("""
        SELECT id, match, market, match_date, closing_odds, closing_fetched_at
        FROM shadow_bets
        WHERE match_date BETWEEN NOW() - INTERVAL '10 days'
                             AND NOW() + INTERVAL '90 minutes'
          AND (closing_odds IS NULL OR closing_fetched_at IS NULL
               OR closing_fetched_at < match_date)
    """), engine)
    if sdf.empty:
        return

    s_updated = 0
    rows_cache: dict = {}
    with engine.begin() as conn:
        for _, row in sdf.iterrows():
            match = row["match"]
            market = row["market"]
            bet_match_date = pd.to_datetime(row["match_date"], utc=True)
            try:
                home, away = match.split(" vs ")
            except ValueError:
                continue

            # una sola consulta por partido (el shadow trae ~10 mercados c/u)
            key = (match, str(bet_match_date))
            if key not in rows_cache:
                rows_cache[key] = _nearest_market_row(home, away, bet_match_date)
            odds_df = rows_cache[key]
            if odds_df.empty:
                continue

            closing_odds, fetched_at = closing_quote_for(market, odds_df.iloc[0])
            if closing_odds is None:
                continue
            current = row["closing_fetched_at"]
            if fetched_at is None:
                # frescura desconocida (fila previa al 22-sep): solo si no
                # había cierre, y sin hora — el aprendizaje no la usará
                if not pd.isna(row["closing_odds"]):
                    continue
            elif not should_update_closing(current, fetched_at, bet_match_date):
                continue

            conn.execute(text("""
                UPDATE shadow_bets
                SET closing_odds = :closing_odds, closing_fetched_at = :fetched_at
                WHERE id = :id
            """), {"closing_odds": float(closing_odds),
                   "fetched_at": pd.to_datetime(fetched_at, utc=True).to_pydatetime()
                   if fetched_at is not None else None,
                   "id": int(row["id"])})
            s_updated += 1

    print(f"🌑 Shadow closing: {s_updated}/{len(sdf)}")


def update_closing_odds():
    """
    Backfill de cierres (lo usa scripts/one_shot_data_quality_cleanup.py).
    Delegado a scripts/update_closing_odds.py en modo backfill: hasta el
    22-sep-26 esto era una segunda copia de esa lógica que nunca podía
    funcionar (la búsqueda de la fila quedó indentada dentro del `except`,
    así que `odds_df` no existía → UnboundLocalError en cada bet).
    """
    from scripts.update_closing_odds import update_closing_odds as _update
    _update(only_near_kickoff=False)