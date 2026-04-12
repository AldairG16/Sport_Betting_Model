"""
src/pipeline/mlb_pipeline.py
============================
Pipeline de prediccion para MLB.

Modelo v1:
  - Elo especifico de MLB (solo partidos baseball_mlb)
  - Tasa de carreras por equipo (ultimos 20 juegos) como lambda Poisson
  - Blend: 60% run-rate Poisson + 40% Elo
  - Mercados: moneyline (home_win / away_win), totals (over/under carreras), run line (ah ±1.5)
  - Sin empates, sin corners, sin BTTS

Reutiliza:
  - find_value_bets(), kelly_stake() de betting_engine
  - save_bets() de save_bets
  - line_moved_against(), should_skip_low_liquidity() de line_movement
  - asian_handicap_model.prob_ah() para run line
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from scipy.stats import poisson
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.models.betting_engine import find_value_bets, kelly_stake
from src.models.save_bets import save_bets
from src.features.line_movement import line_moved_against, should_skip_low_liquidity
from src.models.asian_handicap_model import prob_ah
from src.models.bankroll_manager import get_current_bankroll, ensure_bankroll_schema
from src.utils.team_normalizer import normalize_team

# ============================================================
# CONSTANTES
# ============================================================
LEAGUE          = "baseball_mlb"
FORM_WINDOW     = 20     # ultimos N juegos para calcular lambda
MIN_GAMES       = 10     # minimo de juegos historicos para predecir
BASE_ELO        = 1500
K_ELO           = 20
HOME_ADV_ELO    = 40     # ventaja local en MLB es menor que en futbol
GOAL_DIFF_ELO   = 0.20
ELO_WEIGHT      = 0.40   # peso del Elo en el blend final
RUN_WEIGHT      = 0.60   # peso del modelo de carreras en el blend
MIN_EDGE        = 0.08   # edge minimo para considerar value
MAX_ODDS        = 5.0    # cuota maxima
MAX_PROB_RATIO  = 2.0    # prob modelo > 2x mercado = sospechosa
MAX_EDGE        = 0.499  # edge maximo confiable

# ============================================================
# ELO ESPECIFICO DE MLB
# ============================================================

def _compute_mlb_elo() -> dict:
    """
    Calcula ratings Elo solo con partidos de MLB.
    Retorna dict {team_name: elo_rating}.
    """
    df = pd.read_sql("""
        SELECT home_team, away_team, home_goals, away_goals, date
        FROM matches
        WHERE league = 'baseball_mlb'
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
        ORDER BY date ASC
    """, engine)

    elo = {}

    for _, row in df.iterrows():
        home = row["home_team"]
        away = row["away_team"]
        elo.setdefault(home, BASE_ELO)
        elo.setdefault(away, BASE_ELO)

        home_adj = elo[home] + HOME_ADV_ELO
        away_adj = elo[away]
        home_exp = 1 / (1 + 10 ** ((away_adj - home_adj) / 400))

        hg = row["home_goals"]
        ag = row["away_goals"]
        if hg > ag:
            home_actual, away_actual = 1.0, 0.0
        elif hg < ag:
            home_actual, away_actual = 0.0, 1.0
        else:
            home_actual, away_actual = 0.5, 0.5  # raro en MLB pero puede pasar (suspended)

        run_diff = abs(hg - ag)
        mult = 1.0 + run_diff * GOAL_DIFF_ELO

        k = K_ELO * mult
        elo[home] += k * (home_actual - home_exp)
        elo[away] += k * (away_actual - (1 - home_exp))

    return elo


# ============================================================
# TASA DE CARRERAS (LAMBDA POISSON)
# ============================================================

def _get_run_rates(team: str, match_date: str) -> dict:
    """
    Calcula carreras promedio anotadas y recibidas por un equipo
    en sus ultimos FORM_WINDOW juegos antes de match_date.

    Retorna {"scored": float, "allowed": float, "games": int}
    """
    df = pd.read_sql(
        text("""
            SELECT home_team, away_team, home_goals, away_goals
            FROM matches
            WHERE league = 'baseball_mlb'
              AND (LOWER(home_team) = :team OR LOWER(away_team) = :team)
              AND date < :match_date
              AND home_goals IS NOT NULL
            ORDER BY date DESC
            LIMIT :window
        """),
        engine,
        params={
            "team":       team.lower(),
            "match_date": match_date,
            "window":     FORM_WINDOW,
        },
    )

    if df.empty:
        return {"scored": 4.5, "allowed": 4.5, "games": 0}  # promedio MLB

    scored = allowed = 0.0
    for _, row in df.iterrows():
        if row["home_team"].lower() == team.lower():
            scored  += row["home_goals"]
            allowed += row["away_goals"]
        else:
            scored  += row["away_goals"]
            allowed += row["home_goals"]

    n = len(df)
    return {
        "scored":  scored / n,
        "allowed": allowed / n,
        "games":   n,
    }


# ============================================================
# PROBABILIDADES DE TOTALS (OVER/UNDER)
# ============================================================

def _poisson_totals(lambda_home: float, lambda_away: float, line: float) -> dict:
    """
    Calcula P(over) y P(under) para una linea de totals dada.
    Usa distribucion de Poisson bivariada.
    """
    max_runs = 30
    p_over = p_under = p_exact = 0.0

    for h in range(max_runs + 1):
        for a in range(max_runs + 1):
            p = poisson.pmf(h, lambda_home) * poisson.pmf(a, lambda_away)
            total = h + a
            if total > line:
                p_over += p
            elif total < line:
                p_under += p
            else:
                p_exact += p

    # Linea exacta: si la linea es entera hay push → se divide
    # En MLB las lineas suelen ser .5 (ej. 8.5) → sin push
    if abs(line - round(line)) < 1e-9:
        p_over  += p_exact * 0.5
        p_under += p_exact * 0.5
    else:
        # Linea .5 → todo va a un lado
        if line % 1 > 0:
            p_over  += p_exact  # si decimal > 0, total exacto NO supera la linea

    total_prob = p_over + p_under
    if total_prob < 1e-9:
        return {"over": 0.5, "under": 0.5}

    return {
        "over":  round(p_over / total_prob, 4),
        "under": round(p_under / total_prob, 4),
    }


# ============================================================
# PROBABILIDAD DE MONEYLINE (BLEND ELO + RUN RATE)
# ============================================================

def _blend_win_prob(
    elo_home: float, elo_away: float,
    lambda_home: float, lambda_away: float,
) -> tuple[float, float]:
    """
    Combina ELO y run-rate Poisson para estimar P(home wins).
    MLB no tiene empates.
    """
    # Elo win prob
    home_elo_adj = elo_home + HOME_ADV_ELO
    elo_home_win = 1 / (1 + 10 ** ((elo_away - home_elo_adj) / 400))

    # Poisson win prob (simulacion hasta max_runs)
    max_runs = 25
    p_home_win = p_away_win = 0.0
    for h in range(max_runs + 1):
        for a in range(max_runs + 1):
            p = poisson.pmf(h, lambda_home) * poisson.pmf(a, lambda_away)
            if h > a:
                p_home_win += p
            elif a > h:
                p_away_win += p
            # empate: MLB juega extra innings hasta haber ganador → ignoramos

    # Normalizar Poisson (sin empates)
    denom = p_home_win + p_away_win
    if denom < 1e-9:
        pois_home_win = 0.5
    else:
        pois_home_win = p_home_win / denom

    # Blend
    final_home = ELO_WEIGHT * elo_home_win + RUN_WEIGHT * pois_home_win
    final_home = max(0.05, min(0.95, final_home))
    final_away = 1.0 - final_home

    return round(final_home, 4), round(final_away, 4)


# ============================================================
# SAFE ODDS HELPER
# ============================================================

def _safe_odds(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

def run_mlb_pipeline():
    """
    Genera value bets para partidos MLB proximos.
    Guarda en bets_history y retorna lista de bets.
    """
    print("\nRUNNING MLB PREDICTION PIPELINE\n")

    ensure_bankroll_schema()
    current_bankroll = get_current_bankroll()
    print(f"Bankroll actual: {current_bankroll:.2f} unidades")

    # ── Cargar ELO MLB ────────────────────────────────────────────────
    print("Calculando Elo MLB...")
    elo = _compute_mlb_elo()
    print(f"  Equipos con Elo: {len(elo)}")

    # ── Cargar partidos proximos de MLB ───────────────────────────────
    df = pd.read_sql("""
        SELECT *
        FROM upcoming_matches
        WHERE sport_key = 'baseball_mlb'
          AND match_date::timestamp BETWEEN NOW() + INTERVAL '1 hour'
                                       AND NOW() + INTERVAL '3 days'
        ORDER BY match_date
    """, engine)

    if df.empty:
        print("Sin partidos MLB proximos en DB.")
        print("  Asegurate de que 'baseball_mlb' este en SPORT_KEYS y corre update_upcoming_matches.py")
        return []

    print(f"Partidos MLB encontrados: {len(df)}\n")

    all_bets = []
    skipped_no_data  = 0
    skipped_no_odds  = 0
    skipped_sharp    = 0
    skipped_liquidity = 0

    for _, row in df.iterrows():
        home = normalize_team(row["home_team"])
        away = normalize_team(row["away_team"])
        match_str  = f"{row['home_team']} vs {row['away_team']}"
        match_date = str(row["match_date"])[:10]

        # ── Odds basicas ─────────────────────────────────────────────
        home_odds   = _safe_odds(row.get("home_odds"))
        away_odds   = _safe_odds(row.get("away_odds"))
        over25_odds = _safe_odds(row.get("over25_odds"))   # linea de totals de la DB
        under25_odds = _safe_odds(row.get("under25_odds"))
        ah_home_odds = _safe_odds(row.get("ah_home_odds"))
        ah_away_odds = _safe_odds(row.get("ah_away_odds"))
        ah_line      = row.get("ah_line")
        bk_count     = int(row.get("bookmaker_count") or 0)

        if not home_odds or not away_odds:
            skipped_no_odds += 1
            continue

        # ── Filtro de liquidez ────────────────────────────────────────
        if should_skip_low_liquidity(bk_count):
            skipped_liquidity += 1
            continue

        # ── Datos historicos de carreras ──────────────────────────────
        home_runs = _get_run_rates(home, match_date)
        away_runs = _get_run_rates(away, match_date)

        if home_runs["games"] < MIN_GAMES or away_runs["games"] < MIN_GAMES:
            skipped_no_data += 1
            continue

        # ── Lambdas ───────────────────────────────────────────────────
        # lambda = tasa de anotacion propia × factor defensa rival
        # Normalizado por promedio MLB (~4.5 carreras/equipo)
        MLB_AVG = 4.5
        lambda_home = (home_runs["scored"] * away_runs["allowed"]) / MLB_AVG
        lambda_away = (away_runs["scored"] * home_runs["allowed"]) / MLB_AVG

        # Clamp razonable para MLB
        lambda_home = max(1.0, min(lambda_home, 10.0))
        lambda_away = max(1.0, min(lambda_away, 10.0))

        # ── Elo ───────────────────────────────────────────────────────
        elo_home = elo.get(home, BASE_ELO)
        elo_away = elo.get(away, BASE_ELO)

        # ── Probabilidades ────────────────────────────────────────────
        p_home_win, p_away_win = _blend_win_prob(elo_home, elo_away, lambda_home, lambda_away)

        model_probs = {
            "home_win": p_home_win,
            "away_win": p_away_win,
        }

        # Totals: usamos la linea real del mercado si la hay
        # La DB guarda over25/under25 como el par Over 2.5 de futbol,
        # pero para MLB en upcoming_matches viene del mercado 'totals'
        # con punto != 2.5 (suele ser 8.5, 9, etc.)
        # El script update_upcoming_matches.py filtra solo over25_odds (point==2.5)
        # Para MLB no hay point 2.5 → over25_odds sera NULL
        # Usamos ah_line como proxy de totals si esta disponible
        # TODO: en la proxima iteracion parsear la linea correcta de totals para MLB
        # Por ahora calculamos con lambda sum como total esperado
        total_line = lambda_home + lambda_away  # linea implicita del modelo
        if total_line > 1:
            totals = _poisson_totals(lambda_home, lambda_away, total_line)
            # Solo agregamos si tenemos odds reales de mercado
            # (over25_odds sera None para MLB por la razon de arriba)

        # Run line (Asian Handicap ±1.5 — mercado principal de MLB)
        if ah_line is not None:
            try:
                ah_line_f = float(ah_line)
                p_rl_home, p_rl_away = prob_ah(lambda_home, lambda_away, ah_line_f)
                model_probs[f"ah_home_{ah_line_f:+.1f}"] = round(p_rl_home, 4)
                model_probs[f"ah_away_{ah_line_f:+.1f}"] = round(p_rl_away, 4)
            except Exception:
                pass

        # ── Odds dict ────────────────────────────────────────────────
        odds = {
            "home_win": home_odds,
            "away_win": away_odds,
        }
        if ah_line is not None and ah_home_odds:
            try:
                ah_line_f = float(ah_line)
                odds[f"ah_home_{ah_line_f:+.1f}"] = ah_home_odds
                odds[f"ah_away_{ah_line_f:+.1f}"] = ah_away_odds
            except Exception:
                pass

        # ── Line movement filter ──────────────────────────────────────
        try:
            from src.features.line_movement import get_line_movement
            line_data = get_line_movement(match_str)
        except Exception:
            line_data = None

        # ── Value bets ────────────────────────────────────────────────
        raw_bets = find_value_bets(model_probs, odds)

        if not raw_bets:
            continue

        # Filtrar por line movement
        filtered_bets = []
        for bet in raw_bets:
            if line_data and line_moved_against(bet["market"], line_data):
                skipped_sharp += 1
                continue
            filtered_bets.append(bet)

        if not filtered_bets:
            continue

        # Top 2 bets por partido
        for bet in filtered_bets[:2]:
            edge = float(bet["edge"])
            odds_val = float(bet["odds"])
            prob = float(bet["probability"])

            # Filtrar sospechosas y edge bajo
            if edge >= MAX_EDGE:
                continue
            if edge < MIN_EDGE:
                continue
            if odds_val > MAX_ODDS:
                continue
            if odds_val > 0:
                market_implied = 1.0 / odds_val
                if market_implied > 0 and prob > MAX_PROB_RATIO * market_implied:
                    continue

            stake = kelly_stake(prob, odds_val, current_bankroll)
            if stake <= 0:
                continue

            all_bets.append({
                "match":       match_str,
                "match_date":  str(row["match_date"]),
                "league":      LEAGUE,
                "market":      bet["market"],
                "probability": round(prob, 4),
                "odds":        odds_val,
                "edge":        round(edge, 4),
                "stake":       round(stake, 2),
            })

    # ── Correlacion: reducir stake si hay 2 bets del mismo partido ───
    from collections import Counter
    match_counts = Counter(b["match"] for b in all_bets)
    for b in all_bets:
        n = match_counts[b["match"]]
        if n > 1:
            b["stake"] = round(b["stake"] / (n ** 0.5), 2)

    # ── Portfolio exposure cap (mismo 15% que soccer) ─────────────────
    if all_bets and current_bankroll > 0:
        total_stake = sum(b["stake"] for b in all_bets)
        max_stake   = current_bankroll * 0.15
        if total_stake > max_stake:
            factor = max_stake / total_stake
            for b in all_bets:
                b["stake"] = round(b["stake"] * factor, 2)

    # ── Debug summary ──────────────────────────────────────────────────
    print(f"Sin odds:         {skipped_no_odds}")
    print(f"Sin datos hist.:  {skipped_no_data}")
    print(f"Sin liquidez:     {skipped_liquidity}")
    print(f"Sharp rechazados: {skipped_sharp}")
    print(f"Bets generadas:   {len(all_bets)}")

    if all_bets:
        print("\n⚾ MLB BEST BETS")
        for b in all_bets[:5]:
            print(f"  {b['match']} | {b['market']} | edge: {b['edge']} | odds: {b['odds']} | stake: {b['stake']}")

        save_bets(all_bets)
        print(f"\n✅ {len(all_bets)} bets MLB guardadas en bets_history")

    return all_bets
