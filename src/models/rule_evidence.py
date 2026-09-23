"""
src/models/rule_evidence.py
===========================
Evidencia semanal de las reglas del modelo contra RESULTADOS reales.

Las candidatas shadow (todas las que tienen precio de referencia y se
desvían ≥2pt del mercado, se hayan apostado o no) se liquidan con la misma
función que las apuestas (save_bets.resolve_shadow_outcomes). Con eso cada
regla se juzga por lo que de verdad pasó y no por su justificación original:

  1. Modelo vs mercado — ¿la probabilidad final acierta más que la del
     mercado sin margen? (Brier por familia; negativo = el modelo aporta)
  2. Ajustes manuales (FLB, "la tabla miente", empates) — ¿la probabilidad
     con ajustes acierta más que la anclada sin ellos? (Brier)
  3. Sesgo favorito-longshot DEL MERCADO — acierto real − prob. del mercado
     por lado y banda de cuota. Si la probabilidad sin margen ya no muestra
     el sesgo, la regla FLB lo estaría corrigiendo dos veces.
  4. Apostables (edge ≥ MIN_EDGE) — ¿el edge que el modelo dice tener es
     real? Brecha de calibración (prob. − acierto) y ROI por unidad.
  5. Filtros de selección sobre las apostables: entre semana, fuera de los
     sweet spots de cuota y ligas duras. Cada filtro afirma que su grupo es
     PEOR; se compara su brecha de calibración contra la del resto.

Honestidad estadística:
  - IC 95% con error estándar robusto por partido (las candidatas de un
    mismo partido no son independientes).
  - Sin veredicto con menos de MIN_N filas o MIN_CLUSTERS partidos.
  - Push y medias (AH de cuarto, DNB con empate) cuentan en el ROI pero no
    en Brier/calibración: su p es la de ganar sin devolución.
  - Solo la cohorte actual (settings.LEARNING_SINCE).
  - Las candidatas son todas las del barrido, no solo las apostadas: miden
    la regla en toda la región donde actúa.

Este módulo solo MIDE. Con estos mismos datos se aprende sola la escala de
los ajustes manuales (shade_learner); los filtros quedan a decisión del
dueño con la evidencia a la vista — son decisiones discretas y su señal
(win/loss) necesita mucha más muestra que la del CLV.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from config.settings import LEARNING_SINCE
from src.models.anchor_learner import FAMILIES, market_family
from src.utils.model_state import save_state

STATE_KEY = "rule_evidence"

MIN_N = 100            # filas para dar veredicto
MIN_CLUSTERS = 30      # partidos distintos para dar veredicto
MIN_N_GROUP = 50       # filtros: filas por grupo (penalizado y resto)
MIN_CLUSTERS_GROUP = 15
Z95 = 1.96
MIN_ABS_DELTA = 1e-6   # δ = 0 → la regla no actuó en esa candidata
SETTLED = ("win", "loss", "push", "half_win", "half_loss")
ODDS_BANDS = ((1.01, 1.60), (1.60, 2.20), (2.20, 2.80), (2.80, 4.00), (4.00, 1000.0))

VERDICT_FOR = "a favor"
VERDICT_AGAINST = "en contra"
VERDICT_NONE = "sin evidencia"
VERDICT_THIN = "insuficiente"


# ─────────────────────────────────────────────────────────────────────────────
# Estadística (funciones puras)
# ─────────────────────────────────────────────────────────────────────────────

def cluster_mean(values, clusters) -> dict:
    """Media con error estándar robusto por cluster (partido)."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    if n == 0:
        return {"n": 0, "clusters": 0, "mean": None, "se": None}
    m = float(v.mean())
    sums = pd.Series(v - m).groupby(np.asarray(clusters)).sum().to_numpy()
    g = len(sums)
    se = float(np.sqrt(g / (g - 1) * np.sum(sums ** 2)) / n) if g > 1 else None
    return {"n": int(n), "clusters": int(g), "mean": m, "se": se}


