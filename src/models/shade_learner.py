"""
src/models/shade_learner.py
===========================
Aprende cada semana CUÁNTO conservar de los ajustes manuales (shades).

Después de anclar al mercado, la probabilidad pasa por reglas escritas a
mano: el sesgo favorito-longshot (FLB), "la tabla miente" y los empates
contextuales, con un tope conjunto de ±5pt (D12). Su efecto combinado en
cada mercado anclado es δ (shade_delta) y la probabilidad final es
    p = p_ancla + s · δ
Hasta el 22-sep-26 s valía 1 siempre: las reglas se aplicaban tal como se
diseñaron, sin que nada midiera si aciertan.

Método — el resultado real como juez:
  El CLV no sirve aquí: estas reglas afirman que el precio está sesgado
  incluso al cierre, así que el cierre no puede confirmarlas. Con las
  candidatas shadow ya liquidadas (y = 1 si ganó, 0 si perdió), la escala
  que minimiza el error cuadrático (Brier) de p es la pendiente de la
  regresión SIN intercepto
      y − p_ancla = s · δ + ruido
    s ≈ 1 → las reglas aciertan tal como están.
    s ≈ 0 → no aportan: mejor la probabilidad anclada sola.
    s < 0 → empeoran (se acota a 0: se apagan).
  Un win/loss tiene mucha varianza: hacen falta cientos de candidatas para
  que los datos pesen más que el prior. Por eso el prior es "las reglas
  como están" y el paso semanal está acotado.

Límites (misma política que el peso del modelo — automático con límites):
  - Prior s = 1 con sd 0.5, encogimiento precisión-ponderado.
  - Rango [0, 1]: puede apagar las reglas, no amplificarlas (δ ya pasó el
    tope D12 y amplificarlo lo rebasaría).
  - Paso máximo ±0.25 por semana.
  - MIN_N candidatas liquidadas (win/loss) con δ ≠ 0 por familia; si una
    familia no llega se usa el estimado agregado y si tampoco, el prior.
  - Solo la cohorte actual (settings.LEARNING_SINCE); error estándar robusto
    por partido (las candidatas de un partido no son independientes).
  - Push y medias (AH de cuarto, DNB con empate) quedan fuera: su p es la
    de ganar condicionada a que no haya devolución.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config.settings import LEARNING_SINCE
from src.models.anchor_learner import FAMILIES, market_family
from src.utils.model_state import load_state, save_state

STATE_KEY = "shade_scales"

PRIOR_SCALE = 1.0          # las reglas tal como se diseñaron
PRIOR_SD = 0.5
S_MIN, S_MAX = 0.0, 1.0
MAX_WEEKLY_STEP = 0.25
MIN_N = 300                # candidatas win/loss con δ ≠ 0 para estimar una familia
MIN_CLUSTERS = 30          # partidos distintos
MIN_ABS_DELTA = 1e-6       # δ = 0 no informa nada sobre la escala


def fit_scale(delta: np.ndarray, resid: np.ndarray, clusters: np.ndarray) -> dict:
    """
    OLS sin intercepto de resid (y − p_ancla) sobre delta, con error
    estándar robusto por cluster (partido). Función pura.
    """
    n = len(delta)
    sxx = float(np.sum(delta * delta)) if n else 0.0
    if n < 3 or sxx <= 0:
        return {"n": int(n), "clusters": 0, "s": None, "se": None}
    s = float(np.sum(delta * resid) / sxx)
    u = resid - s * delta
    scores = pd.Series(delta * u).groupby(np.asarray(clusters)).sum().to_numpy()
    g = len(scores)
    correction = g / (g - 1) if g > 1 else 1.0
    se = float(np.sqrt(correction * np.sum(scores ** 2)) / sxx)
    return {"n": int(n), "clusters": int(g), "s": s, "se": se}


def posterior_scale(s_hat: float | None, se: float | None,
                    prior: float = PRIOR_SCALE, prior_sd: float = PRIOR_SD) -> float:
    """Media posterior normal-normal (precisión-ponderada), acotada al rango."""
    if s_hat is None or se is None or not np.isfinite(s_hat) or not np.isfinite(se) or se <= 0:
        return prior
    w_data, w_prior = 1.0 / se ** 2, 1.0 / prior_sd ** 2
    post = (s_hat * w_data + prior * w_prior) / (w_data + w_prior)
    return float(min(S_MAX, max(S_MIN, post)))


def _step_limited(target: float, previous: float | None) -> float:
    if previous is None:
        previous = PRIOR_SCALE
    lo, hi = previous - MAX_WEEKLY_STEP, previous + MAX_WEEKLY_STEP
    return float(min(S_MAX, max(S_MIN, min(hi, max(lo, target)))))


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Filas útiles de shadow_bets liquidadas → (family, delta, resid, cluster)."""
    cols = ("family", "delta", "resid", "cluster")
    if df.empty:
        return pd.DataFrame(columns=list(cols))
    out = df.copy()
    for c in ("p_pre_shade", "shade_delta"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[out["result"].isin(("win", "loss"))
              & out["p_pre_shade"].notna() & out["shade_delta"].notna()]
    out = out[out["shade_delta"].abs() > MIN_ABS_DELTA]
    out["family"] = out["market"].map(market_family)
    out = out[out["family"].notna()]
    y = (out["result"] == "win").astype(float)
    out["delta"] = out["shade_delta"].astype(float)
    out["resid"] = y - out["p_pre_shade"].astype(float)
    out["cluster"] = out["match"].astype(str) + "|" + out["match_date"].astype(str)
    return out


def _enough(fit: dict) -> bool:
    return (fit.get("s") is not None and fit["n"] >= MIN_N
            and fit.get("clusters", 0) >= MIN_CLUSTERS)


def learn_shade_scales(df: pd.DataFrame, previous: dict | None = None) -> dict:
    """
    Estado nuevo de escalas a partir de candidatas shadow liquidadas.
    `previous` es el estado de la semana anterior (para el paso máximo).
    Función pura: no toca la DB.
    """
    data = prepare(df)
    prev_fams = (previous or {}).get("families", {})

    def _fit(sub):
        if sub.empty:
            return {"n": 0, "clusters": 0, "s": None, "se": None}
        return fit_scale(sub["delta"].to_numpy(), sub["resid"].to_numpy(),
                         sub["cluster"].to_numpy())

    pooled = _fit(data)
    families = {}
    for fam in FAMILIES:
        fit = _fit(data[data["family"] == fam])
        if _enough(fit):
            source, target = "family", posterior_scale(fit["s"], fit["se"])
        elif _enough(pooled):
            source, target = "pooled", posterior_scale(pooled["s"], pooled["se"])
        else:
            source, target = "prior", PRIOR_SCALE
        prev_s = (prev_fams.get(fam) or {}).get("scale")
        families[fam] = {
            "scale": round(_step_limited(target, prev_s), 4),
            "target": round(target, 4),
            "source": source,
            "n": fit["n"],
            "s_hat": None if fit.get("s") is None else round(fit["s"], 4),
            "se": None if fit.get("se") is None else round(fit["se"], 4),
        }

    return {
        "families": families,
        "pooled": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in pooled.items()},
        "policy": {
            "prior": PRIOR_SCALE, "prior_sd": PRIOR_SD, "range": [S_MIN, S_MAX],
            "max_weekly_step": MAX_WEEKLY_STEP, "min_n": MIN_N,
            "min_clusters": MIN_CLUSTERS, "since": LEARNING_SINCE,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def family_scale(family: str | None, state: dict | None) -> float:
    """Escala de una familia; 1.0 si no hay estado o si el valor es inválido
    (un estado corrupto nunca debe tumbar ni torcer una corrida)."""
    try:
        s = float(state["families"][family]["scale"])
        if S_MIN <= s <= S_MAX:
            return s
    except (TypeError, KeyError, ValueError, AttributeError):
        pass
    return PRIOR_SCALE


def shade_scale_for(market: str, state: dict | None) -> float:
    """Escala de los shades para un mercado anclado (1.0 si no hay estado)."""
    return family_scale(market_family(market), state)


def load_shade_scales() -> dict:
    return load_state(STATE_KEY) or {}


def run_shade_learning(df: pd.DataFrame | None = None, verbose: bool = True) -> dict:
    """Paso semanal: lee las shadow liquidadas, aprende, persiste y reporta."""
    if df is None:
        from src.models.rule_evidence import read_resolved_shadow
        df = read_resolved_shadow()
    previous = load_shade_scales()
    state = learn_shade_scales(df, previous)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev = (previous or {}).get("families", {})
    lines = [b("🎚️ ESCALA DE LOS AJUSTES MANUALES (aprendida con resultados reales)")]
    for fam, node in state.get("families", {}).items():
        before = (prev.get(fam) or {}).get("scale", PRIOR_SCALE)
        s_hat = node.get("s_hat")
        s_txt = f"ŝ={s_hat:+.2f}±{node['se']:.2f}" if s_hat is not None else "ŝ=—"
        lines.append(f"  {fam:<14} {before:.0%} → {node['scale']:.0%}  "
                     f"(n={node['n']}, {s_txt}, fuente={node['source']})")
    pooled = state.get("pooled", {})
    if pooled.get("s") is not None:
        lines.append(f"  agregado: n={pooled['n']} ŝ={pooled['s']:+.2f}±{pooled['se']:.2f}")
    return "\n".join(lines)
