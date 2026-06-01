import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.models.bankroll_manager import update_bankroll, ensure_bankroll_schema


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

    # Asegurar columna league existe
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE bets_history
            ADD COLUMN IF NOT EXISTS league TEXT
        """))

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
                    clv
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
                    :clv
                )
                ON CONFLICT (match, market, match_date) DO NOTHING
            """), row.to_dict())

    print(f"✅ {len(df)} bets saved to bets_history")


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
        """Devuelve la fila de matches más cercana a match_date (±1 día)."""
        md = pd.to_datetime(match_date)
        cands = _all_matches[
            (_all_matches["home_team_l"] == home_l) &
            (_all_matches["away_team_l"] == away_l)
        ].copy()
        if cands.empty:
            return None
        cands["_dist"] = (pd.to_datetime(cands["date"]) - md).abs()
        cands = cands[cands["_dist"] <= pd.Timedelta(days=1)]
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

                outcome = "loss"
                profit = -stake

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
                        print(f"⚠️  AH market mal formateado '{market}': {ah_err}")
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
                # UPDATE
                # =========================

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

                # ── Actualizar bankroll real ──────────────────────────────
                # Cada vez que se resuelve una apuesta, el bankroll se
                # actualiza para que el Kelly del próximo ciclo use el
                # capital correcto.
                try:
                    ensure_bankroll_schema()
                    update_bankroll(
                        profit=float(profit),
                        notes=f"{match} | {market} | {outcome}"
                    )
                except Exception as br_err:
                    print(f"⚠️ bankroll update skipped: {br_err}")

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
                    print(f"⚠️  Recheck error ({row.get('match', '?')}): {e}")
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
            print(f"⚠️  {r1.rowcount} bets 'pending' → 'unresolved' (tras 3 días sin resultado)")

        r2 = conn.execute(text("""
            UPDATE bets_history
            SET result = 'stale'
            WHERE result = 'unresolved'
              AND match_date < NOW() - INTERVAL '7 days'
        """))
        if r2.rowcount > 0:
            print(f"🗑️  {r2.rowcount} bets 'unresolved' → 'stale' (tras 7 días sin datos fuente)")

def update_closing_odds():

    print("\n📡 UPDATING CLOSING ODDS...\n")

    # Fix: incluir match_date para filtrar el partido correcto (antes
    # un bet de hace 3 meses tomaba las odds de un partido FUTURO del
    # mismo enfrentamiento → CLV completamente falso)
    df = pd.read_sql("""
        SELECT id, match, market, match_date
        FROM bets_history
        WHERE closing_odds IS NULL
    """, engine)

    if df.empty:
        print("No bets need closing odds")
        return

    updated = 0

    with engine.begin() as conn:

        for _, row in df.iterrows():

            match = row["match"]
            market = row["market"]
            bet_match_date = pd.to_datetime(row["match_date"])

            try:
                home, away = match.split(" vs ")
            except ValueError:
                continue

            # Fix: buscar el partido MÁS CERCANO en fecha (±4 horas del
            # kickoff original). Esto garantiza que tomamos odds del partido
            # correcto, no de otro enfrentamiento futuro del mismo equipo.
            # upcoming_matches.home_team / away_team vienen RAW de la API
            # (ej. "Independiente Rivadavia"). Usamos home_team_norm / away_team_norm
            # que guardan la forma canónica (ej. "ind rivadavia"), que es la misma
            # que aparece en bets_history.match.
            # upcoming_matches.match_date es TIMESTAMPTZ. Pasamos strings
            # con offset UTC explícito (+00:00) para que la comparación sea
            # inequívoca (bet_match_date es naive pero sabemos que está en UTC).
            odds_df = pd.read_sql(text("""
                SELECT *
                FROM upcoming_matches
                WHERE home_team_norm = :home
                AND away_team_norm = :away
                AND match_date BETWEEN :date_from::timestamptz AND :date_to::timestamptz
                ORDER BY ABS(EXTRACT(EPOCH FROM (match_date - :exact_date::timestamptz))) ASC
                LIMIT 1
            """), engine, params={
                "home":       normalize_team(home).lower().strip(),
                "away":       normalize_team(away).lower().strip(),
                "date_from":  (bet_match_date - pd.Timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "date_to":    (bet_match_date + pd.Timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "exact_date": bet_match_date.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            })

            if odds_df.empty:
                continue

            odds_row = odds_df.iloc[0]

            closing_odds = None

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

            elif market == "dnb_home":
                # DNB odds derivadas de h2h closing odds
                h = odds_row.get("home_odds")
                a = odds_row.get("away_odds")
                if h and a and h > 1 and a > 1:
                    imp_sum = 1/h + 1/a
                    closing_odds = round(imp_sum / (1/h), 3)

            elif market == "dnb_away":
                h = odds_row.get("home_odds")
                a = odds_row.get("away_odds")
                if h and a and h > 1 and a > 1:
                    imp_sum = 1/h + 1/a
                    closing_odds = round(imp_sum / (1/a), 3)

            elif market.startswith("ah_home_") or market.startswith("ah_away_"):
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
                if market.startswith("corners_over_"):
                    closing_odds = odds_row.get("corners_over_odds")
                else:
                    closing_odds = odds_row.get("corners_under_odds")

            elif market.startswith("cards_over_") or market.startswith("cards_under_"):
                if market.startswith("cards_over_"):
                    closing_odds = odds_row.get("cards_over_odds")
                else:
                    closing_odds = odds_row.get("cards_under_odds")

            if closing_odds is None:
                continue

            conn.execute(text("""
                UPDATE bets_history
                SET closing_odds = :closing_odds
                WHERE id = :id
            """), {
                "closing_odds": float(closing_odds),
                "id": int(row["id"])
            })

            updated += 1

    print(f"✅ Closing odds updated: {updated}")