"""
Backtest Engine — Análisis Estadístico Completo
================================================
Evalúa el rendimiento real del modelo con métricas profesionales.

Métricas incluidas:
  - ROI total y por categoría (liga, mercado, rango de edge, rango de odds)
  - Drawdown máximo y promedio
  - Racha máxima de pérdidas
  - Significancia estadística (t-test vs hipótesis nula ROI=0)
  - CLV análisis (¿apostamos a mejores odds que el cierre?)
  - Curva de bankroll

Nota: Requiere bets resueltas en bets_history.
      Mientras más bets resueltas, más significativo el análisis.
      Mínimo recomendado: 100 bets para resultados estadísticamente útiles.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd
from scipy import stats
from config.database import engine


# =========================
# HELPERS
# =========================

def _profit(row) -> float:
    if row["result"] == 1:
        return round((row["odds"] - 1) * row["stake"], 4)
    elif row["result"] == 0:
        return round(-row["stake"], 4)
    return 0.0


def _roi(profit_series, stake_series) -> float:
    total_staked = stake_series.sum()
    return profit_series.sum() / total_staked if total_staked > 0 else 0.0


def _max_drawdown(cumulative_profit: pd.Series) -> float:
    """Máxima caída desde un pico hasta el siguiente valle."""
    peak = cumulative_profit.cummax()
    drawdown = cumulative_profit - peak
    return round(drawdown.min(), 4)


def _significance(profits: pd.Series) -> dict:
    """
    t-test de una cola: ¿es el ROI significativamente mayor que 0?

    H0: E[profit] = 0  (modelo sin edge)
    H1: E[profit] > 0  (modelo con edge positivo)
    """
    if len(profits) < 10:
        return {"t_stat": None, "p_value": None, "significant": None, "n": len(profits)}

    t_stat, p_two_tailed = stats.ttest_1samp(profits, popmean=0)
    p_one_tailed = p_two_tailed / 2 if t_stat > 0 else 1.0

    return {
        "t_stat":      round(t_stat, 3),
        "p_value":     round(p_one_tailed, 4),
        "significant": p_one_tailed < 0.05,   # 95% confianza
        "n":           len(profits)
    }


def _edge_bucket(edge: float) -> str:
    if edge < 0.05:   return "0-5%"
    if edge < 0.08:   return "5-8%"
    if edge < 0.12:   return "8-12%"
    return "12%+"


def _odds_bucket(odds: float) -> str:
    if odds < 1.7:    return "1.5-1.7"
    if odds < 2.0:    return "1.7-2.0"
    if odds < 2.5:    return "2.0-2.5"
    if odds < 3.5:    return "2.5-3.5"
    return "3.5-6.0"


# =========================
# MAIN BACKTEST
# =========================

def run_backtest(min_bets: int = 5) -> dict:
    """
    Ejecuta el backtest completo sobre bets_history.

    Returns:
        dict con todas las métricas, o mensaje de insuficiente data.
    """
    print("\n📊 BACKTEST ENGINE — ANÁLISIS COMPLETO\n")

    # Cargar bets resueltas
    try:
        df = pd.read_sql("""
            SELECT *
            FROM bets_history
            WHERE result IS NOT NULL
              AND result::text NOT IN ('pending', 'null', '')
            ORDER BY match_date
        """, engine)
    except Exception as e:
        print(f"❌ Error accediendo a bets_history: {e}")
        return {}

    if df.empty:
        print("⚠️  Sin bets resueltas todavía.")
        print("    Las bets se resuelven automáticamente después de cada partido.")
        return {}

    if len(df) < min_bets:
        print(f"⚠️  Solo {len(df)} bets resueltas (mínimo recomendado: {min_bets})")
        print("    Mostrando estadísticas preliminares...\n")

    # =========================
    # PREPARACIÓN
    # =========================

    df["profit"]      = df.apply(_profit, axis=1)
    df["edge_bucket"] = df["edge"].apply(_edge_bucket)
    df["odds_bucket"] = df["odds"].apply(_odds_bucket)
    df["cum_profit"]  = df["profit"].cumsum()

    total_bets   = len(df)
    total_profit = df["profit"].sum()
    total_staked = df["stake"].sum()
    roi          = _roi(df["profit"], df["stake"])
    win_rate     = (df["result"] == 1).mean()
    avg_odds     = df["odds"].mean()
    avg_edge     = df["edge"].mean()
    max_dd       = _max_drawdown(df["cum_profit"])

    # Racha de pérdidas
    streak = 0
    max_streak = 0
    for r in df["result"]:
        if r == 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Significancia
    sig = _significance(df["profit"])

    # =========================
    # RESUMEN GENERAL
    # =========================

    print("=" * 55)
    print("  RESUMEN GENERAL")
    print("=" * 55)
    print(f"  Bets totales:       {total_bets}")
    print(f"  Bets ganadas:       {(df['result']==1).sum()}  ({win_rate*100:.1f}%)")
    print(f"  Total apostado:     ${total_staked:.2f}")
    print(f"  Profit total:       ${total_profit:+.2f}")
    print(f"  ROI:                {roi*100:+.2f}%")
    print(f"  Odds promedio:      {avg_odds:.2f}")
    print(f"  Edge promedio:      {avg_edge*100:.1f}%")
    print(f"  Max drawdown:       ${max_dd:.2f}")
    print(f"  Racha max perdidas: {max_streak}")

    if sig["p_value"] is not None:
        sig_str = "SI ✅" if sig["significant"] else "NO ❌"
        print(f"  Significativo 95%:  {sig_str}  (p={sig['p_value']}, n={sig['n']})")

    print()

    # =========================
    # ROI POR MERCADO
    # =========================

    print("=" * 55)
    print("  ROI POR MERCADO")
    print("=" * 55)
    _print_breakdown(df, "market")

    # =========================
    # ROI POR LIGA
    # =========================

    if "league" in df.columns:
        print("=" * 55)
        print("  ROI POR LIGA")
        print("=" * 55)
        _print_breakdown(df, "league")

    # =========================
    # ROI POR RANGO DE EDGE
    # =========================

    print("=" * 55)
    print("  ROI POR RANGO DE EDGE")
    print("=" * 55)
    _print_breakdown(df, "edge_bucket")

    # =========================
    # ROI POR RANGO DE ODDS
    # =========================

    print("=" * 55)
    print("  ROI POR RANGO DE ODDS")
    print("=" * 55)
    _print_breakdown(df, "odds_bucket")

    # =========================
    # CLV ANÁLISIS
    # =========================

    if "clv" in df.columns:
        clv_data = df["clv"].dropna()
        if len(clv_data) >= 5:
            print("=" * 55)
            print("  CLOSING LINE VALUE (CLV)")
            print("=" * 55)
            print(f"  CLV promedio:    {clv_data.mean():+.4f}")
            print(f"  CLV positivo:    {(clv_data > 0).mean()*100:.1f}% de bets")
            print(f"  CLV > 2%:        {(clv_data > 0.02).mean()*100:.1f}% de bets")
            print()
            print("  Interpretación:")
            if clv_data.mean() > 0.01:
                print("  ✅ CLV positivo: apostamos ANTES de que el mercado ajuste.")
                print("     Señal fuerte de edge real a largo plazo.")
            elif clv_data.mean() > 0:
                print("  ⚠️  CLV levemente positivo: modelo funciona, continuar.")
            else:
                print("  ❌ CLV negativo: el mercado sabe más. Revisar timing y edge mínimo.")
            print()

    # =========================
    # RECOMENDACIONES
    # =========================

    print("=" * 55)
    print("  RECOMENDACIONES")
    print("=" * 55)

    if roi > 0.05:
        print("  ✅ ROI > 5%: modelo rentable, mantener estrategia.")
    elif roi > 0:
        print("  ⚠️  ROI positivo pero bajo. Aumentar edge mínimo a 7%.")
    else:
        print("  ❌ ROI negativo. Revisar filtros o esperar más muestras.")

    if avg_edge < 0.06:
        print("  ⚠️  Edge promedio < 6%. Considera subir umbral mínimo.")

    if max_streak > 8:
        print(f"  ⚠️  Racha de {max_streak} pérdidas consecutivas. Revisa bankroll.")

    if sig["n"] < 100:
        print(f"  ℹ️  {sig['n']} bets resueltas. Necesitas ~100 para significancia robusta.")

    print()

    return {
        "total_bets":   total_bets,
        "roi":          roi,
        "total_profit": total_profit,
        "win_rate":     win_rate,
        "max_drawdown": max_dd,
        "max_streak":   max_streak,
        "significance": sig
    }


# =========================
# HELPER BREAKDOWN
# =========================

def _print_breakdown(df: pd.DataFrame, col: str):
    grouped = df.groupby(col).apply(
        lambda g: pd.Series({
            "bets":   len(g),
            "wins":   (g["result"] == 1).sum(),
            "profit": g["profit"].sum(),
            "staked": g["stake"].sum(),
            "roi":    _roi(g["profit"], g["stake"]) * 100
        })
    ).reset_index()

    grouped = grouped.sort_values("roi", ascending=False)

    for _, row in grouped.iterrows():
        bar = "▓" * int(max(0, min(row["roi"] / 5, 10)))
        print(f"  {str(row[col]):<30} {row['bets']:>3} bets  ROI: {row['roi']:>+7.1f}%  {bar}")
    print()


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    run_backtest()
