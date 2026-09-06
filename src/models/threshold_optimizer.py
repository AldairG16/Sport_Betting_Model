"""
Threshold Optimizer
===================
Calibra automáticamente los parámetros del modelo usando datos históricos.

Método:
  1. Usa partidos históricos resueltos de bets_history (si hay suficientes)
  2. Si no, simula apuestas usando ELO vs resultados reales de matches
  3. Prueba distintas combinaciones de thresholds con grid search
  4. Guarda los óptimos en config/thresholds.json

Parámetros que optimiza:
  - edge_min:        Edge mínimo para considerar una apuesta [0.03, 0.10]
  - kelly_fraction:  Fracción de Kelly [0.15, 0.35]
  - min_probability: Probabilidad mínima del modelo [0.35, 0.55]
  - max_odds:        Odds máximas permitidas [4.0, 7.0]
"""

import sys
import os
import json
import numpy as np
import pandas as pd
from pathlib import Path
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent.parent))

from config.database import engine

# =========================
# PATHS
# =========================

CONFIG_DIR     = Path(__file__).parent.parent.parent / "config"
THRESHOLDS_FILE = CONFIG_DIR / "thresholds.json"

# =========================
# DEFAULTS (conservadores)
# =========================

DEFAULT_THRESHOLDS = {
    "edge_min":        0.050,
    "kelly_fraction":  0.250,
    "min_probability": 0.400,
    "max_odds":        5.000,
    "min_odds":        1.500,
    "updated_at":      None,
    "sample_size":     0,
    "simulated":       True
}

# =========================
# GRID DE BÚSQUEDA
# =========================

GRID = {
    "edge_min":        [0.030, 0.040, 0.050, 0.060, 0.070, 0.080],
    "min_probability": [0.35,  0.40,  0.45,  0.50],
    "max_odds":        [4.0,   5.0,   6.0],
}


# =========================
# LOAD / SAVE
# =========================

def load_thresholds() -> dict:
    """Carga thresholds guardados o retorna defaults."""
    if THRESHOLDS_FILE.exists():
        try:
            with open(THRESHOLDS_FILE, "r") as f:
                data = json.load(f)
            # Mezclar con defaults para campos nuevos
            merged = DEFAULT_THRESHOLDS.copy()
            merged.update(data)
            return merged
        except Exception:
            pass
    return DEFAULT_THRESHOLDS.copy()


