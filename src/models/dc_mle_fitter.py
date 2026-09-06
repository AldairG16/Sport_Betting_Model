"""
Dixon-Coles MLE Fitter
======================
Ajusta los parámetros del modelo Dixon-Coles mediante Máxima Verosimilitud (MLE)
sobre los datos históricos de la DB.

¿Por qué es mejor que el enfoque actual?
  Enfoque actual (heurístico):
    lambda_home = attack_home × defense_away × home_adv × tempo
    donde attack/defense = promedios ponderados de goles (no ajustan por rival)

  Dixon-Coles MLE:
    lambda_home = exp(α_home + β_away + γ)
    donde α (ataque) y β (defensa) son ajustados SIMULTÁNEAMENTE para todos
    los equipos minimizando la log-verosimilitud → ajuste automático por calidad
    del rival.

  Diferencia práctica:
    - Liverpool marca 3 vs Norwich (rival débil) → α_liverpool se ajusta poco
    - Liverpool marca 3 vs City (rival fuerte) → α_liverpool sube más
    El enfoque actual no distingue. MLE sí.

Arquitectura:
  1. fit_dc_parameters():  ajusta y guarda en data/dc_params.json
  2. get_dc_lambdas():     carga los parámetros y retorna (λ_home, λ_away)
  3. Integración pipeline: usa lambdas MLE como señal adicional (blend 40%)

El ajuste corre SEMANALMENTE (modo weekly) ya que tarda ~20-60 segundos.
Los resultados se cachean en disco para no recalcular en cada predicción.

Parámetros del modelo:
  α_i : ataque del equipo i  (en log-espacio)
  β_i : defensa del equipo i (en log-espacio; mayor = mejor defensa)
  γ   : ventaja de local (compartida para todos los equipos)
  ρ   : parámetro tau de Dixon-Coles (corrección baja puntuación)

Restricción de identificabilidad: mean(α_i) = 0
"""

import json
import sys
import warnings
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

sys.path.append(str(Path(__file__).parent.parent.parent))

from config.database import engine

DC_PARAMS_FILE = Path(__file__).parent.parent.parent / "data" / "dc_params.json"
DC_PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── Hiperparámetros del ajuste ───────────────────────────────────────────
DECAY_PER_DAY   = 0.006    # decay temporal — vida media ~115 días (antes 0.004 = ~173 días)
                           # Con 0.006: partidos de 6 meses pesan 34% vs 49% antes
                           # Más sensible a forma reciente y cambios de entrenador
MIN_MATCHES     = 8        # mínimo de partidos para incluir un equipo en el ajuste
MAX_SEASONS     = 3        # usar solo últimas 3 temporadas (≈ 3 × 365 días)
MAX_ITER        = 2000     # iteraciones máximas del optimizador
                           # L-BFGS-B con limited-memory Hessian → ~30s aún con 2000 iters
                           # Necesario para convergencia plena con prior + reg fuerte

# ─── L2 Regularization (Sprint 1 — item #1, 06-may-26) ──────────────────
# El fit anterior tenía REG=0.001 sobre attacks/defenses y NADA sobre rho.
# Síntomas: rho=0.0 (corner solution) — el optimizador se quedaba pegado
# al borde superior por dos razones: (a) `np.clip` mataba el gradiente,
# (b) sin prior, pequeñas variaciones de data movían rho cualquier dirección.
#
# Fixes aplicados:
#   1. Transformación sigmoide de rho → siempre en (-0.5, 0), gradiente fluye.
#   2. Prior bayesiano fuerte sobre rho (PRIOR_RHO=-0.10, REG_RHO=50)
#      → centrado en el valor empírico DC97 (Dixon-Coles 1997).
#   3. L2 sobre teams subido 10x (0.001 → 0.01) para shrinkage efectivo
#      en los 534 equipos (muchos con n<20 partidos).
REG_TEAMS       = 0.001    # L2 sobre attacks/defenses (mismo valor original)
                           # 0.005 y 0.01 sobre-encogían: x0 arranca en zeros
                           # y la reg fuerte impide que los params "alcancen"
                           # su escala natural (std ~0.58) antes que se acabe
                           # MAX_ITER. El L2 estaba aplastando el dynamic range
                           # 3x (std 0.58 → 0.19) → top teams indiferenciables
                           # de mid-tier → Brier individual peor.
                           # Conclusión: el rho era el verdadero problema, NO
                           # la reg de teams. Mantenemos 0.001 (reg suave).
