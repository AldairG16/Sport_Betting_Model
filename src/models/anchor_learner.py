"""
src/models/anchor_learner.py
============================
Aprende cada semana CUÁNTO confiar en el modelo frente al mercado.

La probabilidad final de un mercado anclado es
    p = p_mercado + w · (p_modelo − p_mercado) = p_mercado + w · d
y hasta ahora w estaba fijo en 0.35 (ancla 65/35, arquitectura 14-sep-26).
Este módulo lo estima con los datos que el sistema ya recolecta solo.

Método — el mercado al cierre como juez:
  Cada candidata shadow guarda d (desvío del modelo CRUDO contra el precio
  sin margen, antes de anclar) y la cuota de apertura; el closing le añade
  la cuota de cierre. Δ = 1/cierre − 1/apertura es cuánto se movió el
  mercado hacia ese resultado. Si el cierre es un estimador insesgado de la
  probabilidad real, el peso óptimo del modelo es la pendiente β de la
  regresión Δ = α + β·d: la fracción de la opinión del modelo que el
  mercado termina confirmando. (α absorbe la deriva común de márgenes
  entre apertura y cierre.)
    β ≈ 0     → el modelo no anticipa nada: su opinión no debe mover la prob.
    β ≈ 0.35  → el ancla actual está bien puesta.
    β > 0.35  → el modelo anticipa más de lo que se le reconoce.
  Ventaja frente a esperar resultados: Δ tiene ~100x menos varianza que un
  win/loss, así que con 1-2 semanas de partidos normales β ya es preciso, y
  usa TODAS las candidatas (shadow), no solo las apuestas colocadas.

Límites (decisión del dueño, 22-sep-26: automático con límites):
  - Encogimiento bayesiano hacia el prior 0.35 (precisión-ponderado): con
    poca muestra manda el prior; con mucha, los datos.
  - Rango [0.00, 0.50] y paso máximo de ±0.10 por semana.
  - Mínimo MIN_N candidatas con cierre por familia; si una familia no llega
    se usa el estimado agregado (todas las familias) y si tampoco, el prior.
  - Solo datos de la cohorte actual (settings.LEARNING_SINCE).

Independencia: se usa una sola pata por par (la que el modelo favorece,
d > 0) y el error estándar es robusto por partido (clusters), porque las
candidatas de un mismo partido no son observaciones independientes.

Calidad del cierre: solo cuentan cierres descargados poco antes del
kickoff y después de la apertura (src/utils/closing_quality.py). Un
"cierre" que es la misma cuota de apertura da Δ = 0 y sesga β hacia 0.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from config.settings import LEARNING_SINCE
from src.utils.model_state import load_state, save_state

STATE_KEY = "anchor_weights"

PRIOR_MODEL_WEIGHT = 0.35   # ancla 65/35 (arquitectura 14-sep-26)
PRIOR_SD = 0.10             # incertidumbre del prior (±0.10 ≈ 1σ)
W_MIN, W_MAX = 0.0, 0.50
MAX_WEEKLY_STEP = 0.10
MIN_N = 100                 # candidatas con cierre para estimar una familia
DELTA_CLIP = 0.15           # winsoriza movimientos absurdos (datos rotos)
MAX_ABS_DEVIATION = 0.50    # |d| mayor es dato roto, no opinión del modelo

# Familias de mercados anclables (mismo conjunto que _anchorable() del
# pipeline). Doble oportunidad y tiros no anclan: no entran.
FAMILIES = ("1x2", "totals", "btts", "ah_dnb", "halftime", "corners_cards")


def market_family(market: str) -> str | None:
    m = str(market or "")
    if m in ("home_win", "draw", "away_win"):
        return "1x2"
    if m in ("over25", "under25"):
        return "totals"
    if m in ("btts", "btts_no"):
        return "btts"
    if m.startswith(("ah_home_", "ah_away_")) or m in ("dnb_home", "dnb_away"):
        return "ah_dnb"
    if m.startswith(("h1_", "h2_")):
        return "halftime"
    if m.startswith(("corners_over_", "corners_under_", "cards_over_", "cards_under_")):
        return "corners_cards"
    return None


def fit_slope(d: np.ndarray, delta: np.ndarray, clusters: np.ndarray) -> dict:
    """
    OLS de delta sobre d con intercepto; error estándar de la pendiente
    robusto por cluster (partido). Función pura.
    """
    n = len(d)
    if n < 3 or float(np.var(d)) <= 0:
        return {"n": int(n), "beta": None, "se": None, "alpha": None}
    x = d - d.mean()
    beta = float(np.sum(x * (delta - delta.mean())) / np.sum(x * x))
    alpha = float(delta.mean() - beta * d.mean())
    resid = delta - alpha - beta * d
    scores = pd.Series(x * resid).groupby(np.asarray(clusters)).sum().to_numpy()
    g = len(scores)
    correction = g / (g - 1) if g > 1 else 1.0
    se = float(np.sqrt(correction * np.sum(scores ** 2)) / np.sum(x * x))
    return {"n": int(n), "clusters": int(g), "beta": beta, "se": se, "alpha": alpha}


def posterior_weight(beta: float | None, se: float | None,
                     prior: float = PRIOR_MODEL_WEIGHT,
                     prior_sd: float = PRIOR_SD) -> float:
    """Media posterior normal-normal (precisión-ponderada), acotada al rango."""
    if beta is None or se is None or not np.isfinite(beta) or not np.isfinite(se) or se <= 0:
        return prior
    w_data, w_prior = 1.0 / se ** 2, 1.0 / prior_sd ** 2
    post = (beta * w_data + prior * w_prior) / (w_data + w_prior)
    return float(min(W_MAX, max(W_MIN, post)))


def _step_limited(target: float, previous: float | None) -> float:
    if previous is None:
        previous = PRIOR_MODEL_WEIGHT
    lo, hi = previous - MAX_WEEKLY_STEP, previous + MAX_WEEKLY_STEP
    return float(min(W_MAX, max(W_MIN, min(hi, max(lo, target)))))


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Filtra y construye (family, d, delta, cluster) desde filas de shadow_bets."""
    if df.empty:
        return df.assign(family=[], d=[], delta=[], cluster=[])
    out = df.copy()
    for c in ("deviation", "odds", "closing_odds"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[(out["odds"] > 1.01) & (out["closing_odds"] > 1.01)
              & out["deviation"].notna()]
    out["family"] = out["market"].map(market_family)
    out = out[out["family"].notna()]
    out = out[(out["deviation"] > 0) & (out["deviation"] <= MAX_ABS_DEVIATION)]
    out["d"] = out["deviation"].astype(float)
    out["delta"] = (1.0 / out["closing_odds"] - 1.0 / out["odds"]).clip(-DELTA_CLIP, DELTA_CLIP)
    out["cluster"] = out["match"].astype(str) + "|" + out["match_date"].astype(str)
    return out


def learn_anchor_weights(df: pd.DataFrame, previous: dict | None = None) -> dict:
    """
    Estado nuevo de pesos a partir de candidatas shadow con cierre.
    `previous` es el estado de la semana anterior (para el paso máximo).
    Función pura: no toca la DB.
    """
    data = prepare(df)
    prev_fams = (previous or {}).get("families", {})

    pooled = fit_slope(data["d"].to_numpy(), data["delta"].to_numpy(),
                       data["cluster"].to_numpy()) if len(data) else {"n": 0, "beta": None, "se": None}
    pooled_ok = pooled["n"] >= MIN_N and pooled.get("beta") is not None

    families = {}
    for fam in FAMILIES:
        sub = data[data["family"] == fam]
        fit = fit_slope(sub["d"].to_numpy(), sub["delta"].to_numpy(),
                        sub["cluster"].to_numpy()) if len(sub) else {"n": 0, "beta": None, "se": None}
        if fit["n"] >= MIN_N and fit.get("beta") is not None:
            source, target = "family", posterior_weight(fit["beta"], fit["se"])
        elif pooled_ok:
            source, target = "pooled", posterior_weight(pooled["beta"], pooled["se"])
        else:
            source, target = "prior", PRIOR_MODEL_WEIGHT
        prev_w = (prev_fams.get(fam) or {}).get("weight")
        weight = _step_limited(target, prev_w)
        families[fam] = {
            "weight": round(weight, 4),
            "target": round(target, 4),
            "source": source,
            "n": fit["n"],
            "beta": None if fit.get("beta") is None else round(fit["beta"], 4),
            "se": None if fit.get("se") is None else round(fit["se"], 4),
        }

    return {
        "families": families,
        "pooled": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in pooled.items()},
        "policy": {
            "prior": PRIOR_MODEL_WEIGHT, "prior_sd": PRIOR_SD,
            "range": [W_MIN, W_MAX], "max_weekly_step": MAX_WEEKLY_STEP,
            "min_n": MIN_N, "since": LEARNING_SINCE,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def model_weight_for(market: str, state: dict | None) -> float:
    """Peso del modelo para un mercado anclado (prior si no hay estado)."""
    fam = market_family(market)
    node = ((state or {}).get("families") or {}).get(fam) if fam else None
    try:
        w = float(node["weight"])
        if W_MIN <= w <= W_MAX:
            return w
    except (TypeError, KeyError, ValueError):
        pass
    return PRIOR_MODEL_WEIGHT


def load_anchor_weights() -> dict:
    return load_state(STATE_KEY) or {}


def read_shadow_with_closing() -> pd.DataFrame:
    """
    Solo lectura: candidatas shadow de la cohorte con un cierre VÁLIDO —
    cuota descargada poco antes del kickoff y después de la apertura
    (closing_quality). Sin esa condición entraban cierres que eran la
    misma cuota de apertura (Δ = 0 exacto), que aplastan β hacia 0.
    """
    from config.database import engine
    from src.utils.closing_quality import valid_closing_sql, ensure_closing_columns
    ensure_closing_columns(engine)
    return pd.read_sql(text(f"""
        SELECT match, match_date, market, deviation, odds, closing_odds
        FROM shadow_bets
        WHERE closing_odds IS NOT NULL
          AND deviation IS NOT NULL
          AND match_date >= CAST(:since AS timestamptz)
          AND {valid_closing_sql()}
          AND closing_fetched_at > created_at
    """), engine, params={"since": LEARNING_SINCE})


def run_anchor_learning(verbose: bool = True) -> dict:
    """Paso semanal: lee shadow_bets, aprende, persiste y reporta."""
    df = read_shadow_with_closing()
    previous = load_anchor_weights()
    state = learn_anchor_weights(df, previous)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev = (previous or {}).get("families", {})
    lines = [b("⚓ PESO DEL MODELO vs MERCADO (aprendido por CLV shadow)")]
    for fam, node in state.get("families", {}).items():
        before = (prev.get(fam) or {}).get("weight", PRIOR_MODEL_WEIGHT)
        beta = node.get("beta")
        beta_s = f"β={beta:+.2f}±{node['se']:.2f}" if beta is not None else "β=—"
        lines.append(f"  {fam:<14} {before:.0%} → {node['weight']:.0%}  "
                     f"(n={node['n']}, {beta_s}, fuente={node['source']})")
    pooled = state.get("pooled", {})
    if pooled.get("beta") is not None:
        lines.append(f"  agregado: n={pooled['n']} β={pooled['beta']:+.3f}±{pooled['se']:.3f}")
    return "\n".join(lines)
