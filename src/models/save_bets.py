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
        created_at   TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (match, market, match_date)
    )
"""

# Tablas creadas antes del 22-sep-26 no tienen closing_fetched_at
# (cuándo se descargó la cuota usada como cierre — ver closing_quality).
SHADOW_ALTER_SQL = "ALTER TABLE shadow_bets ADD COLUMN IF NOT EXISTS closing_fetched_at TIMESTAMPTZ"
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
                try:
                    # savepoint por fila: un fallo no aborta el lote
                    with conn.begin_nested():
                        conn.execute(text("""
                            INSERT INTO shadow_bets
                                (match, match_date, league, market, p_final,
                                 p_ref, deviation, odds, edge_market, reason)
                            VALUES
                                (:match, :match_date, :league, :market, :p_final,
                                 :p_ref, :deviation, :odds, :edge_market, :reason)
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
    """
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE bets_history
            SET odds_placed = :odds_placed
            WHERE match = :match
              AND market = :market
              AND match_date = :match_date
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

def update_bet_results():

    print("\n📡 UPDATING BET RESULTS...\n")

    # Fix timezone: esperar 3h después del kickoff para asegurar que el
    # partido terminó (match_date es el kickoff en UTC, 90+15 min + buffer).
    df = pd.read_sql("""
        SELECT *
        FROM bets_history
        WHERE result = 'pending'
        AND match_date < NOW() - INTERVAL '3 hours'
    """, engine)

    if df.empty:
        print("No bets to update")
        return

    # El bankroll se escribe en la misma transacción que cada bet (N3, r5):
    # garantizar el esquema una sola vez antes del batch.
    ensure_bankroll_schema()

    # ── Pre-carga de resultados (elimina N+1 queries) ─────────────────────
    # En vez de hacer 1-5 pd.read_sql() por bet, traemos TODOS los partidos
    # relevantes en UNA sola query y resolvemos en memoria.
    _min_date = (df["match_date"].min() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    _max_date = (df["match_date"].max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    _all_matches = pd.read_sql(text("""
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
    """), engine, params={"d_from": _min_date, "d_to": _max_date})

    def _lookup_match(home_l: str, away_l: str, match_date) -> pd.Series | None:
        """Devuelve la fila de matches más cercana a match_date (±1 día).

        Matching en dos pasos: exacto en minúsculas primero; si no hay,
        fallback por normalize_team de AMBOS lados (matches puede tener
        el nombre crudo del CSV, ej. "Nott'm Forest" vs el "nottm forest"
        de la API — y viceversa).
        """
        md = pd.to_datetime(match_date)
        cands = _all_matches[
            (_all_matches["home_team_l"] == home_l) &
            (_all_matches["away_team_l"] == away_l)
        ].copy()
        if cands.empty:
            cands = _all_matches[
                _all_matches["home_team_l"].apply(
                    lambda t: normalize_team(str(t)) == home_l)
                & _all_matches["away_team_l"].apply(
                    lambda t: normalize_team(str(t)) == away_l)
            ].copy()
        if cands.empty:
            return None
        cands["_dist"] = (pd.to_datetime(cands["date"]) - md).abs()
        # ±2 días: las fuentes (API vs CSV) a veces difieren en la fecha
        # registrada del mismo partido (reprogramaciones, zona horaria CSV).
        cands = cands[cands["_dist"] <= pd.Timedelta(days=2)]
        if cands.empty:
            return None
        return cands.sort_values("_dist").iloc[0]

    updated = 0

    with engine.begin() as conn:

        for _, row in df.iterrows():

            try:
                match = row["match"]
                market = row["market"]
                odds = row["odds"]
                stake = row["stake"]

                # =========================
                # SAFE SPLIT
                # =========================
                if " vs " not in match:
                    continue

                home, away = match.split(" vs ")

                home = normalize_team(home)
                away = normalize_team(away)

                # =========================
                # FETCH RESULT (NORMALIZED)
                # =========================
                match_date = pd.to_datetime(row["match_date"])
                _mrow = _lookup_match(home.lower(), away.lower(), match_date)

                if _mrow is None:
                    continue

                hg = _mrow["home_goals"]
                ag = _mrow["away_goals"]

                # Fix: validar NULL antes de comparar (antes causaba TypeError
                # silencioso → bet quedaba "pending" indefinidamente → profit
                # real se perdía del learning)
                if hg is None or ag is None or pd.isna(hg) or pd.isna(ag):
                    continue

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
                    if hs is not None and as_ is not None:
                        total_shots = float(hs) + float(as_)
                        line = float(market.split("_")[-1])
                        if market.startswith("shots_over_"):
                            outcome = "win" if total_shots > line else "loss"
                        else:
                            outcome = "win" if total_shots <= line else "loss"
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
                    if hy is not None and ay is not None:
                        total_cards = float(hy) + float(ay)
                        line = float(market.split("_")[-1])
                        if market.startswith("cards_over_"):
                            outcome = "win" if total_cards > line else "loss"
                        else:
                            outcome = "win" if total_cards <= line else "loss"
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
                    if hc is not None and ac is not None:
                        total_corners = float(hc) + float(ac)
                        line = float(market.split("_")[-1])
                        if market.startswith("corners_over_"):
                            outcome = "win" if total_corners > line else "loss"
                        else:
                            outcome = "win" if total_corners <= line else "loss"
                    else:
                        outcome = "unresolved"
                        profit  = 0.0

                if outcome == "win":
                    profit = stake * (odds - 1)

                # =========================
                # UPDATE (+ SAVEPOINT por fila)
                # =========================
                # Ronda 5 (N3): bet + bankroll se escriben en el MISMO
                # savepoint — si el bankroll falla, la bet no se marca final
                # y reintenta el próximo ciclo (antes el try/except tragaba
                # el fallo y la bet quedaba final con bankroll sin aplicar:
                # drift de −7.54u medido en producción). El savepoint aísla
                # el fallo a esta fila sin abortar la transacción del batch.

                with conn.begin_nested():
                    conn.execute(text("""
                        UPDATE bets_history
                        SET result = :result,
                            profit = :profit
                        WHERE id = :id
                    """), {
                        "result": outcome,
                        "profit": float(profit),
                        "id": int(row["id"])
                    })

                    # ── Actualizar bankroll real ──────────────────────────
                    # Cada vez que se resuelve una apuesta, el bankroll se
                    # actualiza para que el Kelly del próximo ciclo use el
                    # capital correcto. Una bet 'unresolved' (profit 0) ya no
                    # genera fila de bankroll: 307 filas amount-0 contaminaban
                    # el historial y desacomodaban la reconciliación.
                    if outcome != "unresolved":
                        update_bankroll(
                            profit=float(profit),
                            notes=f"{match} | {market} | {outcome}",
                            conn=conn,
                        )

                updated += 1

            except Exception as e:
                print("❌ Error:", e)

    print(f"✅ Updated {updated} bets")

    # ── Re-check: intentar resolver bets 'unresolved' que ahora tengan resultado ──
    # Los resultados pueden llegar tarde (fetch_results corre diario).
    # Si ahora hay un resultado en matches, reclasificamos la bet.
    unresolved_df = pd.read_sql("""
        SELECT *
        FROM bets_history
        WHERE result = 'unresolved'
    """, engine)
    if not unresolved_df.empty:
        recheck_count = 0
        # Traemos TODAS las columnas que algún market puede necesitar en
        # una sola query — antes traíamos sólo home_goals/away_goals y
        # re-marcábamos como pending aunque los campos específicos
        # (corners/cards/HT/shots) siguieran NULL, causando bouncing
        # pending↔unresolved que disparaba Claude en cada cron.
        with engine.begin() as conn:
            for _, row in unresolved_df.iterrows():
                try:
                    match = row["match"]
                    if " vs " not in match:
                        continue
                    home, away = match.split(" vs ")
                    home = normalize_team(home)
                    away = normalize_team(away)
                    match_date = pd.to_datetime(row["match_date"])
                    required = _market_required_match_fields(row["market"])
                    _ur_row = _lookup_match(home.lower(), away.lower(), match_date)
                    if _ur_row is None:
                        continue
                    # Validar que TODOS los campos requeridos por este market
                    # estén presentes — no sólo goles. Sin esto un bet de
                    # corners se re-marcaba como pending cuando home_goals
                    # existían pero home_corners era NULL, generando el
                    # loop infinito que costaba tokens.
                    fields_ready = all(
                        _ur_row.get(f) is not None and not pd.isna(_ur_row.get(f))
                        for f in required
                    )
                    if not fields_ready:
                        continue
                    # Datos completos para ESTE market → re-pending para reevaluar
                    conn.execute(text("""
                        UPDATE bets_history
                        SET result = 'pending'
                        WHERE id = :id
                    """), {"id": int(row["id"])})
                    recheck_count += 1
                except Exception as e:
                    # Fix: logging para no perder errores silenciosamente
                    log.error(f"⚠️  Recheck error ({row.get('match', '?')}): {e}")
        if recheck_count > 0:
            print(f"🔄 {recheck_count} bets 'unresolved' re-marcadas como 'pending' (datos market-specific ya completos)")

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