REG_RHO         = 50.0     # L2 sobre desviación de rho del prior (fuerte)
PRIOR_RHO       = -0.10    # valor empírico DC97 (~ promedio fútbol europeo)
RHO_BOUND       = 0.5      # |rho| <= 0.5 (límite sano vía sigmoide)

# Backwards-compat: REGULARIZATION antiguo se mapea a REG_TEAMS por si
# algún script externo lo importaba.
REGULARIZATION  = REG_TEAMS

# ─── Blend con el pipeline actual ────────────────────────────────────────
DC_MLE_WEIGHT   = 0.40     # 40% MLE + 60% forma actual → transición suave


def _rho_from_x(x):
    """
    Transforma el parámetro libre x ∈ ℝ a rho ∈ (-0.5, 0) vía sigmoide.
    rho = -RHO_BOUND * sigmoid(x)
    Inversa: x = log(-rho / (RHO_BOUND + rho))   con rho < 0.

    Por qué: el código viejo usaba `np.clip(params[-1], -0.5, 0.0)` directamente
    sobre el parámetro a optimizar. Eso crea un plateau con gradiente nulo
    cuando params[-1] > 0, y SLSQP queda atrapado en rho=0 (corner solution).
    Con sigmoide, rho siempre vive estrictamente dentro del rango y el
    gradiente fluye en todo ℝ.
    """
    # clip estabiliza la exp para evitar overflow en x muy positivos
    x_safe = np.clip(x, -50.0, 50.0)
    return -RHO_BOUND / (1.0 + np.exp(-x_safe))


def _x_from_rho(rho):
    """Inversa de _rho_from_x — usada para inicializar."""
    rho = float(rho)
    if rho >= 0:
        rho = -1e-6
    if rho <= -RHO_BOUND:
        rho = -RHO_BOUND + 1e-6
    return float(np.log(-rho / (RHO_BOUND + rho)))


# ─────────────────────────────────────────────────────────────
# AJUSTE MLE
# ─────────────────────────────────────────────────────────────