def verdict(stat: dict, lower_supports: bool, min_n: int = MIN_N,
            min_clusters: int = MIN_CLUSTERS) -> str:
    """
    Veredicto de un estadístico frente a lo que afirma la regla.
    `lower_supports`: True si un valor NEGATIVO respalda la regla (ej.
    ΔBrier < 0 = los ajustes aciertan más); False si lo respalda uno positivo.
    """
    if (stat.get("n", 0) < min_n or stat.get("clusters", 0) < min_clusters
            or stat.get("se") is None or stat.get("mean") is None):
        return VERDICT_THIN
    lo = stat["mean"] - Z95 * stat["se"]
    hi = stat["mean"] + Z95 * stat["se"]
    if hi < 0:
        return VERDICT_FOR if lower_supports else VERDICT_AGAINST
    if lo > 0:
        return VERDICT_AGAINST if lower_supports else VERDICT_FOR
    return VERDICT_NONE


def _stat(values, clusters, lower_supports: bool, **kw) -> dict:
    s = cluster_mean(values, clusters)
    s["verdict"] = verdict(s, lower_supports, **kw)
    return _rounded(s)


def _diff_stat(a: dict, b: dict, lower_supports: bool) -> dict:
    """a − b entre dos grupos (se aproximan independientes: IC conservador
    si comparten partidos con correlación positiva, que es lo habitual)."""
    out = {"n": min(a["n"], b["n"]), "clusters": min(a["clusters"], b["clusters"]),
           "mean": None, "se": None}
    if a["mean"] is not None and b["mean"] is not None:
        out["mean"] = a["mean"] - b["mean"]
        if a["se"] is not None and b["se"] is not None:
            out["se"] = float(np.sqrt(a["se"] ** 2 + b["se"] ** 2))
    out["verdict"] = verdict(out, lower_supports, min_n=MIN_N_GROUP,
                             min_clusters=MIN_CLUSTERS_GROUP)
    return _rounded(out)


