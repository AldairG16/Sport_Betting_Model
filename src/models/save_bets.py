import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.models.bankroll_manager import update_bankroll, ensure_bankroll_schema


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

    df = pd.read_sql("""
        SELECT *
        FROM bets_history
        WHERE result = 'pending'
        AND match_date < NOW()
    """, engine)

    if df.empty:
        print("No bets to update")
        return

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
                # CRITICO: filtrar por fecha del partido (±3 dias)
                # Sin este filtro, el sistema usa resultados de partidos
                # de temporadas anteriores entre los mismos equipos.
                match_date = pd.to_datetime(row["match_date"])
                result_df = pd.read_sql(text("""
                    SELECT home_goals, away_goals
                    FROM matches
                    WHERE LOWER(home_team) = :home
                    AND LOWER(away_team) = :away
                    AND date BETWEEN :date_from AND :date_to
                    ORDER BY date DESC
                    LIMIT 1
                """), engine, params={
                    "home":      home.lower(),
                    "away":      away.lower(),
                    "date_from": (match_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                    "date_to":   (match_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                })

                if result_df.empty:
                    continue

                hg = result_df.iloc[0]["home_goals"]
                ag = result_df.iloc[0]["away_goals"]

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
                    match_date = pd.to_datetime(row["match_date"])
                    shots_df = pd.read_sql(text("""
                        SELECT home_shots_target, away_shots_target
                        FROM matches
                        WHERE LOWER(home_team) = :home
                        AND LOWER(away_team) = :away
                        AND date BETWEEN :date_from AND :date_to
                        ORDER BY date DESC
                        LIMIT 1
                    """), engine, params={
                        "home":      home.lower(),
                        "away":      away.lower(),
                        "date_from": (match_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                        "date_to":   (match_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    })
                    if not shots_df.empty:
                        hs = shots_df.iloc[0]["home_shots_target"]
                        as_ = shots_df.iloc[0]["away_shots_target"]
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
                        parts     = market.split("_")          # ["ah", "home", "-1.5"]
                        side      = parts[1]                   # "home" o "away"
                        home_line = float(parts[2])            # -1.5, -1.0, +0.5 ...
                        margin    = hg - ag                    # positivo = local gana
                        diff      = margin + home_line

                        if abs(diff) < 1e-9:                   # push (línea entera exacta)
                            outcome = "push"
                            profit  = 0.0
                        elif diff > 0:                         # local cubre
                            if side == "home":
                                outcome = "win"
                        else:                                   # visitante cubre
                            if side == "away":
                                outcome = "win"
                    except (IndexError, ValueError):
                        pass   # market mal formateado → queda como "loss"

                elif market.startswith("cards_over_") or market.startswith("cards_under_"):
                    match_date = pd.to_datetime(row["match_date"])
                    cards_df = pd.read_sql(text("""
                        SELECT home_yellow, away_yellow
                        FROM matches
                        WHERE LOWER(home_team) = :home
                        AND LOWER(away_team) = :away
                        AND date BETWEEN :date_from AND :date_to
                        ORDER BY date DESC
                        LIMIT 1
                    """), engine, params={
                        "home":      home.lower(),
                        "away":      away.lower(),
                        "date_from": (match_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                        "date_to":   (match_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    })
                    if not cards_df.empty:
                        hy = cards_df.iloc[0]["home_yellow"]
                        ay = cards_df.iloc[0]["away_yellow"]
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

                elif market.startswith("corners_over_") or market.startswith("corners_under_"):
                    # Resolver bets de córners usando home_corners + away_corners de matches
                    match_date = pd.to_datetime(row["match_date"])
                    corners_df = pd.read_sql(text("""
                        SELECT home_corners, away_corners
                        FROM matches
                        WHERE LOWER(home_team) = :home
                        AND LOWER(away_team) = :away
                        AND date BETWEEN :date_from AND :date_to
                        ORDER BY date DESC
                        LIMIT 1
                    """), engine, params={
                        "home":      home.lower(),
                        "away":      away.lower(),
                        "date_from": (match_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                        "date_to":   (match_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    })
                    if not corners_df.empty:
                        hc = corners_df.iloc[0]["home_corners"]
                        ac = corners_df.iloc[0]["away_corners"]
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
                    result_df = pd.read_sql(text("""
                        SELECT home_goals, away_goals
                        FROM matches
                        WHERE LOWER(home_team) = :home
                        AND LOWER(away_team) = :away
                        AND date BETWEEN :date_from AND :date_to
                        ORDER BY date DESC
                        LIMIT 1
                    """), engine, params={
                        "home":      home.lower(),
                        "away":      away.lower(),
                        "date_from": (match_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                        "date_to":   (match_date + pd.Timedelta(days=3)).strftime("%Y-%m-%d"),
                    })
                    if not result_df.empty:
                        # Resultado encontrado → re-insertar como pending para que se resuelva
                        conn.execute(text("""
                            UPDATE bets_history
                            SET result = 'pending'
                            WHERE id = :id
                        """), {"id": int(row["id"])})
                        recheck_count += 1
                except Exception:
                    pass
        if recheck_count > 0:
            print(f"🔄 {recheck_count} bets 'unresolved' re-marcadas como 'pending' (resultado encontrado)")

    # ── Timeout: bets pendientes de hace +7 días sin resultado en DB ──────
    # Aplica a partidos internacionales (clasificatorias, amistosos) que
    # no están en football-data.co.uk y nunca se resolverán automáticamente.
    # Usamos 7 días (en vez de 3) para dar tiempo a que lleguen los resultados.
    with engine.begin() as conn:
        r = conn.execute(text("""
            UPDATE bets_history
            SET result = 'unresolved'
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '7 days'
        """))
        if r.rowcount > 0:
            print(f"⚠️  {r.rowcount} bets marcadas 'unresolved' (sin resultado en DB tras 7 dias)")

def update_closing_odds():

    print("\n📡 UPDATING CLOSING ODDS...\n")

    df = pd.read_sql("""
        SELECT id, match, market
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

            try:
                home, away = match.split(" vs ")
            except ValueError:
                continue

            odds_df = pd.read_sql(text("""
                SELECT *
                FROM upcoming_matches
                WHERE LOWER(home_team) = :home
                AND LOWER(away_team) = :away
                ORDER BY match_date DESC
                LIMIT 1
            """), engine, params={
                "home": home.lower(),
                "away": away.lower()
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