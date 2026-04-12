"""
src/models/walkforward_backtest.py
==================================
Walk-Forward Analysis del Sports Betting Model.

Simula la performance real del modelo cronologicamente:
  - Equity curve con drawdowns
  - Sharpe ratio (anualizado)
  - Rolling win rate y ROI
  - Simulacion de estrategias alternativas (filtros de mercado/liga/CLV)
  - Flat staking vs Kelly vs Half-Kelly comparativo

Uso:
  python -m src.models.walkforward_backtest
"""

import sys
import os
import numpy as np
import pandas as pd
from scipy import stats

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine


# ============================================================
# HELPERS
# ============================================================

def _sharpe_ratio(returns: pd.Series, periods_per_year: float = 365.0) -> float:
    """
    Sharpe ratio anualizado.
    returns = profit de cada apuesta como fraccion del bankroll.
    """
    if returns.std() == 0 or len(returns) < 10:
        return 0.0
    daily_sr = returns.mean() / returns.std()
    return round(daily_sr * np.sqrt(periods_per_year), 3)


def _max_drawdown_details(cum_profit: pd.Series) -> dict:
    """Max drawdown con fechas de pico, valle y recuperacion."""
    peak = cum_profit.cummax()
    dd = cum_profit - peak

    if dd.min() >= 0:
        return {"max_dd": 0, "peak_idx": None, "valley_idx": None, "recovered": True}

    valley_idx = dd.idxmin()
    peak_idx = cum_profit[:valley_idx].idxmax()

    # Buscar recuperacion
    post_valley = cum_profit[valley_idx:]
    recovered_mask = post_valley >= cum_profit[peak_idx]
    if recovered_mask.any():
        recovery_idx = recovered_mask.idxmax()
        recovered = True
    else:
        recovery_idx = None
        recovered = False

    return {
        "max_dd":      round(dd.min(), 2),
        "peak_idx":    peak_idx,
        "valley_idx":  valley_idx,
        "recovery_idx": recovery_idx,
        "recovered":   recovered,
    }


def _simulate_strategy(df: pd.DataFrame, name: str, mask: pd.Series = None,
                        flat_stake: float = 1.0) -> dict:
    """
    Simula una estrategia sobre un subset de bets.

    Args:
        df: DataFrame con todas las bets (cronologico)
        name: Nombre de la estrategia
        mask: Boolean mask para filtrar bets (None = todas)
        flat_stake: Stake fijo por apuesta (para comparacion justa)

    Returns:
        dict con metricas de la estrategia
    """
    sub = df[mask].copy() if mask is not None else df.copy()

    if len(sub) < 5:
        return {"name": name, "n": len(sub), "skip": True}

    # Profit con flat staking (1u por apuesta) para comparacion justa
    sub["flat_profit"] = sub.apply(
        lambda r: flat_stake * (r["odds"] - 1) if r["result"] == "win"
        else (-flat_stake if r["result"] == "loss" else 0.0),
        axis=1
    )

    # Profit con stake real (Kelly)
    sub["real_profit"] = sub.apply(
        lambda r: r["stake"] * (r["odds"] - 1) if r["result"] == "win"
        else (-r["stake"] if r["result"] == "loss" else 0.0),
        axis=1
    )

    n = len(sub)
    wins = (sub["result"] == "win").sum()
    losses = (sub["result"] == "loss").sum()
    wr = wins / n if n > 0 else 0

    flat_total = sub["flat_profit"].sum()
    flat_roi = flat_total / (n * flat_stake) if n > 0 else 0

    real_total = sub["real_profit"].sum()
    real_staked = sub["stake"].sum()
    real_roi = real_total / real_staked if real_staked > 0 else 0

    cum_flat = sub["flat_profit"].cumsum()
    dd_info = _max_drawdown_details(cum_flat)

    # Sharpe (usando flat profit como returns)
    sharpe = _sharpe_ratio(sub["flat_profit"])

    # Longest losing streak
    streak = 0
    max_streak = 0
    for r in sub["result"]:
        if r == "loss":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # t-test significancia
    if n >= 10:
        t_stat, p_two = stats.ttest_1samp(sub["flat_profit"], popmean=0)
        p_val = p_two / 2 if t_stat > 0 else 1.0
    else:
        t_stat, p_val = 0, 1.0

    return {
        "name":       name,
        "n":          n,
        "wins":       wins,
        "losses":     losses,
        "wr":         wr,
        "flat_profit": round(flat_total, 2),
        "flat_roi":   round(flat_roi * 100, 1),
        "real_profit": round(real_total, 2),
        "real_roi":   round(real_roi * 100, 1),
        "max_dd":     dd_info["max_dd"],
        "max_streak": max_streak,
        "sharpe":     sharpe,
        "p_value":    round(p_val, 4),
        "significant": p_val < 0.05,
        "skip":       False,
    }


