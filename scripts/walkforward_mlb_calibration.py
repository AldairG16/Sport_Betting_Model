"""
scripts/walkforward_mlb_calibration.py
=======================================
Walk-forward de CALIBRACIÓN para el modelo MLB.

OJO: este NO mide ROI ni edge — no hay odds históricos MLB en la DB.
Lo que medimos es si el modelo es estadísticamente sano:

  • Brier score (cuanto más bajo, mejor; <0.25 es "decente",
    >0.25 = peor que apostar 50/50 fijo)
  • Log loss
  • Curva de calibración (predicciones vs realidad por bucket)
  • Accuracy de selección del favorito
  • Diferencia vs baseline "home siempre gana"

Si el modelo no pasa este test, no tiene sentido buscar odds.
Si pasa, el siguiente paso es paper-trading con odds reales.

Train: partidos hasta 2024-03-31
Test:  partidos 2024-04-01 → 2024-09-30 (temporada regular 2024)
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine

# ============================================================
# RE-IMPLEMENTACIÓN STANDALONE (no usar pipeline en vivo
# para evitar dependencias de upcoming_matches)
# ============================================================

BASE_ELO       = 1500
K_ELO          = 20
HOME_ADV_ELO   = 40
GOAL_DIFF_ELO  = 0.20
ELO_WEIGHT     = 0.40
RUN_WEIGHT     = 0.60
FORM_WINDOW    = 20
MIN_GAMES      = 10
MLB_AVG        = 4.5

TRAIN_CUTOFF = "2024-04-01"
TEST_START   = "2024-04-01"
TEST_END     = "2024-10-01"


def _load_games() -> pd.DataFrame:
    df = pd.read_sql("""
        SELECT date, home_team, away_team, home_goals, away_goals
        FROM matches
        WHERE league = 'baseball_mlb'
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
        ORDER BY date ASC
    """, engine)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _compute_elo_up_to(df_all: pd.DataFrame, cutoff_date) -> dict:
    """Calcula Elo sólo con partidos antes de cutoff_date."""
    df = df_all[df_all["date"] < cutoff_date]
    elo = {}
    for _, row in df.iterrows():
        h, a = row["home_team"], row["away_team"]
        elo.setdefault(h, BASE_ELO)
        elo.setdefault(a, BASE_ELO)

        h_adj = elo[h] + HOME_ADV_ELO
        h_exp = 1 / (1 + 10 ** ((elo[a] - h_adj) / 400))

        hg, ag = row["home_goals"], row["away_goals"]
        if hg > ag:   h_act, a_act = 1.0, 0.0
        elif hg < ag: h_act, a_act = 0.0, 1.0
        else:         h_act, a_act = 0.5, 0.5

        mult = 1.0 + abs(hg - ag) * GOAL_DIFF_ELO
        k = K_ELO * mult
        elo[h] += k * (h_act - h_exp)
        elo[a] += k * (a_act - (1 - h_exp))
    return elo


def _build_team_history_index(df_all: pd.DataFrame) -> dict:
    """
    Pre-construye un dict {team: [(date, scored, allowed), ...]}
    ordenado por fecha. Después _get_run_rates_fast hace un O(window)
    en vez de un O(N) scan del DataFrame por cada juego.
    """
    idx = {}
    for _, row in df_all.iterrows():
        h, a = row["home_team"], row["away_team"]
        d = row["date"]; hg = row["home_goals"]; ag = row["away_goals"]
        idx.setdefault(h, []).append((d, hg, ag))
        idx.setdefault(a, []).append((d, ag, hg))
    return idx


def _get_run_rates_fast(team_idx: dict, team: str, match_date) -> dict:
    """Run rates de últimos FORM_WINDOW juegos antes de match_date."""
    games = team_idx.get(team, [])
    # binary search: encontrar los que tienen date < match_date
    # games está ordenado por date (construido en orden cronológico)
    import bisect
    dates = [g[0] for g in games]
    cutoff_pos = bisect.bisect_left(dates, match_date)
    relevant = games[max(0, cutoff_pos - FORM_WINDOW):cutoff_pos]
    if not relevant:
        return {"scored": 4.5, "allowed": 4.5, "games": 0}
    scored = sum(g[1] for g in relevant)
    allowed = sum(g[2] for g in relevant)
    n = len(relevant)
    return {"scored": scored / n, "allowed": allowed / n, "games": n}


def _blend_win_prob(elo_home, elo_away, lam_h, lam_a) -> float:
    """Devuelve P(home wins) con blend Elo + Poisson, sin empates.

    Vectorizado: en vez de 26x26 = 676 llamadas a poisson.pmf por juego,
    pre-calcula los dos vectores pmf y los multiplica con broadcasting.
    ~50x más rápido."""
    from scipy.stats import poisson
    h_adj = elo_home + HOME_ADV_ELO
    elo_p = 1 / (1 + 10 ** ((elo_away - h_adj) / 400))

    max_r = 25
    rng = np.arange(max_r + 1)
    pmf_h = poisson.pmf(rng, lam_h)   # shape (26,)
    pmf_a = poisson.pmf(rng, lam_a)   # shape (26,)
    grid = np.outer(pmf_h, pmf_a)     # shape (26, 26) P(home=h, away=a)
    # mask de triangular superior (h > a) y triangular inferior (a > h)
    home_wins_mask = np.triu(np.ones_like(grid, dtype=bool), k=1)  # h > a
    away_wins_mask = np.tril(np.ones_like(grid, dtype=bool), k=-1)  # a > h
    p_h = grid[home_wins_mask].sum()
    p_a = grid[away_wins_mask].sum()

    denom = p_h + p_a
    pois_p = 0.5 if denom < 1e-9 else p_h / denom

    blended = ELO_WEIGHT * elo_p + RUN_WEIGHT * pois_p
    return max(0.05, min(0.95, blended))


def _brier_score(probs, outcomes):
    return float(np.mean((np.array(probs) - np.array(outcomes)) ** 2))


def _log_loss(probs, outcomes, eps=1e-15):
    p = np.clip(np.array(probs), eps, 1 - eps)
    y = np.array(outcomes)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _calibration_buckets(probs, outcomes, n_buckets=10):
    probs = np.array(probs)
    outcomes = np.array(outcomes)
    bins = np.linspace(0, 1, n_buckets + 1)
    rows = []
    for i in range(n_buckets):
        lo, hi = bins[i], bins[i+1]
        mask = (probs >= lo) & (probs < hi if i < n_buckets - 1 else probs <= hi)
        n = int(mask.sum())
        if n == 0:
            rows.append((lo, hi, n, None, None))
            continue
        mean_pred = float(probs[mask].mean())
        mean_actual = float(outcomes[mask].mean())
        rows.append((lo, hi, n, mean_pred, mean_actual))
    return rows


def main():
    print("=" * 70)
    print("WALK-FORWARD CALIBRACIÓN MLB")
    print("=" * 70)
    print(f"Train: hasta {TRAIN_CUTOFF}")
    print(f"Test:  {TEST_START} → {TEST_END}")
    print()

    df_all = _load_games()
    print(f"Total juegos cargados: {len(df_all)}")
    print(f"Rango: {df_all['date'].min().date()} → {df_all['date'].max().date()}")

    cutoff = pd.Timestamp(TRAIN_CUTOFF)
    test_start = pd.Timestamp(TEST_START)
    test_end = pd.Timestamp(TEST_END)

    # Train: pre-cutoff
    train_df = df_all[df_all["date"] < cutoff]
    test_df = df_all[
        (df_all["date"] >= test_start) & (df_all["date"] < test_end)
    ].copy()

    print(f"\nTrain games: {len(train_df)}")
    print(f"Test games:  {len(test_df)}")

    # Elo inicial calculado con TRAIN
    print("\n→ Calculando Elo inicial con train set...", flush=True)
    elo = _compute_elo_up_to(df_all, cutoff)
    print(f"  Equipos con Elo: {len(elo)}", flush=True)

    # Pre-construir índice de juegos por equipo (cronológico)
    print("→ Construyendo índice cronológico por equipo...", flush=True)
    team_idx = _build_team_history_index(df_all)
    print(f"  Equipos indexados: {len(team_idx)}", flush=True)

    # ── Predicciones walk-forward ─────────────────────────────────
    # Para cada juego del test, predecimos con info ANTERIOR a su fecha,
    # luego actualizamos el Elo con el resultado real.
    probs_home_win = []
    outcomes = []         # 1 si gana home, 0 si gana away
    skipped = 0

    print("\n→ Walk-forward sobre test set...", flush=True)
    total_n = len(test_df)
    for i, (_, row) in enumerate(test_df.iterrows()):
        if i % 200 == 0:
            print(f"  …{i}/{total_n}", flush=True)
        h, a, md = row["home_team"], row["away_team"], row["date"]

        runs_h = _get_run_rates_fast(team_idx, h, md)
        runs_a = _get_run_rates_fast(team_idx, a, md)
        if runs_h["games"] < MIN_GAMES or runs_a["games"] < MIN_GAMES:
            skipped += 1
            # Actualizar Elo igual aunque saltemos predict
            _update_elo_one(elo, h, a, row["home_goals"], row["away_goals"])
            continue

        lam_h = max(1.0, min((runs_h["scored"] * runs_a["allowed"]) / MLB_AVG, 10.0))
        lam_a = max(1.0, min((runs_a["scored"] * runs_h["allowed"]) / MLB_AVG, 10.0))

        elo_h = elo.get(h, BASE_ELO)
        elo_a = elo.get(a, BASE_ELO)
        p_h = _blend_win_prob(elo_h, elo_a, lam_h, lam_a)

        probs_home_win.append(p_h)
        outcomes.append(1 if row["home_goals"] > row["away_goals"] else 0)

        # Update Elo después del juego
        _update_elo_one(elo, h, a, row["home_goals"], row["away_goals"])

    print(f"  Predicciones generadas: {len(probs_home_win)}")
    print(f"  Skipped (sin form):     {skipped}")

    # ── Métricas ───────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("RESULTADOS")
    print("=" * 70)

    brier = _brier_score(probs_home_win, outcomes)
    logl = _log_loss(probs_home_win, outcomes)
    base_rate = float(np.mean(outcomes))

    # Baseline 1: predict siempre prob_base = home_win_rate del train
    train_home_rate = float(
        (train_df["home_goals"] > train_df["away_goals"]).mean()
    )
    base_brier = _brier_score(
        [train_home_rate] * len(outcomes), outcomes
    )
    base_logl = _log_loss(
        [train_home_rate] * len(outcomes), outcomes
    )

    # Baseline 2: coin-flip 50/50
    flip_brier = _brier_score([0.5] * len(outcomes), outcomes)
    flip_logl = _log_loss([0.5] * len(outcomes), outcomes)

    accuracy = float(
        np.mean([1 if (p > 0.5) == (o == 1) else 0
                 for p, o in zip(probs_home_win, outcomes)])
    )

    print(f"\nTest games con predict: {len(probs_home_win)}")
    print(f"Home win rate test:     {base_rate*100:.1f}%")
    print(f"Home win rate train:    {train_home_rate*100:.1f}%")
    print()
    print(f"{'Métrica':<20} {'Modelo':>10} {'Baseline rate':>15} {'Coin flip':>12}")
    print("-" * 60)
    print(f"{'Brier score':<20} {brier:>10.4f} {base_brier:>15.4f} {flip_brier:>12.4f}")
    print(f"{'Log loss':<20} {logl:>10.4f} {base_logl:>15.4f} {flip_logl:>12.4f}")
    print(f"{'Accuracy':<20} {accuracy*100:>9.1f}%  -               -")
    print()

    # Veredicto numérico
    print("INTERPRETACIÓN:")
    if brier < base_brier * 0.97:
        print("  🟢 Modelo Brier < baseline-rate * 0.97 → aporta señal real")
    elif brier < base_brier:
        print("  🟡 Modelo apenas mejor que predecir tasa-base constante")
    else:
        print("  🔴 Modelo PEOR que predecir tasa-base — sin valor")

    if brier > 0.25:
        print("  🔴 Brier > 0.25 → peor que coin flip ponderado")
    elif brier > 0.24:
        print("  🟡 Brier 0.24-0.25 → marginal")
    else:
        print("  🟢 Brier < 0.24 → razonablemente calibrado")

    # ── Calibración por bucket ─────────────────────────────────────
    print("\nCURVA DE CALIBRACIÓN (prob predicha vs realidad):")
    print(f"{'Bucket':<14} {'N':>5} {'Pred avg':>10} {'Real avg':>10} {'Sesgo':>8}")
    print("-" * 50)
    for lo, hi, n, mp, ma in _calibration_buckets(probs_home_win, outcomes):
        if n == 0:
            print(f"{f'{lo:.1f}-{hi:.1f}':<14} {n:>5}    -          -        -")
        else:
            bias = ma - mp
            flag = "⚠️" if abs(bias) > 0.05 else ""
            print(f"{f'{lo:.1f}-{hi:.1f}':<14} {n:>5} {mp:>10.3f} {ma:>10.3f} {bias:>+7.3f}  {flag}")

    print("\n" + "=" * 70)
    print("NOTA: Esto sólo mide calibración del modelo, NO edge vs mercado.")
    print("Para saber si gana dinero, necesitás odds históricas MLB.")
    print("=" * 70)


def _update_elo_one(elo: dict, h: str, a: str, hg, ag):
    elo.setdefault(h, BASE_ELO)
    elo.setdefault(a, BASE_ELO)
    h_adj = elo[h] + HOME_ADV_ELO
    h_exp = 1 / (1 + 10 ** ((elo[a] - h_adj) / 400))
    if hg > ag:   h_act, a_act = 1.0, 0.0
    elif hg < ag: h_act, a_act = 0.0, 1.0
    else:         h_act, a_act = 0.5, 0.5
    mult = 1.0 + abs(hg - ag) * GOAL_DIFF_ELO
    k = K_ELO * mult
    elo[h] += k * (h_act - h_exp)
    elo[a] += k * (a_act - (1 - h_exp))


if __name__ == "__main__":
    main()
