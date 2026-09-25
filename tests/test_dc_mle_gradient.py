"""
Ajuste Dixon-Coles con gradiente EXACTO (25-sep-26). Hasta entonces
L-BFGS-B aproximaba el gradiente por diferencias finitas sobre ~2,500
parámetros, agotaba MAX_FUN tras pocos pasos y NINGÚN fit convergía.
"""

import numpy as np
import pandas as pd
import pytest
from scipy.optimize import approx_fprime

from src.models import dc_mle_fitter as dc


def _problem(seed=0, n_teams=6, n=400):
    rng = np.random.default_rng(seed)
    h = rng.integers(0, n_teams, n)
    a = (h + rng.integers(1, n_teams, n)) % n_teams
    hg = rng.poisson(1.4, n)
    ag = rng.poisson(1.1, n)
    hg[:4], ag[:4] = [0, 1, 0, 1], [0, 0, 1, 1]          # los 4 marcadores con τ
    w = rng.uniform(0.2, 1.0, n)
    mult = (rng.uniform(size=n) > 0.1).astype(float)      # ~10% en sede neutral
    lf = dc._log_factorial(hg) + dc._log_factorial(ag)
    params = rng.normal(0, 0.3, 2 * n_teams + 2)
    params[-2], params[-1] = 0.25, dc._x_from_rho(-0.12)
    return params, (h, a, hg, ag, w, mult, n_teams, lf)


def test_exact_gradient_matches_finite_differences():
    params, args = _problem()
    _, grad = dc.dc_objective(params, *args)
    num = approx_fprime(params, lambda p: dc.dc_objective(p, *args)[0], 1e-6)
    np.testing.assert_allclose(grad, num, rtol=1e-4, atol=1e-4)


def test_objective_is_the_same_penalized_likelihood_as_before():
    """El valor coincide con la fórmula del ajuste anterior (_tau_vec)."""
    params, (h, a, hg, ag, w, mult, n, lf) = _problem(seed=3)
    att, de, ha = params[:n], params[n:2 * n], params[-2]
    rho = dc._rho_from_x(params[-1])
    lam_h = np.exp(att[h] + de[a] + ha * mult)
    lam_a = np.exp(att[a] + de[h])
    ll = w * (hg * np.log(lam_h) - lam_h - dc._log_factorial(hg)
              + ag * np.log(lam_a) - lam_a - dc._log_factorial(ag)
              + np.log(dc._tau_vec(hg, ag, lam_h, lam_a, rho)))
    old = -(ll.sum() - dc.REG_TEAMS * (np.sum(att ** 2) + np.sum(de ** 2))
            - dc.REG_RHO * (rho - dc.PRIOR_RHO) ** 2)
    assert dc.dc_objective(params, h, a, hg, ag, w, mult, n, lf)[0] == pytest.approx(old)


def test_fit_converges_and_recovers_home_advantage(monkeypatch):
    """Datos sintéticos con ventaja local conocida (e^0.25 = ×1.28)."""
    rng = np.random.default_rng(7)
    teams = [f"t{i}" for i in range(20)]
    att = rng.normal(0, 0.3, 20)
    dfn = rng.normal(0, 0.3, 20)
    rows, day = [], pd.Timestamp("2025-01-01")
    for k in range(3000):
        i, j = rng.choice(20, 2, replace=False)
        rows.append({"home_team": teams[i], "away_team": teams[j],
                     "home_goals": rng.poisson(np.exp(0.1 + att[i] + dfn[j] + 0.25)),
                     "away_goals": rng.poisson(np.exp(0.1 + att[j] + dfn[i])),
                     "date": day + pd.Timedelta(days=k // 8), "neutral": False})
    monkeypatch.setattr(dc.pd, "read_sql", lambda *a, **k: pd.DataFrame(rows))
    monkeypatch.setattr(dc, "_previous_fit", lambda verbose=False: {})
    out = dc.fit_dc_parameters(verbose=False, save=False)
    assert out["converged"] is True
    assert out["home_adv"] == pytest.approx(0.25, abs=0.06)
    assert out["final_grad_max"] < 1.0
