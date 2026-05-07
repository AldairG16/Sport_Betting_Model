"""
scripts/validate_by_league_calibration.py
=========================================
Backtest A/B de la calibración por liga (Sprint 1 — Mejora #2).

Compara `apply_calibration` con vs sin pasar `league`. Demuestra cuánto
mejora el Brier y reduce el bias residual al usar factor por liga.

Read-only — NO toca DB ni archivos.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.models.calibration_monitor import (
    apply_calibration,
    load_calibration_factors,
)


def main():
    print("=" * 70)
    print("VALIDACION A/B — Calibración por liga (Mejora #2)")
    print("=" * 70)

    factors = load_calibration_factors()
    bl = factors.get("by_league", {})
    print(f"\nLigas con calibración propia: {len(bl)}")

    # Cargar bets resueltas — usamos la misma ventana de 90 días
    df = pd.read_sql("""
        SELECT match, market, league, probability, result, odds
        FROM bets_history
        WHERE result IN ('win', 'loss')
          AND probability IS NOT NULL
          AND probability BETWEEN 0.01 AND 0.99
          AND match_date >= NOW() - INTERVAL '90 days'
    """, engine)
    df["outcome"] = (df["result"] == "win").astype(int)
    df["pnl_flat"] = df.apply(
        lambda r: (r["odds"] - 1) if r["result"] == "win" else -1, axis=1
    )
    print(f"Bets resueltas: {len(df)}")

    # Aplicar calibración A: solo global (league=None)
    # Aplicar calibración B: con liga (league=row.league)
    df["prob_A_global"] = df.apply(
        lambda r: apply_calibration(r["probability"], r["market"], league=None),
        axis=1,
    )
    df["prob_B_byleague"] = df.apply(
        lambda r: apply_calibration(r["probability"], r["market"], league=r["league"]),
        axis=1,
    )

    # Brier
    def brier(p, y): return float(np.mean((p - y) ** 2))

    print("\n" + "=" * 70)
    print(f"{'Mercado':<14} {'N':>5} {'Brier A':>10} {'Brier B':>10} {'Δ':>10}  Bias_A    Bias_B")
    print("-" * 90)

    for market in ["home_win", "away_win", "draw", "over25", "under25",
                   "btts", "btts_no"]:
        m_df = df[df["market"] == market]
        if len(m_df) < 10:
            continue
        ba = brier(m_df["prob_A_global"], m_df["outcome"])
        bb = brier(m_df["prob_B_byleague"], m_df["outcome"])
        delta = bb - ba
        bias_a = float(m_df["prob_A_global"].mean() - m_df["outcome"].mean())
        bias_b = float(m_df["prob_B_byleague"].mean() - m_df["outcome"].mean())
        marker = "✅" if delta < -0.001 else ("≈" if abs(delta) < 0.001 else "❌")
        bias_marker = "✅" if abs(bias_b) < abs(bias_a) - 0.005 else "≈"
        print(f"{market:<14} {len(m_df):>5} {ba:>10.5f} {bb:>10.5f} {delta:>+10.5f} {marker} "
              f"{bias_a:>+7.4f}  {bias_b:>+7.4f} {bias_marker}")

    # Foco específico: away_win por liga
    print("\n" + "=" * 70)
    print("DETALLE — AWAY_WIN por liga (las que tienen mayor bias)")
    print("=" * 70)
    print(f"{'Liga':<40} {'N':>4} {'Real%':>7} "
          f"{'A pred':>8} {'B pred':>8} {'Bias_A':>8} {'Bias_B':>8}")
    print("-" * 90)

    aw = df[df["market"] == "away_win"]
    by_league_stats = (
        aw.groupby("league")
        .agg(
            n=("match", "count"),
            real=("outcome", "mean"),
            pred_A=("prob_A_global", "mean"),
            pred_B=("prob_B_byleague", "mean"),
        )
        .reset_index()
        .sort_values("n", ascending=False)
    )
    for _, r in by_league_stats.iterrows():
        if r["n"] < 3:
            continue
        bias_a = r["pred_A"] - r["real"]
        bias_b = r["pred_B"] - r["real"]
        marker = "✅" if abs(bias_b) < abs(bias_a) - 0.01 else (
                 "❌" if abs(bias_b) > abs(bias_a) + 0.01 else "≈")
        print(f"{r['league']:<40} {int(r['n']):>4} {r['real']*100:>6.1f}% "
              f"{r['pred_A']*100:>7.1f}% {r['pred_B']*100:>7.1f}% "
              f"{bias_a*100:>+7.1f}% {bias_b*100:>+7.1f}% {marker}")

    # ROI hipotético (edge >= 5%)
    print("\n" + "=" * 70)
    print("ROI HIPOTÉTICO con threshold edge >= 5% (flat-stake 1u)")
    print("=" * 70)

    for label, prob_col in [("A — solo global", "prob_A_global"),
                             ("B — by_league",  "prob_B_byleague")]:
        # edge = prob_calibrada * odds - 1
        df[f"edge_{prob_col}"] = df[prob_col] * df["odds"] - 1
        picks = df[df[f"edge_{prob_col}"] >= 0.05]
        if len(picks) == 0:
            print(f"  {label:<25}  Sin bets con edge >= 5%")
            continue
        roi = picks["pnl_flat"].mean() * 100
        wr = picks["outcome"].mean() * 100
        print(f"  {label:<25}  N={len(picks):>3}  WR={wr:>5.1f}%  "
              f"ROI={roi:>+7.2f}%  PnL={picks['pnl_flat'].sum():>+7.2f}u")


if __name__ == "__main__":
    main()
