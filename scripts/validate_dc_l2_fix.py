"""
scripts/validate_dc_l2_fix.py
=============================
Backtest A/B del fix de regularización L2 sobre Dixon-Coles
(Sprint 1 — item #1).

Compara `data/dc_params.json` (post-fix con sigmoide+prior) contra
`data/dc_params_pre_l2_fix.json` (pre-fix con rho=0.0).

Métricas:
  - Brier score sobre BTTS_yes y over25 en últimos N partidos resueltos
  - Calibración: prob predicha vs frecuencia real
  - Mean Absolute Error de probabilidades

Read-only: NO toca DB, NO toca dc_params.json, solo lee.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.models.poisson_markets import totals_and_btts


DC_OLD = Path("data/dc_params_pre_l2_fix.json")
DC_NEW = Path("data/dc_params.json")
N_DAYS = 365   # últimos 12 meses de matches resueltos para A/B


def _load(path: Path) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _lambdas_from_params(params: dict, home: str, away: str) -> tuple | None:
    """Réplica de get_dc_lambdas pero sobre un dict cargado a mano."""
    teams = params.get("teams", {})
    home_p = teams.get(home) or teams.get(home.lower()) or {
        k: v for k, v in teams.items() if k.lower() == home.lower()
    }.get(home.lower())
    away_p = teams.get(away) or teams.get(away.lower()) or {
        k: v for k, v in teams.items() if k.lower() == away.lower()
    }.get(away.lower())
    if home_p is None or away_p is None:
        return None
    home_adv = params.get("home_adv", 0.25)
    lam_h = float(np.exp(home_p["attack"] + away_p["defense"] + home_adv))
    lam_a = float(np.exp(away_p["attack"] + home_p["defense"]))
    return lam_h, lam_a


def main():
    print("=" * 70)
    print("VALIDACION A/B — Dixon-Coles L2 fix (Sprint 1 #1)")
    print("=" * 70)

    if not DC_OLD.exists():
        print(f"ERROR: {DC_OLD} no existe. Hace falta el backup pre-fix.")
        return

    old_params = _load(DC_OLD)
    new_params = _load(DC_NEW)

    print(f"\nOLD: rho={old_params.get('rho'):>7.4f}  home_adv={old_params.get('home_adv'):.4f}  fitted_at={old_params.get('fitted_at', '')[:19]}")
    print(f"NEW: rho={new_params.get('rho'):>7.4f}  home_adv={new_params.get('home_adv'):.4f}  fitted_at={new_params.get('fitted_at', '')[:19]}")

    # ── Cargar matches resueltos recientes ──────────────────────────────
    print(f"\nCargando matches resueltos últimos {N_DAYS} días...")
    df = pd.read_sql(f"""
        SELECT home_team, away_team, home_goals, away_goals, date
        FROM matches
        WHERE home_goals IS NOT NULL
          AND away_goals IS NOT NULL
          AND date >= NOW() - INTERVAL '{N_DAYS} days'
          AND date <= NOW()
          AND (league IS NULL OR league LIKE 'soccer%%' OR league LIKE 'fifa%%' OR league LIKE 'uefa%%')
    """, engine)
    print(f"  Matches: {len(df):,}")

    # ── Realidad: btts_yes y over25 ─────────────────────────────────────
    df["btts_real"]   = ((df["home_goals"] >= 1) & (df["away_goals"] >= 1)).astype(int)
    df["over25_real"] = ((df["home_goals"] + df["away_goals"]) > 2).astype(int)

    # ── Predicciones OLD vs NEW ─────────────────────────────────────────
    rho_old = old_params.get("rho", 0.0)
    rho_new = new_params.get("rho", 0.0)

    # Gating consistente con pipeline: si |rho| < 0.02, no aplicar DC tau
    use_rho_old = rho_old if abs(rho_old) >= 0.02 else None
    use_rho_new = rho_new if abs(rho_new) >= 0.02 else None

    print(f"\nGating de DC tau correction:")
    print(f"  OLD: |rho|={abs(rho_old):.4f}  →  {'ACTIVA' if use_rho_old else 'POISSON CRUDA'}")
    print(f"  NEW: |rho|={abs(rho_new):.4f}  →  {'ACTIVA' if use_rho_new else 'POISSON CRUDA'}")

    rows = []
    skipped = 0
    for _, row in df.iterrows():
        lh = _lambdas_from_params(old_params, row["home_team"], row["away_team"])
        ln = _lambdas_from_params(new_params, row["home_team"], row["away_team"])
        if lh is None or ln is None:
            skipped += 1
            continue

        old_pred = totals_and_btts(lh[0], lh[1], rho=use_rho_old)
        new_pred = totals_and_btts(ln[0], ln[1], rho=use_rho_new)

        rows.append({
            "btts_real":      row["btts_real"],
            "over25_real":    row["over25_real"],
            "btts_old":       old_pred["btts_yes"],
            "btts_new":       new_pred["btts_yes"],
            "over25_old":     old_pred["over25"],
            "over25_new":     new_pred["over25"],
            "lh_old":         lh[0],
            "la_old":         lh[1],
            "lh_new":         ln[0],
            "la_new":         ln[1],
        })

    p = pd.DataFrame(rows)
    n_used = len(p)
    print(f"  Equipos no encontrados: {skipped} matches saltados (de {len(df):,})")
    print(f"  Matches usados en A/B:  {n_used:,}")

    if n_used < 100:
        print("\nMUESTRA INSUFICIENTE — abortando")
        return

    # ── Brier Score ─────────────────────────────────────────────────────
    def _brier(pred, real):
        return float(np.mean((pred - real) ** 2))

    brier_btts_old = _brier(p["btts_old"], p["btts_real"])
    brier_btts_new = _brier(p["btts_new"], p["btts_real"])
    brier_o25_old  = _brier(p["over25_old"], p["over25_real"])
    brier_o25_new  = _brier(p["over25_new"], p["over25_real"])

    # ── Bias (pred - real) ──────────────────────────────────────────────
    bias_btts_old = float(p["btts_old"].mean() - p["btts_real"].mean())
    bias_btts_new = float(p["btts_new"].mean() - p["btts_real"].mean())
    bias_o25_old  = float(p["over25_old"].mean() - p["over25_real"].mean())
    bias_o25_new  = float(p["over25_new"].mean() - p["over25_real"].mean())

    print("\n" + "=" * 70)
    print("RESULTADOS — BRIER SCORE (menor = mejor)")
    print("=" * 70)
    print(f"{'Mercado':<12} {'OLD':>10} {'NEW':>10} {'Δ':>10} {'%Δ':>8}")
    print("-" * 70)
    for label, o, n in [
        ("BTTS",   brier_btts_old, brier_btts_new),
        ("Over2.5", brier_o25_old, brier_o25_new),
    ]:
        delta = n - o
        pct   = 100 * delta / o if o > 0 else 0
        marker = "✅" if delta < 0 else ("❌" if delta > 0.001 else "≈")
        print(f"{label:<12} {o:>10.5f} {n:>10.5f} {delta:>+10.5f} {pct:>+7.2f}% {marker}")

    print("\n" + "=" * 70)
    print("RESULTADOS — BIAS (pred − real, cerca de 0 = bien calibrado)")
    print("=" * 70)
    print(f"{'Mercado':<12} {'OLD':>10} {'NEW':>10} {'|Δ| OLD':>12} {'|Δ| NEW':>12}")
    print("-" * 70)
    for label, o, n in [
        ("BTTS",   bias_btts_old, bias_btts_new),
        ("Over2.5", bias_o25_old, bias_o25_new),
    ]:
        marker = "✅" if abs(n) < abs(o) else ("❌" if abs(n) > abs(o) + 0.005 else "≈")
        print(f"{label:<12} {o:>+10.4f} {n:>+10.4f} {abs(o):>12.4f} {abs(n):>12.4f} {marker}")

    # ── Calibración por bucket de probabilidad ──────────────────────────
    print("\n" + "=" * 70)
    print("CALIBRACION POR BUCKET — BTTS (NEW)")
    print("=" * 70)
    print(f"{'Rango':<15} {'N':>6} {'pred avg':>10} {'real avg':>10} {'gap':>8}")
    print("-" * 70)
    for lo, hi in [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.0)]:
        mask = (p["btts_new"] >= lo) & (p["btts_new"] < hi)
        if mask.sum() < 10:
            continue
        pred = p.loc[mask, "btts_new"].mean()
        real = p.loc[mask, "btts_real"].mean()
        gap  = pred - real
        marker = "✅" if abs(gap) < 0.05 else ("⚠️ " if abs(gap) < 0.10 else "❌")
        print(f"[{lo:.2f},{hi:.2f}) {mask.sum():>6} {pred:>10.3f} {real:>10.3f} {gap:>+7.3f} {marker}")

    # ── Diff de lambdas (el rho diferente cambia la suma) ───────────────
    diff_lh = (p["lh_new"] - p["lh_old"]).abs().mean()
    diff_la = (p["la_new"] - p["la_old"]).abs().mean()
    print(f"\nLambdas: |Δlh|_avg={diff_lh:.4f}  |Δla|_avg={diff_la:.4f}")
    print(f"  (no debe ser enorme — los teams cambian poco, solo rho cambió)")


if __name__ == "__main__":
    main()