def _rounded(s: dict) -> dict:
    """Floats a 6 decimales y NaN/inf → None (JSONB no acepta NaN)."""
    def r(v):
        if isinstance(v, float):
            return round(float(v), 6) if np.isfinite(v) else None
        return v
    return {k: r(v) for k, v in s.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Preparación
# ─────────────────────────────────────────────────────────────────────────────

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Filas liquidadas de shadow_bets → columnas de análisis."""
    cols = ["match", "match_date", "league", "market", "p_final", "p_ref", "odds",
            "edge_market", "p_pre_shade", "shade_delta", "result", "profit"]
    if df.empty:
        return pd.DataFrame(columns=cols + ["family", "binary", "y", "cluster", "weekday"])
    out = df.copy()
    for c in ("p_final", "p_ref", "odds", "edge_market", "p_pre_shade", "shade_delta", "profit"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[out["result"].isin(SETTLED) & (out["odds"] > 1.01)].copy()
    out["family"] = out["market"].map(market_family)
    out["binary"] = out["result"].isin(("win", "loss"))
    out["y"] = (out["result"] == "win").astype(float)
    out["cluster"] = out["match"].astype(str) + "|" + out["match_date"].astype(str)
    out["weekday"] = pd.to_datetime(out["match_date"], utc=True).dt.weekday
    return out


def rule_params() -> dict:
    """Parámetros de las reglas tal como los aplica el pipeline (una fuente)."""
    from src.pipeline import prediction_pipeline as pp
    return {"min_edge": pp.MIN_EDGE, "midweek_days": set(pp.MIDWEEK_DAYS),
            "tough_leagues": set(pp.TOUGH_LEAGUES), "sweet_spots": dict(pp.SWEET_SPOTS),
            "flb_ref": pp.FLB_REF, "flb_side": pp.flb_side}


# ─────────────────────────────────────────────────────────────────────────────
# Secciones del reporte
# ─────────────────────────────────────────────────────────────────────────────

def _by_family(d: pd.DataFrame, values: pd.Series, lower_supports: bool) -> dict:
    out = {}
    for fam in FAMILIES:
        m = d["family"] == fam
        if m.any():
            out[fam] = _stat(values[m], d.loc[m, "cluster"], lower_supports)
    return out


def model_vs_market(data: pd.DataFrame) -> dict:
    """ΔBrier = Brier(p_final) − Brier(p_mercado). Negativo = el modelo aporta."""
    d = data[data["binary"] & data["family"].notna()
             & data["p_final"].notna() & data["p_ref"].notna()]
    diff = (d["p_final"] - d["y"]) ** 2 - (d["p_ref"] - d["y"]) ** 2
    return {"all": _stat(diff, d["cluster"], lower_supports=True),
            "by_family": _by_family(d, diff, lower_supports=True)}


def shades(data: pd.DataFrame) -> dict:
    """ΔBrier = Brier(con ajustes) − Brier(anclada sin ajustes). Negativo = ayudan."""
    d = data[data["binary"] & data["p_pre_shade"].notna() & data["shade_delta"].notna()
             & (data["shade_delta"].abs() > MIN_ABS_DELTA)]
    diff = (d["p_final"] - d["y"]) ** 2 - (d["p_pre_shade"] - d["y"]) ** 2
    up = d["shade_delta"] > 0
    return {"all": _stat(diff, d["cluster"], lower_supports=True),
            "up": _stat(diff[up], d.loc[up, "cluster"], lower_supports=True),
            "down": _stat(diff[~up], d.loc[~up, "cluster"], lower_supports=True),
            "by_family": _by_family(d, diff, lower_supports=True)}


def flb_market(data: pd.DataFrame, params: dict) -> dict:
    """
    Acierto real − prob. del mercado sin margen. La regla FLB afirma que el
    mercado INFRAVALORA a los favoritos (brecha > 0 con cuota < FLB_REF) y
    SOBREVALORA los longshots (brecha < 0 con cuota > FLB_REF).
    """
    d = data[data["binary"] & data["family"].notna() & data["p_ref"].notna()]
    gap = d["y"] - d["p_ref"]
    fav = d["odds"] < params["flb_ref"]
    side = d["market"].map(params["flb_side"])
    table = []
    for s in ("home", "away", "neutral"):
        for lo, hi in ODDS_BANDS:
            m = (side == s) & (d["odds"] >= lo) & (d["odds"] < hi)
            if not m.any():
                continue
            st = cluster_mean(gap[m], d.loc[m, "cluster"])
            table.append(_rounded({"side": s, "band": f"{lo:.2f}-{hi:.2f}" if hi < 100 else f"{lo:.2f}+",
                                   "n": st["n"], "gap": st["mean"], "se": st["se"],
                                   "mean_y": float(d.loc[m, "y"].mean()),
                                   "mean_p_ref": float(d.loc[m, "p_ref"].mean())}))
    return {"favorites": _stat(gap[fav], d.loc[fav, "cluster"], lower_supports=False),
            "longshots": _stat(gap[~fav], d.loc[~fav, "cluster"], lower_supports=True),
            "table": table}


def _group(d: pd.DataFrame, mask: pd.Series) -> dict:
    """Brecha de calibración (p − y, binarias) y ROI (todas) de un grupo."""
    b = d[mask & d["binary"] & d["p_final"].notna()]
    gap = cluster_mean(b["p_final"] - b["y"], b["cluster"])
    r = d[mask & d["profit"].notna()]
    roi = cluster_mean(r["profit"], r["cluster"])
    return {"gap": gap, "roi": roi}


def bettable(data: pd.DataFrame, params: dict) -> dict:
    """¿El edge que dice tener el modelo en las apostables es real?"""
    d = data[data["edge_market"] >= params["min_edge"]]
    g = _group(d, pd.Series(True, index=d.index))
    gap, roi = dict(g["gap"]), dict(g["roi"])
    # brecha > 0 = el modelo promete más aciertos de los que llegan
    gap["verdict"] = verdict(gap, lower_supports=True)
    roi["verdict"] = verdict(roi, lower_supports=False)
    bin_ = d[d["binary"] & d["p_final"].notna()]
    return {"min_edge": params["min_edge"], "gap": _rounded(gap), "roi": _rounded(roi),
            "mean_p": round(float(bin_["p_final"].mean()), 4) if len(bin_) else None,
            "hit_rate": round(float(bin_["y"].mean()), 4) if len(bin_) else None}


def filters(data: pd.DataFrame, params: dict) -> dict:
    """
    Cada filtro afirma que su grupo penalizado es PEOR: más sobreconfiado
    (brecha p − y mayor) que el resto de las apostables. Veredicto sobre la
    diferencia de brechas; el ROI se reporta como dato (mucho más ruidoso).
    """
    d = data[data["edge_market"] >= params["min_edge"]]
    in_spot_market = d["market"].isin(list(params["sweet_spots"]))
    outside = pd.Series(False, index=d.index)
    for mkt, (lo, hi) in params["sweet_spots"].items():
        m = d["market"] == mkt
        outside |= m & ~d["odds"].between(lo, hi)
    splits = {
        "entre_semana": (d["weekday"].isin(params["midweek_days"]),
                         ~d["weekday"].isin(params["midweek_days"])),
        "fuera_de_sweet_spot": (outside, in_spot_market & ~outside),
        "liga_dura": (d["league"].isin(params["tough_leagues"]),
                      ~d["league"].isin(params["tough_leagues"])),
    }
    out = {}
    for name, (pen, rest) in splits.items():
        a, b = _group(d, pen), _group(d, rest)
        out[name] = {
            "penalized": {"gap": _rounded(a["gap"]), "roi": _rounded(a["roi"])},
            "rest": {"gap": _rounded(b["gap"]), "roi": _rounded(b["roi"])},
            # la regla dice: brecha del penalizado > brecha del resto
            "gap_diff": _diff_stat(a["gap"], b["gap"], lower_supports=False),
            "roi_diff": _diff_stat(a["roi"], b["roi"], lower_supports=True),
        }
    return out


def build_rule_evidence(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Reporte completo (función pura: no toca la DB)."""
    params = params or rule_params()
    data = prepare(df)
    return {
        "since": LEARNING_SINCE,
        "n_settled": int(len(data)),
        "n_binary": int(data["binary"].sum()) if len(data) else 0,
        "n_matches": int(data["cluster"].nunique()) if len(data) else 0,
        "model_vs_market": model_vs_market(data),
        "shades": shades(data),
        "flb_market": flb_market(data, params),
        "bettable": bettable(data, params),
        "filters": filters(data, params),
        "policy": {"min_n": MIN_N, "min_clusters": MIN_CLUSTERS,
                   "min_n_group": MIN_N_GROUP, "z": Z95},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def conclusive(evidence: dict) -> bool:
    """¿Hay al menos un veredicto con datos suficientes?"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            v = node.get("verdict")
            if v is not None:
                found.append(v)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(evidence)
    return any(v != VERDICT_THIN for v in found)


# ─────────────────────────────────────────────────────────────────────────────
# DB y reporte
# ─────────────────────────────────────────────────────────────────────────────

def read_resolved_shadow() -> pd.DataFrame:
    """Solo lectura: candidatas shadow de la cohorte ya liquidadas."""
    from config.database import engine
    from src.models.save_bets import SHADOW_TABLE_SQL, SHADOW_ALTER_SQL
    with engine.begin() as conn:
        conn.execute(text(SHADOW_TABLE_SQL))
        conn.execute(text(SHADOW_ALTER_SQL))
    return pd.read_sql(text("""
        SELECT match, match_date, league, market, p_final, p_ref, odds,
               edge_market, p_pre_shade, shade_delta, result, profit
        FROM shadow_bets
        WHERE result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
          AND match_date >= CAST(:since AS timestamptz)
    """), engine, params={"since": LEARNING_SINCE})


def run_rule_evidence(df: pd.DataFrame | None = None, verbose: bool = True) -> dict:
    """Paso semanal: construye el reporte, lo guarda en model_state y lo imprime."""
    if df is None:
        df = read_resolved_shadow()
    evidence = build_rule_evidence(df)
    save_state(STATE_KEY, evidence)
    if verbose:
        print(format_report(evidence))
    return evidence


def _fmt(stat: dict, scale: float = 1.0, unit: str = "", digits: int = 4) -> str:
    if stat.get("mean") is None:
        return "—"
    ci = f" ± {Z95 * stat['se'] * scale:.{digits}f}{unit}" if stat.get("se") is not None else ""
    return f"{stat['mean'] * scale:+.{digits}f}{unit}{ci}"


def format_report(ev: dict, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = [b("🧪 EVIDENCIA DE LAS REGLAS (resultados reales)"),
             f"Cohorte desde {ev.get('since')}: {ev.get('n_settled', 0)} candidatas liquidadas "
             f"en {ev.get('n_matches', 0)} partidos (veredicto con n ≥ {MIN_N})."]
    if not ev.get("n_settled"):
        lines.append("Aún no hay candidatas liquidadas.")
        return "\n".join(lines)

    mvm = ev["model_vs_market"]
    lines.append(b("Modelo vs mercado") + " (Brier; − = el modelo acierta más)")
    lines.append(f"  todas         {_fmt(mvm['all'])}  n={mvm['all']['n']}  → {mvm['all']['verdict']}")
    for fam, st in mvm["by_family"].items():
        lines.append(f"  {fam:<13} {_fmt(st)}  n={st['n']}  → {st['verdict']}")

    sh = ev["shades"]
    lines.append(b("Ajustes manuales") + " (FLB, tabla, empates; Brier con vs sin; − = ayudan)")
    for k, label in (("all", "todos"), ("up", "suben prob."), ("down", "bajan prob.")):
        st = sh[k]
        lines.append(f"  {label:<13} {_fmt(st)}  n={st['n']}  → {st['verdict']}")

    flb = ev["flb_market"]
    lines.append(b("Sesgo favorito-longshot del mercado") + " (acierto − prob. mercado)")
    lines.append(f"  favoritos     {_fmt(flb['favorites'], 100, 'pt', 1)}  "
                 f"n={flb['favorites']['n']}  → {flb['favorites']['verdict']}")
    lines.append(f"  longshots     {_fmt(flb['longshots'], 100, 'pt', 1)}  "
                 f"n={flb['longshots']['n']}  → {flb['longshots']['verdict']}")

    bt = ev["bettable"]
    lines.append(b(f"Apostables (edge ≥ {bt['min_edge'] * 100:.0f}pt)"))
    lines.append(f"  brecha p − acierto {_fmt(bt['gap'], 100, 'pt', 1)}  n={bt['gap']['n']}  "
                 f"→ {bt['gap']['verdict']}")
    lines.append(f"  ROI por unidad     {_fmt(bt['roi'], 100, '%', 1)}  n={bt['roi']['n']}")

    lines.append(b("Filtros") + " (brecha del grupo penalizado − resto; + = la regla acierta)")
    for name, node in ev["filters"].items():
        gd = node["gap_diff"]
        lines.append(f"  {name.replace('_', ' '):<20} {_fmt(gd, 100, 'pt', 1)}  "
                     f"n={node['penalized']['gap']['n']}/{node['rest']['gap']['n']}  → {gd['verdict']}")
    return "\n".join(lines)
