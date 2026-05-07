"""
tests/test_brier_regression.py
==============================
Pre-deploy guard (Sprint 2 — Mejora #6).

Si un cambio rompe la calibración, este test FALLA antes de deploy.

Lee `config/brier_baseline.json` (snapshot del último Brier validado) y
compara con `config/calibration_factors.json` (calculado por el monitor
semanal). El test falla si:
  • global_brier actual > baseline * (1 + tol)
  • Brier por mercado (con sample suficiente) empeora >tol%

NO corre el monitor — usa los valores ya calculados. Esto desacopla CI del DB.

Para regenerar el baseline tras una mejora real:
  1. Verificar mejora con backtest A/B
  2. python -m src.models.calibration_monitor    (refresca calibration_factors.json)
  3. Copiar Briers a config/brier_baseline.json
  4. Commit ambos
"""

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
BASELINE_FILE = ROOT / "config" / "brier_baseline.json"
CURRENT_FILE  = ROOT / "config" / "calibration_factors.json"

# Mercados con n<10 son ruidosos, no aplica el guard.
MIN_N_FOR_GUARD = 10


def _load(path: Path) -> dict:
    with open(path, "r") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def baseline():
    if not BASELINE_FILE.exists():
        pytest.skip(f"baseline no existe en {BASELINE_FILE}")
    return _load(BASELINE_FILE)


@pytest.fixture(scope="module")
def current():
    if not CURRENT_FILE.exists():
        pytest.skip(f"calibration_factors.json no existe — correr monitor primero")
    return _load(CURRENT_FILE)


def test_global_brier_no_regression(baseline, current):
    """Brier global no debe empeorar más de tol_pct vs baseline."""
    base_global = baseline.get("global_brier")
    if base_global is None:
        pytest.skip("baseline.global_brier no definido")

    # current_factors.json no guarda global_brier directamente — lo
    # reconstruimos como promedio ponderado por n_bets de los mercados base.
    base_markets = ("home_win", "draw", "away_win", "over25", "under25")
    weighted, total_n = 0.0, 0
    for mkt in base_markets:
        d = current.get(mkt, {})
        if isinstance(d, dict) and d.get("brier") and d.get("n_bets", 0) >= MIN_N_FOR_GUARD:
            weighted += d["brier"] * d["n_bets"]
            total_n  += d["n_bets"]
    if total_n == 0:
        pytest.skip("sin mercados con n suficiente para reconstruir global")

    cur_global = weighted / total_n
    tol = baseline.get("regression_tol_pct", 0.05)
    threshold = base_global * (1 + tol)

    assert cur_global <= threshold, (
        f"REGRESIÓN: Brier global {cur_global:.4f} > baseline*{1+tol} = {threshold:.4f}\n"
        f"Baseline {base_global:.4f}  →  Actual {cur_global:.4f}\n"
        f"Si la regresión es esperada (cambio de modelo, drift legítimo), "
        f"actualizar config/brier_baseline.json."
    )


def test_per_market_brier_no_regression(baseline, current):
    """Cada mercado base con n>=10 no debe empeorar Brier más de tol vs baseline."""
    tol = baseline.get("regression_tol_pct", 0.05)
    base_markets = baseline.get("markets", {})
    failures = []

    for mkt, base_brier in base_markets.items():
        if base_brier is None:
            continue
        cur_data = current.get(mkt, {})
        if not isinstance(cur_data, dict):
            continue
        cur_brier = cur_data.get("brier")
        n         = cur_data.get("n_bets", 0)
        if cur_brier is None or n < MIN_N_FOR_GUARD:
            continue

        threshold = base_brier * (1 + tol)
        if cur_brier > threshold:
            failures.append(
                f"  {mkt:<14} baseline={base_brier:.4f}  actual={cur_brier:.4f}  "
                f"(+{(cur_brier/base_brier-1)*100:.1f}%) [n={n}]"
            )

    assert not failures, (
        f"Brier regressión por mercado (>{int(tol*100)}% tolerancia):\n" +
        "\n".join(failures) +
        "\n\nSi es esperado, regenerar config/brier_baseline.json."
    )


def test_baseline_file_well_formed(baseline):
    """Garantiza que el baseline tiene los campos esperados."""
    assert "regression_tol_pct" in baseline
    assert "markets" in baseline
    assert isinstance(baseline["markets"], dict)
    assert 0 < baseline["regression_tol_pct"] < 1
