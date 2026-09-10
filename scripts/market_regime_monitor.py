"""
scripts/market_regime_monitor.py
================================
Monitor de RÉGIMEN DEL MERCADO.

El drift detector compara el rendimiento del MODELO contra su histórico.
Este monitor vigila algo previo: si el MERCADO mismo cambió de forma
(overround promedio, panel de bookmakers, volatilidad de consenso).
Si The Odds API cambió de formato, un bookmaker salió del panel o las
líneas se volvieron más ruidosas, los edges del modelo se distorsionan
SILENCIOSAMENTE — este check lo detecta antes de contaminar apuestas.

Comparación: ventana reciente (7 días) vs baseline (8-37 días atrás).

Umbrales de alerta:
  - vig promedio sube >= 1.5 puntos porcentuales (margen más caro)
  - bookmaker_count mediano cae >= 30% (panel diezmado)
  - spread_pct mediano sube >= 50% relativo (consenso roto)

Ejecutar semanalmente (lo llama el orchestrator --mode weekly).
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine


def run_market_regime_monitor(verbose: bool = True) -> dict:
    try:
        # upcoming_matches retiene las últimas odds vistas por partido
        df = pd.read_sql(text("""
            SELECT sport_key,
                   home_odds, draw_odds, away_odds,
                   bookmaker_count,
                   h2h_spread_pct,
                   updated_at
            FROM upcoming_matches
            WHERE home_odds IS NOT NULL AND away_odds IS NOT NULL
              AND home_odds > 1 AND away_odds > 1
        """), engine)
    except Exception as e:
        if verbose:
            print(f"❌ market_regime_monitor: {e}")
        return {"status": "error", "error": str(e)}

    if df.empty:
        if verbose:
            print("market_regime_monitor: sin odds en upcoming_matches.")
        return {"status": "no_data"}

    # Vig (overround) 1x2: 1/home + 1/draw + 1/away − 1
    df["vig"] = (
        1.0 / df["home_odds"]
        + pd.to_numeric(df["draw_odds"], errors="coerce").rpow(-1).fillna(0)
        + 1.0 / df["away_odds"]
        - 1.0
    )
    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True, errors="coerce")
    now = df["updated_at"].max()

    recent = df[df["updated_at"] > now - pd.Timedelta(days=7)]
    baseline = df[
        (df["updated_at"] <= now - pd.Timedelta(days=7))
        & (df["updated_at"] > now - pd.Timedelta(days=37))
    ]

    if recent.empty or baseline.empty:
        if verbose:
            print("market_regime_monitor: ventanas insuficientes para comparar.")
        return {"status": "insufficient_windows"}

    def _stats(d: pd.DataFrame) -> dict:
        return {
            "vig_mean":      float(d["vig"].mean()),
            "bk_median":     float(d["bookmaker_count"].median()),
            "spread_median": float(d["h2h_spread_pct"].median()),
            "n":             len(d),
        }

    rs, bs = _stats(recent), _stats(baseline)
    alerts = []

    if (rs["vig_mean"] - bs["vig_mean"]) >= 0.015:
        alerts.append(
            f"Vig promedio subió {bs['vig_mean']*100:.1f}% → {rs['vig_mean']*100:.1f}% "
            f"(+{(rs['vig_mean']-bs['vig_mean'])*100:.1f}pp) — márgenes más caros, edges más chicos"
        )
    if bs["bk_median"] > 0 and rs["bk_median"] < bs["bk_median"] * 0.70:
        alerts.append(
            f"Bookmakers medianos cayeron {bs['bk_median']:.0f} → {rs['bk_median']:.0f} "
            f"(-{1 - rs['bk_median']/bs['bk_median']:.0%}) — consenso debilitado"
        )
    if bs["spread_median"] > 0 and rs["spread_median"] > bs["spread_median"] * 1.50:
        alerts.append(
            f"Spread de consenso subió {bs['spread_median']:.1f}% → {rs['spread_median']:.1f}% "
            f"(+{rs['spread_median']/bs['spread_median'] - 1:.0%}) — mercado dividido"
        )

    if verbose:
        print("\n🧭 MARKET REGIME MONITOR (7d vs 8-37d atrás)")
        print(f"   vig promedio:   {bs['vig_mean']*100:5.1f}% → {rs['vig_mean']*100:5.1f}%")
        print(f"   books medianos: {bs['bk_median']:5.0f} → {rs['bk_median']:5.0f}")
        print(f"   spread mediano: {bs['spread_median']:5.1f}% → {rs['spread_median']:5.1f}%")
        if alerts:
            for a in alerts:
                print(f"   🚨 {a}")
        else:
            print("   ✅ Régimen del mercado estable")

    return {
        "status": "ok",
        "recent": rs,
        "baseline": bs,
        "alerts": alerts,
    }


if __name__ == "__main__":
    run_market_regime_monitor()