def fit_dc_parameters(verbose: bool = True) -> dict:
    """
    Ajusta los parámetros Dixon-Coles MLE sobre datos históricos.
    Guarda el resultado en data/dc_params.json.

    Returns:
        dict con parámetros ajustados o {} si falla
    """
    if verbose:
        print("\n🔧 AJUSTANDO DIXON-COLES MLE...")

    # ── Cargar datos históricos ───────────────────────────────────────────
    cutoff = datetime.now() - timedelta(days=MAX_SEASONS * 365)

    try:
        df = pd.read_sql(f"""
            SELECT home_team, away_team, home_goals, away_goals, date,
                   COALESCE(neutral, FALSE) AS neutral
            FROM matches
            WHERE home_goals IS NOT NULL
              AND away_goals IS NOT NULL
              AND date >= '{cutoff.strftime('%Y-%m-%d')}'
              AND (league IS NULL OR league LIKE 'soccer%%' OR league LIKE 'fifa%%' OR league LIKE 'uefa%%')
            ORDER BY date ASC
        """, engine)
    except Exception as e:
        print(f"❌ dc_mle_fitter: error leyendo matches: {e}")
        return {}

    if len(df) < 500:
        print(f"⚠️  Solo {len(df)} partidos — mínimo 500 para MLE confiable")
        return {}

    if verbose:
        print(f"   Partidos: {len(df):,}  |  Rango: {df['date'].min()} → {df['date'].max()}")

    # ── Filtrar equipos con muy pocos datos ──────────────────────────────
    team_counts = pd.concat([df["home_team"], df["away_team"]]).value_counts()
    valid_teams = set(team_counts[team_counts >= MIN_MATCHES].index)
    df = df[df["home_team"].isin(valid_teams) & df["away_team"].isin(valid_teams)]

    teams    = sorted(valid_teams)
    n_teams  = len(teams)
    team_idx = {t: i for i, t in enumerate(teams)}

    if verbose:
        print(f"   Equipos con >= {MIN_MATCHES} partidos: {n_teams}")

    # ── Pesos temporales (decay exponencial) ─────────────────────────────
    df["date"]    = pd.to_datetime(df["date"])
    max_date      = df["date"].max()
    df["days_ago"] = (max_date - df["date"]).dt.days
    df["weight"]  = np.exp(-DECAY_PER_DAY * df["days_ago"])

    # ── Preparar arrays numpy para velocidad ─────────────────────────────
    h_idx   = df["home_team"].map(team_idx).values
    a_idx   = df["away_team"].map(team_idx).values
    hg      = df["home_goals"].values.astype(int)
    ag      = df["away_goals"].values.astype(int)
    weights = df["weight"].values
    # Multiplicador de home_adv: 0.0 si neutral, 1.0 si venue normal.
    # Crítico para selecciones nacionales (Mundial 2026, Euro, Copa América)
    # donde la mayoría de partidos se juegan en sede neutral. Sin esto, el
    # fitter aprende un home_adv inflado que sobreestima al "local" en los
    # partidos del Mundial.
    h_adv_mult = (~df["neutral"].astype(bool).values).astype(float)

    # ── Función de log-verosimilitud (vectorizada) ────────────────────────
    def neg_log_likelihood(params):
        attacks  = params[:n_teams]
        defenses = params[n_teams : 2 * n_teams]
        home_adv = params[-2]
        # rho via sigmoide → siempre en (-RHO_BOUND, 0), gradiente fluye
        rho      = _rho_from_x(params[-1])

        # Lambdas en log-espacio (garantiza λ > 0)
        # h_adv_mult = 0 en partidos neutrales → home_adv no contribuye
        lam_h = np.exp(attacks[h_idx] + defenses[a_idx] + home_adv * h_adv_mult)
        lam_a = np.exp(attacks[a_idx] + defenses[h_idx])

        # Log-probabilidades de Poisson
        log_p_h = hg * np.log(np.maximum(lam_h, 1e-10)) - lam_h - _log_factorial(hg)
        log_p_a = ag * np.log(np.maximum(lam_a, 1e-10)) - lam_a - _log_factorial(ag)

        # Tau (Dixon-Coles correction) — vectorizado
        tau = _tau_vec(hg, ag, lam_h, lam_a, rho)
        log_tau = np.log(np.maximum(tau, 1e-10))

        # Log-likelihood total con pesos
        ll = weights * (log_p_h + log_p_a + log_tau)

        # L2 regularization sobre teams (evita extremos con pocos datos)
        reg_teams = REG_TEAMS * (np.sum(attacks**2) + np.sum(defenses**2))
        # Prior bayesiano sobre rho hacia el valor empírico DC97 = -0.10
        # Sin esto el optimizer derivaba a rho=0 por ruido en data internacional.
        reg_rho   = REG_RHO * (rho - PRIOR_RHO) ** 2

        return -(np.sum(ll) - reg_teams - reg_rho)

    # ── Parámetros iniciales ──────────────────────────────────────────────
    # Warm start desde el fit anterior si existe, así el optimizador no
    # tiene que recorrer todo el camino desde x0=0 → ahorra ~50% iters
    # y mantiene el dynamic range natural de teams (std ~0.58 en attacks).
    # Sin warm-start, con 669 teams × 2 params = 1340 dims, L-BFGS-B se
    # queda corto y devuelve params demasiado encogidos hacia 0.
    x0 = np.zeros(2 * n_teams + 2)
    x0[-2] = 0.25                       # home_advantage inicial
    x0[-1] = _x_from_rho(PRIOR_RHO)     # rho inicial = prior

    if DC_PARAMS_FILE.exists():
        try:
            with open(DC_PARAMS_FILE, "r") as f:
                prev = json.load(f)
            prev_teams = prev.get("teams", {})
            warm = 0
            for team, idx in team_idx.items():
                p = prev_teams.get(team)
                if p:
                    x0[idx]            = p["attack"]
                    x0[n_teams + idx]  = p["defense"]
                    warm += 1
            if warm > 0:
                # home_adv del fit previo (si existe) — más cerca del óptimo
                if "home_adv" in prev:
                    x0[-2] = float(prev["home_adv"])
                if verbose:
                    print(f"   Warm-start: {warm}/{n_teams} equipos heredados de fit previo")
        except Exception as e:
            if verbose:
                print(f"   Warm-start falló ({e}) — arrancando desde zeros")

    # ── Optimización ─────────────────────────────────────────────────────
    # Antes: SLSQP con constraint mean(attacks)=0. Funcionaba pero se trababa
    # con 1070 params (10+ min con MAX_ITER=500) por finite differences O(n²).
    #
    # Ahora: L-BFGS-B sin constraint + identificabilidad post-fit.
    # El modelo Dixon-Coles es invariante al shift α→α+c, β→β-c (los
    # lambdas no cambian). Entonces optimizamos libremente y al terminar
    # restamos mean(α) de los attacks y se lo sumamos a los defenses.
    # L-BFGS-B con limited-memory Hessian escala O(n), no O(n²) → ~30s.
    if verbose:
        print("   Optimizando con L-BFGS-B... (~20-40 segundos)")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = minimize(
            neg_log_likelihood,
            x0,
            method="L-BFGS-B",
            options={"maxiter": MAX_ITER, "ftol": 1e-7, "gtol": 1e-5, "disp": False},
        )

    if not result.success and verbose:
        print(f"   ⚠️  Optimizador no convergió perfectamente: {result.message}")

    params   = result.x
    attacks  = params[:n_teams].copy()
    defenses = params[n_teams : 2 * n_teams].copy()
    home_adv = float(params[-2])
    # Mismo destransformado que neg_log_likelihood — NO usar np.clip aquí
    # porque mata el gradiente y produce el corner solution rho=0.0.
    rho      = float(_rho_from_x(params[-1]))

    # ── Identificabilidad por simetría del modelo ─────────────────────────
    # Dixon-Coles es invariante a α→α+c, β→β-c (los λ no cambian).
    # Restamos la media de attacks y la sumamos a defenses → mean(α)=0.
    # Esto ancla la escala de los parámetros sin afectar las predicciones.
    alpha_mean = float(np.mean(attacks))
    attacks  -= alpha_mean
    defenses += alpha_mean

    # ── Guardar resultados ─────────────────────────────────────────────────
    team_params = {
        team: {
            "attack":  round(float(attacks[i]), 5),
            "defense": round(float(defenses[i]), 5),
        }
        for team, i in team_idx.items()
    }

    output = {
        "teams":      team_params,
        "home_adv":   round(home_adv, 5),
        "rho":        round(rho, 5),
        "n_teams":    n_teams,
        "n_matches":  len(df),
        "converged":  bool(result.success),
        "fitted_at":  datetime.now().isoformat(),
    }

    with open(DC_PARAMS_FILE, "w") as f:
        json.dump(output, f, indent=2)

    if verbose:
        print(f"   ✅ Ajuste completado — {n_teams} equipos | home_adv={home_adv:.3f} | rho={rho:.3f}")
        print(f"   Guardado en: {DC_PARAMS_FILE}")

    return output


