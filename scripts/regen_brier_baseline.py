"""
scripts/regen_brier_baseline.py
===============================
Regenera `config/brier_baseline.json` con los Briers actuales del modelo.

Cuando lo uso:
  • Después de un cambio que mejora el Brier (validado con backtest A/B).
  • Después de un drift legítimo aceptado (ej. cambio de temporada).

Cuando NO lo uso:
  • Por defecto, NUNCA — si los tests fallan es porque algo regresó.
  • Solo regenerar si entendés POR QUÉ subió el Brier y aceptás el cambio.

Uso:
  python scripts/regen_brier_baseline.py            # interactivo (pide confirm)
  python scripts/regen_brier_baseline.py --yes      # sin confirmación

Lo que hace:
  1. Corre el monitor (refresca calibration_factors.json)
  2. Lee los Briers por mercado del JSON actualizado
  3. Compara con el baseline existente (avisa qué mercados subieron Brier)
  4. Pide confirmación
  5. Sobrescribe brier_baseline.json
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT          = Path(__file__).parent.parent
BASELINE_FILE = ROOT / "config" / "brier_baseline.json"
CURRENT_FILE  = ROOT / "config" / "calibration_factors.json"

MIN_N_FOR_BASELINE = 10


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true",
                        help="No pedir confirmación")
    parser.add_argument("--no-refresh", action="store_true",
                        help="No correr el monitor, usar el JSON actual")
    args = parser.parse_args()

    # ── 1) Refrescar calibration_factors.json ───────────────────────────
    if not args.no_refresh:
        print("Corriendo calibration_monitor para refrescar factores...")
        from src.models.calibration_monitor import compute_calibration
        compute_calibration(verbose=False)
        print("OK — calibration_factors.json actualizado")

    if not CURRENT_FILE.exists():
        print(f"ERROR: {CURRENT_FILE} no existe")
        sys.exit(1)

    current = json.loads(CURRENT_FILE.read_text())
    base    = json.loads(BASELINE_FILE.read_text()) if BASELINE_FILE.exists() else {}
    base_markets = base.get("markets", {})

    # ── 2) Comparar mercado por mercado ─────────────────────────────────
    new_markets: dict = {}
    diffs = []
    for mkt, data in current.items():
        if not isinstance(data, dict): continue
        if mkt in ("updated_at", "window_days", "by_league"): continue
        b = data.get("brier")
        n = data.get("n_bets", 0)
        if b is None or n < MIN_N_FOR_BASELINE: continue
        new_markets[mkt] = round(float(b), 4)
        old = base_markets.get(mkt)
        if old is not None:
            delta = new_markets[mkt] - old
            diffs.append((mkt, old, new_markets[mkt], delta, n))

    # Brier global ponderado por n
    global_n, global_w = 0, 0.0
    for mkt in ("home_win", "draw", "away_win", "over25", "under25"):
        d = current.get(mkt, {})
        if isinstance(d, dict) and d.get("brier") and d.get("n_bets", 0) >= MIN_N_FOR_BASELINE:
            global_w += d["brier"] * d["n_bets"]
            global_n += d["n_bets"]
    new_global = round(global_w / global_n, 4) if global_n else None

    # ── 3) Resumen de cambios ────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("DIFF vs baseline actual")
    print("=" * 70)
    print(f"{'Mercado':<14} {'Baseline':>10} {'Nuevo':>10} {'Δ':>10} {'%Δ':>8} n")
    print("-" * 70)
    regressed = []
    for mkt, old, new, delta, n in sorted(diffs, key=lambda x: -abs(x[3])):
        pct = 100 * delta / old if old else 0
        marker = "✅" if delta < 0 else ("⚠️ " if pct > 5 else "≈")
        print(f"{mkt:<14} {old:>10.4f} {new:>10.4f} {delta:>+10.4f} {pct:>+7.2f}% {marker} (n={n})")
        if pct > 5: regressed.append(mkt)

    old_global = base.get("global_brier")
    if old_global and new_global:
        delta_g = new_global - old_global
        print(f"\nGlobal: {old_global:.4f} → {new_global:.4f}  ({delta_g:+.4f}, {100*delta_g/old_global:+.2f}%)")

    if regressed:
        print(f"\n⚠️  {len(regressed)} mercado(s) regresaron >5%: {regressed}")
        print("   ¿Estás seguro de aceptar este nuevo baseline?")

    # ── 4) Confirmación ──────────────────────────────────────────────────
    if not args.yes:
        ans = input("\nSobrescribir baseline? [y/N]: ").strip().lower()
        if ans not in ("y", "yes", "s", "si", "sí"):
            print("Aborted.")
            return

    # ── 5) Escribir nuevo baseline ──────────────────────────────────────
    new_base = {
        "_comment":            "Baseline Brier — regenerado por scripts/regen_brier_baseline.py",
        "_purpose":            "tests/test_brier_regression.py compara contra estos valores.",
        "_regen":              "Solo regenerar tras validar mejoras con backtest A/B.",
        "_updated_at":         date.today().isoformat(),
        "global_brier":        new_global,
        "markets":             new_markets,
        "regression_tol_pct":  base.get("regression_tol_pct", 0.05),
    }
    BASELINE_FILE.write_text(json.dumps(new_base, indent=2))
    print(f"\n✅ Baseline regenerado en {BASELINE_FILE}")
    print(f"   {len(new_markets)} mercados, global_brier={new_global}")


if __name__ == "__main__":
    main()
