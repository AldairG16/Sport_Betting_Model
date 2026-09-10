"""
scripts/evaluate_holdout.py
===========================
Evalúa el modelo sobre el HOLDOUT CONGELADO.

El calibration_monitor ajusta sus factores EXCLUYENDO los últimos
CALIBRATION_HOLDOUT_DAYS días (ver calibration_monitor.py). Este script
mide las métricas SOBRE esa ventana: si el modelo está bien calibrado,
el Brier / ROI del holdout debe acercarse al del período de fit.
Si el holdout es consistentemente peor, hay overfitting a la data de
calibración y los "edges" están inflados.

Ejecutar tras el weekly para tener el pulso de generalización real.
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.models.calibration_monitor import CALIBRATION_HOLDOUT_DAYS, CALIBRATION_WINDOW_DAYS

HALF_WIN = {"win": 1.0, "half_win": 0.5, "push": 0.0, "half_loss": 0.0, "loss": 0.0}


def evaluate_holdout() -> dict:
    try:
        return _evaluate_holdout_inner()
    except Exception as e:
        print(f"❌ evaluate_holdout: {e}")
        return {"status": "error", "error": str(e)}


def _evaluate_holdout_inner() -> dict:
    # Ventana de holdout: últimos N días
    df_hold = pd.read_sql(text(f"""
        SELECT market, league, probability, result, odds, stake
        FROM bets_history
        WHERE result IN ('win', 'loss', 'half_win', 'half_loss')
          AND probability IS NOT NULL AND probability > 0 AND probability < 1
          AND match_date >= NOW() - INTERVAL '{CALIBRATION_HOLDOUT_DAYS} days'
    """), engine)

    # Ventana de fit (la que usa la calibración): entre N y WINDOW días atrás
    df_fit = pd.read_sql(text(f"""
        SELECT market, league, probability, result, odds, stake
        FROM bets_history
        WHERE result IN ('win', 'loss', 'half_win', 'half_loss')
          AND probability IS NOT NULL AND probability > 0 AND probability < 1
          AND match_date >= NOW() - INTERVAL '{CALIBRATION_WINDOW_DAYS} days'
          AND match_date <  NOW() - INTERVAL '{CALIBRATION_HOLDOUT_DAYS} days'
    """), engine)

    if df_hold.empty:
        print(f"Sin bets resueltas en el holdout (últimos {CALIBRATION_HOLDOUT_DAYS}d).")
        return {"status": "no_data"}

    def _metrics(df: pd.DataFrame, label: str) -> dict:
        if df.empty:
            return {"label": label, "n": 0}
        outcome = df["result"].map(HALF_WIN).astype(float)
        brier = float(((df["probability"] - outcome) ** 2).mean())
        # ROI con profit fraccional para half_win/half_loss
        profit = df.apply(
            lambda r: (r["odds"] - 1) * r["stake"] * (1.0 if r["result"] == "win"
                       else 0.5 if r["result"] == "half_win"
                       else -1.0 if r["result"] == "loss"
                       else -0.5 if r["result"] == "half_loss" else 0.0),
            axis=1,
        )
        staked = float(df["stake"].sum())
        roi = float(profit.sum() / staked) if staked > 0 else 0.0
        return {
            "label": label,
            "n":     len(df),
            "brier": round(brier, 5),
            "roi":   round(roi, 4),
            "calibration_gap": round(float(df["probability"].mean() - outcome.mean()), 4),
        }

    m_fit  = _metrics(df_fit,  f"fit ({CALIBRATION_WINDOW_DAYS}-{CALIBRATION_HOLDOUT_DAYS}d atrás)")
    m_hold = _metrics(df_hold, f"holdout (últimos {CALIBRATION_HOLDOUT_DAYS}d)")

    print("\n🧊 HOLDOUT EVALUATION")
    for m in (m_fit, m_hold):
        if m.get("n"):
            print(f"   {m['label']:<38} n={m['n']:>4}  Brier={m['brier']:.4f}  "
                  f"ROI={m['roi']:+.1%}  gap_pred_vs_real={m['calibration_gap']:+.4f}")
        else:
            print(f"   {m['label']:<38} sin datos")

    if m_fit.get("n") and m_hold.get("n"):
        gap_brier = m_hold["brier"] - m_fit["brier"]
        verdict = "✅ generaliza bien" if gap_brier <= 0.01 else \
                  f"⚠️  Brier del holdout {gap_brier:+.4f} peor que el fit — posible overfitting"
        print(f"   {verdict}")

    return {"status": "ok", "fit": m_fit, "holdout": m_hold}


if __name__ == "__main__":
    evaluate_holdout()
