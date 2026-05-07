"""
scripts/validate_world_cup.py
==============================
Valida el modelo contra el Mundial 2022 (64 partidos cargados desde
martj42/international_results) y reporta:
  - Brier Score
  - Win-rate predicho vs real por mercado (1X2 + Over 2.5)
  - ROI hipotético si se hubiera apostado a edge >= MIN_EDGE
  - Factor de calibración sugerido

NO TOCA bets_history. Es read-only sobre la tabla `matches`. La idea es:
  1. Antes del Mundial 2026: correr este script con WORLD_CUP_BETTING_ENABLED=false
     para validar que la calibración 2022 cumple ROI ≥ 0.
  2. Durante el Mundial 2026: correr en paralelo a paper_trades.jsonl y
     comparar predicciones contra resultados reales en `matches`.
  3. Si ambos validan → flip kill-switch a true.

Uso:
    python scripts/validate_world_cup.py
    python scripts/validate_world_cup.py --market over25
    python scripts/validate_world_cup.py --year 2022
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.models.dixon_coles_model import match_outcomes
from src.models.dc_mle_fitter import get_dc_lambdas, _load_params

# Edge mínimo para considerar una "apuesta hipotética" — alineado con prod.
MIN_EDGE = 0.05
# Odds asumidas por mercado para el ROI hipotético (las del closing del WC).
# Para una validación más fina habría que cruzar con archivo histórico real
# de odds; estos son promedios del torneo 2022 según OddsPortal y similares.
DEFAULT_ODDS = {
    "home_win": 2.40,
    "draw":     3.30,
    "away_win": 3.20,
    "over25":   1.95,
    "under25":  1.95,
}


def _load_matches(year: int = 2022) -> pd.DataFrame:
    """Lee los partidos del Mundial desde la tabla matches."""
    with engine.connect() as c:
        df = pd.read_sql(text("""
            SELECT date, home_team, away_team, home_goals, away_goals,
                   COALESCE(neutral, FALSE) AS neutral, league
              FROM matches
             WHERE league = 'soccer_fifa_world_cup'
               AND home_goals IS NOT NULL
               AND away_goals IS NOT NULL
               AND EXTRACT(YEAR FROM date) = :y
             ORDER BY date
        """), c, params={"y": year})
    return df


def _predict_match(home: str, away: str, is_neutral: bool):
    """
    Genera (p_home, p_draw, p_away, p_over25, lam_h, lam_a).
    Usa solo el modelo MLE (suficiente para validación; no toca xG/forma).
    Si el equipo no está en los parámetros MLE, devuelve None.
    """
    res = get_dc_lambdas(home, away, is_neutral=is_neutral)
    if res is None:
        return None

    lam_h, lam_a = res
    p_h, p_d, p_a = match_outcomes(lam_h, lam_a)

    # P(over 2.5) usando Poisson bivariada simple
    from scipy.stats import poisson
    p_total = 0.0
    for i in range(8):
        for j in range(8):
            if i + j >= 3:
                p_total += poisson.pmf(i, lam_h) * poisson.pmf(j, lam_a)

    return {
        "p_home_win": p_h,
        "p_draw":     p_d,
        "p_away_win": p_a,
        "p_over25":   p_total,
        "lam_h":      lam_h,
        "lam_a":      lam_a,
    }


def _outcomes(row) -> dict:
    hg, ag = int(row["home_goals"]), int(row["away_goals"])
    return {
        "home_win": int(hg > ag),
        "draw":     int(hg == ag),
        "away_win": int(hg < ag),
        "over25":   int(hg + ag >= 3),
        "under25":  int(hg + ag <= 2),
    }


def _brier(predicted: list, actual: list) -> float:
    if not predicted:
        return float("nan")
    p = np.array(predicted)
    a = np.array(actual)
    return float(np.mean((p - a) ** 2))


def _hypothetical_roi(predictions: list, outcomes: list,
                      odds: float, min_edge: float = MIN_EDGE) -> dict:
    """
    Calcula ROI si se hubiera apostado 1u flat a cada predicción con
    edge >= min_edge. No usa Kelly — flat-staking para análisis simple.
    """
    bets = 0
    pnl  = 0.0
    wins = 0
    for prob, won in zip(predictions, outcomes):
        implied = 1.0 / odds
        edge = prob - implied
        if edge < min_edge:
            continue
        bets += 1
        if won:
            pnl += odds - 1
            wins += 1
        else:
            pnl -= 1
    roi = (pnl / bets * 100) if bets else 0.0
    wr  = (wins / bets * 100) if bets else 0.0
    return {"bets": bets, "wins": wins, "pnl": pnl, "roi_pct": roi, "wr_pct": wr}


def validate(year: int = 2022, market_filter: str | None = None) -> dict:
    print(f"\n🌍 VALIDACIÓN MUNDIAL {year}")
    print("=" * 60)

    params = _load_params()
    if not params:
        print("❌ No hay parámetros MLE ajustados (data/dc_params.json).")
        print("   Corre primero: python -m src.models.dc_mle_fitter")
        return {"status": "no_mle_params"}

    fitted_at = params.get("fitted_at", "?")
    n_teams   = params.get("n_teams", "?")
    print(f"Parámetros MLE: {n_teams} equipos | fitted_at={fitted_at}")

    df = _load_matches(year)
    if df.empty:
        print(f"⚠️  Sin partidos del Mundial {year} en tabla matches.")
        return {"status": "no_matches"}

    print(f"Partidos cargados: {len(df)}")
    print()

    pred_cols = {"home_win": [], "draw": [], "away_win": [], "over25": []}
    actual_cols = {k: [] for k in pred_cols}
    skipped_no_team = 0

    for _, row in df.iterrows():
        pred = _predict_match(row["home_team"], row["away_team"],
                              is_neutral=bool(row["neutral"]))
        if pred is None:
            skipped_no_team += 1
            continue

        outc = _outcomes(row)
        pred_cols["home_win"].append(pred["p_home_win"])
        pred_cols["draw"].append(pred["p_draw"])
        pred_cols["away_win"].append(pred["p_away_win"])
        pred_cols["over25"].append(pred["p_over25"])

        actual_cols["home_win"].append(outc["home_win"])
        actual_cols["draw"].append(outc["draw"])
        actual_cols["away_win"].append(outc["away_win"])
        actual_cols["over25"].append(outc["over25"])

    n_used = len(pred_cols["home_win"])
    if skipped_no_team:
        print(f"⚠️  {skipped_no_team} partido(s) sin parámetros MLE para algún equipo.")
    print(f"Partidos analizados: {n_used}\n")

    if n_used == 0:
        return {"status": "all_skipped", "skipped": skipped_no_team}

    # ── Reporte por mercado ─────────────────────────────────────────────────
    print(f"{'Mercado':<10} {'Brier':>7} {'Pred%':>7} {'Real%':>7} "
          f"{'Bias':>6} {'ROI(flat)':>10} {'WR':>6} {'N':>4}")
    print("─" * 65)

    overall: dict = {"year": year, "n_matches": n_used,
                     "skipped_no_team": skipped_no_team, "markets": {}}

    for market in ["home_win", "draw", "away_win", "over25"]:
        if market_filter and market != market_filter:
            continue
        preds = pred_cols[market]
        acts  = actual_cols[market]
        brier = _brier(preds, acts)
        wr_pred = np.mean(preds)
        wr_real = np.mean(acts)
        bias    = wr_pred - wr_real

        odds = DEFAULT_ODDS.get(market, 2.0)
        roi_data = _hypothetical_roi(preds, acts, odds)

        # Sugiere factor de calibración: actual / predicted
        cal_factor = wr_real / wr_pred if wr_pred > 0 else 1.0
        cal_factor = max(0.75, min(1.30, cal_factor))

        print(f"{market:<10} {brier:>7.4f} {wr_pred*100:>6.1f}% "
              f"{wr_real*100:>6.1f}% {bias*100:>+5.1f}pp "
              f"{roi_data['roi_pct']:>+9.1f}% {roi_data['wr_pct']:>5.1f}% "
              f"{roi_data['bets']:>4d}")

        overall["markets"][market] = {
            "brier":               round(brier, 5),
            "win_rate_pred":       round(float(wr_pred), 4),
            "win_rate_actual":     round(float(wr_real), 4),
            "bias_pp":             round(float(bias) * 100, 2),
            "calibration_factor":  round(float(cal_factor), 4),
            "hypothetical_roi_pct": round(roi_data["roi_pct"], 2),
            "n_bets_flat":         roi_data["bets"],
            "wins_flat":           roi_data["wins"],
        }

    print()
    # ── Diagnóstico ─────────────────────────────────────────────────────────
    bad = [m for m, d in overall["markets"].items()
           if d["calibration_factor"] < 0.85 or d["calibration_factor"] > 1.15]
    if bad:
        print(f"⚠️  Calibración fuera de rango en: {', '.join(bad)}")
        print("   Considera: (1) más datos previos al Mundial,")
        print("              (2) WORLD_CUP_BETTING_ENABLED=false otro mes.")
    else:
        print("✅ Calibración dentro de ±15%. El modelo es razonable para WC.")

    overall["status"] = "ok"
    return overall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2022,
                    help="Año del Mundial a validar (2022 cargado por default)")
    ap.add_argument("--market", type=str, default=None,
                    help="Filtra a un solo mercado (home_win, draw, etc.)")
    args = ap.parse_args()

    result = validate(year=args.year, market_filter=args.market)
    if result.get("status") not in ("ok",):
        sys.exit(1)


if __name__ == "__main__":
    main()