# ─────────────────────────────────────────────────────────────
# PREDICCIÓN CON PARÁMETROS MLE
# ─────────────────────────────────────────────────────────────

_cached_params: dict | None = None


def _load_params() -> dict:
    """Carga los parámetros del disco (con caché en memoria)."""
    global _cached_params
    if _cached_params is not None:
        return _cached_params

    if DC_PARAMS_FILE.exists():
        try:
            with open(DC_PARAMS_FILE, "r") as f:
                _cached_params = json.load(f)
            return _cached_params
        except Exception as e:
            # JSON corrupto o IO error → caemos a {} que produce λ defaults.
            # El silencio era peligroso: el pipeline corría con λ=1.5 sin
            # avisar que los params MLE no se cargaron. Loguear es esencial
            # para detectar corrupción de DC_PARAMS_FILE.
            print(f"⚠️  No se pudo cargar DC_PARAMS_FILE ({DC_PARAMS_FILE}): {e}. "
                  f"Pipeline correrá con λ defaults — re-ejecutar weekly fit.")

    return {}


def get_dc_lambdas(
    home_team: str,
    away_team: str,
    is_neutral: bool = False,
) -> tuple[float, float] | None:
    """
    Retorna (lambda_home, lambda_away) usando los parámetros MLE ajustados.

    Args:
        home_team:  nombre del equipo local (normalizado)
        away_team:  nombre del equipo visitante (normalizado)
        is_neutral: True si el partido se juega en sede neutral (Mundial,
                    Euro, Copa América, finales/semifinales en sede única).
                    Cuando True, home_adv se anula → lambdas simétricas en
                    términos de venue. Crítico para selecciones nacionales.

    Returns:
        (lambda_home, lambda_away) o None si algún equipo no está en los parámetros
    """
    params = _load_params()
    if not params:
        return None

    teams = params.get("teams", {})
    home_p = teams.get(home_team)
    away_p = teams.get(away_team)

    if home_p is None or away_p is None:
        # Intentar búsqueda case-insensitive
        home_lower = home_team.lower()
        away_lower = away_team.lower()
        teams_lower = {k.lower(): v for k, v in teams.items()}
        home_p = teams_lower.get(home_lower)
        away_p = teams_lower.get(away_lower)

    if home_p is None or away_p is None:
        return None

    home_adv = 0.0 if is_neutral else params.get("home_adv", 0.25)

    lambda_home = np.exp(home_p["attack"] + away_p["defense"] + home_adv)
    lambda_away = np.exp(away_p["attack"] + home_p["defense"])

    return float(lambda_home), float(lambda_away)