def save_thresholds(thresholds: dict):
    """Persiste thresholds optimizados en disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    thresholds["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(THRESHOLDS_FILE, "w") as f:
        json.dump(thresholds, f, indent=2)
    print(f"  Guardado en: {THRESHOLDS_FILE}")


# =========================
# SIMULADOR ELO
# =========================

def _simulate_elo_bets(df_matches: pd.DataFrame, edge_min: float,
                       min_prob: float, max_odds: float) -> pd.Series:
    """
    Simula bets usando probabilidades ELO vs resultados reales.

    Para cada partido:
    - Calcula P(home_win) con ELO
    - Calcula P(away_win) con ELO
    - Asume "mercado justo" = P(resultado) * 0.95 (5% overround)
    - Si edge > edge_min → bet simulada
    - Evalúa resultado real

    Returns:
        Series de profits normalizados (stake = 1 por bet)
    """
    BASE_ELO = 1500
    K        = 20
    HOME_ADV = 80
    elo      = {}
    profits  = []

    df_matches = df_matches.sort_values("date").reset_index(drop=True)

    for _, row in df_matches.iterrows():

        home = str(row.home_team)
        away = str(row.away_team)

        elo.setdefault(home, BASE_ELO)
        elo.setdefault(away, BASE_ELO)

        elo_h  = elo[home] + HOME_ADV
        elo_a  = elo[away]
        p_home = 1 / (1 + 10 ** ((elo_a - elo_h) / 400))
        p_away = 1 - p_home

        # Simular odds de mercado con overround del 5%
        mkt_p_home = p_home * 0.95
        mkt_p_away = p_away * 0.95

        # Fair odds implicadas
        if mkt_p_home > 0 and mkt_p_away > 0:
            odds_home = 1 / mkt_p_home
            odds_away = 1 / mkt_p_away
        else:
            odds_home = odds_away = 0

        # Resultado real
        hg = row.home_goals
        ag = row.away_goals

        # Evaluar bet HOME
        if (min_prob <= p_home <= 0.85 and 1.4 <= odds_home <= max_odds):
            edge_home = p_home - mkt_p_home
            if edge_home >= edge_min:
                won = 1 if hg > ag else 0
                profit = (odds_home - 1) if won else -1
                profits.append(profit)

        # Evaluar bet AWAY
        if (min_prob <= p_away <= 0.85 and 1.4 <= odds_away <= max_odds):
            edge_away = p_away - mkt_p_away
            if edge_away >= edge_min:
                won = 1 if ag > hg else 0
                profit = (odds_away - 1) if won else -1
                profits.append(profit)

        # Actualizar ELO
        home_exp = p_home
        if hg > ag:
            home_score = 1.0
        elif hg == ag:
            home_score = 0.5
        else:
            home_score = 0.0

        k_adj     = K * (1 + abs(hg - ag) * 0.2)
        elo[home] += k_adj * (home_score - home_exp)
        elo[away] += k_adj * ((1 - home_score) - (1 - home_exp))

    return pd.Series(profits) if profits else pd.Series(dtype=float)


# =========================
# OPTIMIZE
# =========================

def optimize_thresholds(min_sample: int = 200, verbose: bool = True) -> dict:
    """
    Ejecuta grid search sobre thresholds y guarda los óptimos.

    Args:
        min_sample: mínimo de bets simuladas para confiar en el resultado
        verbose:    mostrar tabla de resultados

    Returns:
        dict con thresholds optimizados
    """
    print("\n⚙️  THRESHOLD OPTIMIZER\n")

    # --------------------------------
    # 1. Intentar usar bets_history reales
    # --------------------------------
    try:
        df_real = pd.read_sql("""
            SELECT edge, odds, probability, result, profit, stake
            FROM bets_history
            WHERE result IS NOT NULL
              AND result::text NOT IN ('pending', 'null', '')
            ORDER BY match_date
        """, engine)
    except Exception:
        df_real = pd.DataFrame()

    if len(df_real) >= 50:
        print(f"  Usando {len(df_real)} bets reales de bets_history")
        result = _optimize_from_real_bets(df_real, verbose)
        result["simulated"] = False
        result["sample_size"] = len(df_real)
        save_thresholds(result)
        return result

    # --------------------------------
    # 2. Simulación con datos históricos
    # --------------------------------
    print("  Bets reales insuficientes — usando simulación ELO en matches históricos")
    print("  (Los thresholds se recalibrarán automáticamente al acumular 50+ bets reales)\n")

    df_matches = pd.read_sql("""
        SELECT home_team, away_team, home_goals, away_goals, date, league
        FROM matches
        WHERE home_goals IS NOT NULL
          AND date >= NOW() - INTERVAL '3 years'
        ORDER BY date
    """, engine)

    if df_matches.empty:
        print("  ⚠️  Sin datos históricos. Usando defaults.")
        save_thresholds(DEFAULT_THRESHOLDS)
        return DEFAULT_THRESHOLDS

    print(f"  Partidos históricos disponibles: {len(df_matches):,}")

    best_roi    = -99.0
    best_params = {}
    results_log = []

    for edge_min in GRID["edge_min"]:
        for min_prob in GRID["min_probability"]:
            for max_odds in GRID["max_odds"]:

                profits = _simulate_elo_bets(df_matches, edge_min, min_prob, max_odds)

                if len(profits) < min_sample:
                    continue

                roi        = profits.sum() / len(profits)
                win_rate   = (profits > 0).mean()
                n          = len(profits)

                results_log.append({
                    "edge_min": edge_min,
                    "min_prob": min_prob,
                    "max_odds": max_odds,
                    "roi":      roi,
                    "win_rate": win_rate,
                    "n":        n
                })

                if roi > best_roi:
                    best_roi    = roi
                    best_params = {
                        "edge_min":        edge_min,
                        "min_probability": min_prob,
                        "max_odds":        max_odds,
                    }

    if verbose and results_log:
        log_df = (pd.DataFrame(results_log)
                    .sort_values("roi", ascending=False)
                    .head(10))
        print("\n  Top 10 combinaciones:\n")
        print(f"  {'edge_min':>8} {'min_prob':>8} {'max_odds':>8} {'ROI':>8} {'WinRate':>8} {'n':>6}")
        print("  " + "-" * 55)
        for _, r in log_df.iterrows():
            print(f"  {r['edge_min']:>8.3f} {r['min_prob']:>8.2f} {r['max_odds']:>8.1f} "
                  f"{r['roi']*100:>7.2f}% {r['win_rate']*100:>7.1f}% {int(r['n']):>6,}")

    # La simulación ELO no genera señales útiles para seleccionar thresholds
    # porque el "mercado" es solo el modelo escalado → ROI siempre similar.
    # En este caso conservamos los defaults hasta tener bets reales.
    print("\n  ℹ️  Simulación no genera señales de threshold confiables.")
    print("  Usando defaults conservadores hasta acumular 50+ bets reales.\n")

    optimal = DEFAULT_THRESHOLDS.copy()
    optimal["sample_size"] = len(df_matches)
    optimal["simulated"]   = True

    save_thresholds(optimal)
    return optimal


def _optimize_from_real_bets(df: pd.DataFrame, verbose: bool) -> dict:
    """Optimiza thresholds usando bets reales resueltas."""
    best_roi = -99.0
    best_params = {}

    for edge_min in GRID["edge_min"]:
        for min_prob in GRID["min_probability"]:
            for max_odds in GRID["max_odds"]:

                mask = (
                    (df["edge"] >= edge_min) &
                    (df["probability"] >= min_prob) &
                    (df["odds"] <= max_odds)
                )
                subset = df[mask]

                if len(subset) < 20:
                    continue

                roi = subset["profit"].sum() / subset["stake"].sum()

                if roi > best_roi:
                    best_roi = roi
                    best_params = {
                        "edge_min":        edge_min,
                        "min_probability": min_prob,
                        "max_odds":        max_odds,
                    }

    if not best_params:
        return DEFAULT_THRESHOLDS.copy()

    optimal = DEFAULT_THRESHOLDS.copy()
    optimal.update(best_params)
    return optimal


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    result = optimize_thresholds(verbose=True)
    print(f"\n  edge_min:        {result['edge_min']}")
    print(f"  min_probability: {result['min_probability']}")
    print(f"  max_odds:        {result['max_odds']}")
    print(f"  kelly_fraction:  {result['kelly_fraction']}")