# ============================================================
# MAIN WALK-FORWARD
# ============================================================

def run_walkforward(verbose: bool = True) -> dict:
    """
    Ejecuta walk-forward analysis completo.

    Analiza:
    1. Performance cronologica (equity curve)
    2. Estrategia actual vs alternativas
    3. Que filtros mejorarian el sistema
    """
    print("\n" + "=" * 60)
    print("  WALK-FORWARD BACKTEST")
    print("=" * 60)

    # ── Cargar datos ──────────────────────────────────────────
    df = pd.read_sql("""
        SELECT id, match, market, odds, closing_odds, clv, result,
               profit, edge, probability, stake, league, match_date
        FROM bets_history
        WHERE result NOT IN ('pending', 'unresolved')
        ORDER BY match_date ASC
    """, engine)

    if len(df) < 20:
        print(f"  Insuficientes bets ({len(df)}). Minimo 20 para analisis.")
        return {}

    print(f"\n  Periodo: {df['match_date'].min()} -> {df['match_date'].max()}")
    print(f"  Total bets resueltas: {len(df)}")
    print()

    # ── 1. EQUITY CURVE ──────────────────────────────────────
    df["flat_profit"] = df.apply(
        lambda r: 1.0 * (r["odds"] - 1) if r["result"] == "win"
        else (-1.0 if r["result"] == "loss" else 0.0),
        axis=1
    )
    df["cum_profit"] = df["flat_profit"].cumsum()

    print("  EQUITY CURVE (flat 1u/bet)")
    print("  " + "-" * 50)

    # Mostrar cada 20% del camino
    checkpoints = [int(len(df) * p) for p in [0.2, 0.4, 0.6, 0.8, 1.0]]
    for cp in checkpoints:
        if cp > 0:
            sub = df.iloc[:cp]
            cum = sub["flat_profit"].sum()
            wr = (sub["result"] == "win").mean() * 100
            print(f"  Bet #{cp:3d}: P&L={cum:+6.2f}u  WR={wr:.1f}%")
    print()

    # ── 2. DRAWDOWN ANALYSIS ─────────────────────────────────
    dd = _max_drawdown_details(df["cum_profit"])
    print(f"  Max Drawdown: {dd['max_dd']}u")
    if dd["peak_idx"] is not None:
        peak_date = df.loc[dd["peak_idx"], "match_date"]
        valley_date = df.loc[dd["valley_idx"], "match_date"]
        print(f"  Peak:   bet #{dd['peak_idx']} ({peak_date})")
        print(f"  Valley: bet #{dd['valley_idx']} ({valley_date})")
        if dd["recovered"]:
            rec_date = df.loc[dd["recovery_idx"], "match_date"]
            bets_to_recover = dd["recovery_idx"] - dd["valley_idx"]
            print(f"  Recovery: bet #{dd['recovery_idx']} ({rec_date}) - {bets_to_recover} bets")
        else:
            print(f"  Recovery: NOT YET RECOVERED")
    print()

    # ── 3. ROLLING METRICS ───────────────────────────────────
    window = min(50, len(df) // 3)
    if window >= 20:
        print(f"  ROLLING METRICS (ventana de {window} bets)")
        print("  " + "-" * 50)

        df["rolling_wr"] = df["result"].apply(lambda x: 1 if x == "win" else 0).rolling(window).mean()
        df["rolling_roi"] = df["flat_profit"].rolling(window).sum() / window

        last_wr = df["rolling_wr"].iloc[-1]
        last_roi = df["rolling_roi"].iloc[-1]
        best_wr = df["rolling_wr"].max()
        worst_wr = df["rolling_wr"].min()

        print(f"  Win Rate actual (ult {window}):  {last_wr*100:.1f}%")
        print(f"  Win Rate mejor:               {best_wr*100:.1f}%")
        print(f"  Win Rate peor:                {worst_wr*100:.1f}%")
        print(f"  ROI actual (ult {window}):      {last_roi*100:.1f}%")
        print()

    # ── 4. SIMULACION DE ESTRATEGIAS ─────────────────────────
    print("=" * 60)
    print("  COMPARATIVA DE ESTRATEGIAS")
    print("=" * 60)
    print()

    strategies = []

    # Baseline: todas las bets
    strategies.append(_simulate_strategy(df, "TODAS (baseline)"))

    # Sin away_win
    strategies.append(_simulate_strategy(
        df, "Sin away_win",
        mask=df["market"] != "away_win"
    ))

    # Solo home_win + over25
    strategies.append(_simulate_strategy(
        df, "Solo home_win + over25",
        mask=df["market"].isin(["home_win", "over25"])
    ))

    # Solo home_win + over25 + draw
    strategies.append(_simulate_strategy(
        df, "home_win + over25 + draw",
        mask=df["market"].isin(["home_win", "over25", "draw"])
    ))

    # Sin ligas problematicas
    bad_leagues = {
        "soccer_fifa_world_cup_qualifiers_europe",
        "soccer_uefa_europa_league",
        "soccer_netherlands_eredivisie",
        "soccer_conmebol_copa_libertadores",
        "soccer_uefa_champs_league",
    }
    strategies.append(_simulate_strategy(
        df, "Sin ligas perdedoras",
        mask=~df["league"].isin(bad_leagues)
    ))

    # Solo ligas top (Brasil, EFL, Argentina, Bundesliga, La Liga, Portugal)
    top_leagues = {
        "soccer_brazil_campeonato",
        "soccer_efl_champ",
        "soccer_argentina_primera_division",
        "soccer_germany_bundesliga",
        "soccer_spain_la_liga",
        "soccer_portugal_primeira_liga",
        "soccer_mexico_ligamx",
    }
    strategies.append(_simulate_strategy(
        df, "Solo ligas rentables",
        mask=df["league"].isin(top_leagues)
    ))

    # Edge >= 12%
    strategies.append(_simulate_strategy(
        df, "Edge >= 12%",
        mask=df["edge"] >= 0.12
    ))

    # Edge >= 15%
    strategies.append(_simulate_strategy(
        df, "Edge >= 15%",
        mask=df["edge"] >= 0.15
    ))

    # Odds 1.5 - 2.5
    strategies.append(_simulate_strategy(
        df, "Odds 1.5-2.5",
        mask=(df["odds"] >= 1.5) & (df["odds"] <= 2.5)
    ))

    # Odds 2.0 - 3.5
    strategies.append(_simulate_strategy(
        df, "Odds 2.0-3.5",
        mask=(df["odds"] >= 2.0) & (df["odds"] <= 3.5)
    ))

    # CLV > 0 (solo bets donde batimos cierre)
    if df["clv"].notna().sum() > 20:
        strategies.append(_simulate_strategy(
            df, "Solo CLV > 0",
            mask=df["clv"] > 0
        ))
        strategies.append(_simulate_strategy(
            df, "Solo CLV >= 0",
            mask=df["clv"] >= 0
        ))

    # COMBINADA: sin away_win + sin ligas malas + edge >= 12%
    combo_mask = (
        (df["market"] != "away_win") &
        (~df["league"].isin(bad_leagues)) &
        (df["edge"] >= 0.12)
    )
    strategies.append(_simulate_strategy(df, "COMBO OPTIMA", mask=combo_mask))

    # Sin away_win + sin ligas malas
    safe_mask = (
        (df["market"] != "away_win") &
        (~df["league"].isin(bad_leagues))
    )
    strategies.append(_simulate_strategy(df, "Safe (no away, no bad lg)", mask=safe_mask))

    # ── Imprimir tabla comparativa ────────────────────────────
    print(f"  {'Estrategia':<30} {'N':>4} {'WR':>6} {'P&L':>7} {'ROI':>7} {'MaxDD':>6} {'Sharpe':>7} {'Sig?':>5}")
    print("  " + "-" * 85)

    for s in strategies:
        if s.get("skip"):
            print(f"  {s['name']:<30} {s['n']:>4}  -- datos insuficientes --")
            continue

        sig_mark = "YES" if s["significant"] else "no"
        print(
            f"  {s['name']:<30} {s['n']:>4} {s['wr']*100:>5.1f}% "
            f"{s['flat_profit']:>+6.1f}u {s['flat_roi']:>+6.1f}% "
            f"{s['max_dd']:>5.1f}u {s['sharpe']:>6.3f} {sig_mark:>5}"
        )

    print()

    # ── 5. MEJOR ESTRATEGIA ──────────────────────────────────
    valid = [s for s in strategies if not s.get("skip") and s["n"] >= 20]
    if valid:
        # Rankear por Sharpe (mejor risk-adjusted return)
        best = max(valid, key=lambda s: s["sharpe"])
        print(f"  MEJOR ESTRATEGIA (por Sharpe): {best['name']}")
        print(f"    Sharpe={best['sharpe']}  ROI={best['flat_roi']}%  WR={best['wr']*100:.1f}%  N={best['n']}")
        print()

        # Rankear por ROI absoluto
        best_roi = max(valid, key=lambda s: s["flat_roi"])
        print(f"  MEJOR ESTRATEGIA (por ROI): {best_roi['name']}")
        print(f"    ROI={best_roi['flat_roi']}%  Sharpe={best_roi['sharpe']}  P&L={best_roi['flat_profit']}u  N={best_roi['n']}")
        print()

    # ── 6. RECOMENDACIONES ────────────────────────────────────
    print("=" * 60)
    print("  RECOMENDACIONES BASADAS EN DATA")
    print("=" * 60)

    baseline = strategies[0]

    # Away win analysis
    away_strat = next((s for s in strategies if s["name"] == "Sin away_win"), None)
    if away_strat and not away_strat.get("skip"):
        if away_strat["flat_roi"] > baseline["flat_roi"] + 2:
            print(f"  [1] ELIMINAR away_win: ROI sube de {baseline['flat_roi']}% a {away_strat['flat_roi']}%")
        else:
            print(f"  [1] away_win: impacto marginal en ROI ({baseline['flat_roi']}% -> {away_strat['flat_roi']}%)")

    # League analysis
    safe_strat = next((s for s in strategies if s["name"] == "Sin ligas perdedoras"), None)
    if safe_strat and not safe_strat.get("skip"):
        if safe_strat["flat_roi"] > baseline["flat_roi"] + 2:
            print(f"  [2] ELIMINAR ligas perdedoras: ROI sube a {safe_strat['flat_roi']}%")

    # Edge threshold
    e12_strat = next((s for s in strategies if s["name"] == "Edge >= 12%"), None)
    if e12_strat and not e12_strat.get("skip"):
        print(f"  [3] Edge >= 12%: ROI={e12_strat['flat_roi']}% (vs {baseline['flat_roi']}% baseline)")

    # Combo
    combo_strat = next((s for s in strategies if s["name"] == "COMBO OPTIMA"), None)
    if combo_strat and not combo_strat.get("skip"):
        improvement = combo_strat["flat_roi"] - baseline["flat_roi"]
        print(f"  [4] COMBO (no away + no bad leagues + edge>=12%): ROI={combo_strat['flat_roi']}% (+{improvement:.1f}pp)")

    print()

    return {
        "baseline": baseline,
        "strategies": strategies,
        "best_sharpe": best["name"] if valid else None,
        "n_bets": len(df),
    }


if __name__ == "__main__":
    run_walkforward(verbose=True)