def is_params_fresh(max_age_days: int = 8) -> bool:
    """Retorna True si los parámetros fueron ajustados en los últimos max_age_days."""
    params = _load_params()
    if not params:
        return False
    fitted_at_str = params.get("fitted_at", "")
    if not fitted_at_str:
        return False
    try:
        fitted_at = datetime.fromisoformat(fitted_at_str)
        return (datetime.now() - fitted_at).days <= max_age_days
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# HELPERS VECTORIZADOS
# ─────────────────────────────────────────────────────────────

def _log_factorial(n: np.ndarray) -> np.ndarray:
    """Log-factorial vectorizado usando la aproximación de Stirling para n > 12."""
    result = np.zeros_like(n, dtype=float)
    for v in np.unique(n):
        import math
        result[n == v] = math.lgamma(v + 1)
    return result


def _tau_vec(hg, ag, lam_h, lam_a, rho):
    """Corrección tau de Dixon-Coles, vectorizada."""
    tau = np.ones(len(hg))
    m00 = (hg == 0) & (ag == 0)
    m10 = (hg == 1) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m11 = (hg == 1) & (ag == 1)
    tau[m00] = 1.0 - lam_h[m00] * lam_a[m00] * rho
    tau[m10] = 1.0 + lam_a[m10] * rho
    tau[m01] = 1.0 + lam_h[m01] * rho
    tau[m11] = 1.0 - rho
    return np.maximum(tau, 1e-10)


if __name__ == "__main__":
    params = fit_dc_parameters(verbose=True)
    if params:
        # Test rápido
        test_teams = list(params["teams"].keys())[:2]
        if len(test_teams) >= 2:
            lams = get_dc_lambdas(test_teams[0], test_teams[1])
            print(f"\n  Test: {test_teams[0]} vs {test_teams[1]}")
            print(f"  λ_home={lams[0]:.3f}  λ_away={lams[1]:.3f}")
