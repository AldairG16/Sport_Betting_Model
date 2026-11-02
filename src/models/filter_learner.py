"""
src/models/filter_learner.py
============================
Los filtros de selección se prenden y apagan solos con resultados reales
(19-oct-26, a pedido del dueño).

Tres filtros afirman que su grupo de apuestas es PEOR que el resto:
  - entre_semana: lunes a miércoles exigen 1.4× la ventaja mínima;
  - fuera_de_sweet_spot: fuera de la banda de cuota del mercado no se apuesta;
  - liga_dura: Serie A, MLS y Ligue 1 exigen 7 pt de ventaja.
rule_evidence ya los mide contra resultados (brecha de calibración del grupo
penalizado − la del resto, IC 95% robusto por partido), pero hasta aquí el
veredicto solo se reportaba. Ahora decide:
  - un filtro se APAGA solo si los datos lo contradicen con fuerza: n ≥ 100
    en ≥ 30 partidos en cada grupo e IC entero < 0 (su grupo acierta MEJOR
    que el resto: la penalización le quita apuestas buenas);
  - apagado, vuelve cuando la media deja de ser negativa;
  - con menos datos, prendidos: las reglas tal como se diseñaron.
Nunca agrega filtros ni los endurece. Si el estado no se puede leer, quedan
prendidos (falla cerrada). Apagar uno no salta el filtro Pinnacle: el dinero
real sigue exigiendo ventaja contra Pinnacle.

Filtro por región del 🎯 (2-nov-26, el dueño: "haz el filtro aprendido por
región para América"). Los 🎯 de América iban 25 de 59 contra 31.1 que
esperaba el mercado (−10.3 pt, −14.15u), las 5 grandes +15.1 pt y el resto
de Europa +1.9, y perdían con cualquier ventaja: pedir más no los arreglaba.
A diferencia de los tres de arriba (reglas diseñadas), este nace de los
datos, y se mide con los picks del programa, no con las candidatas sombra:
  - brecha = acierto − probabilidad del mercado, de los picks que pasan la
    regla (los 🎯 y los que el filtro pausó, que se liquidan igual) de los
    últimos REGION_WINDOW_DAYS días; gap_diff = la del resto − la de la región;
  - apagado, se prende solo si la región pierde contra el resto con IC 95%
    entero > 0 (MIN_N_GROUP picks en MIN_CLUSTERS_GROUP partidos por grupo);
  - prendido, se apaga en cuanto deja de perder en promedio (gap_diff ≤ 0);
  - con menos datos, se queda como estaba (la primera vez, apagado).
Prendido, el pick de la región se ve igual, nunca 🎯 (program_picks.decide).
Si el estado no se puede leer, ninguna región en pausa: el 🎯 sale con la
regla de siempre.
"""

import statistics
from datetime import datetime, timezone

from src.models.rule_evidence import MIN_CLUSTERS, MIN_CLUSTERS_GROUP, MIN_N, MIN_N_GROUP, Z95, VERDICT_THIN
from src.models.rule_evidence import STATE_KEY as EVIDENCE_KEY
from src.utils.log import get_logger
from src.utils.model_state import load_state, save_state

log = get_logger(__name__)

STATE_KEY = "filter_switches"

FILTERS = ("entre_semana", "fuera_de_sweet_spot", "liga_dura")
REGION_FILTERS = ("america",)
REGION_WINDOW_DAYS = 90
LABELS = {"entre_semana": "partidos entre semana",
          "fuera_de_sweet_spot": "cuota fuera del rango del mercado",
          "liga_dura": "ligas difíciles (Serie A, MLS, Ligue 1)",
          "america": "🎯 en América (MLS, Liga MX, Brasil, Argentina, Libertadores)"}


def _decide(gap_diff: dict, was_on: bool) -> bool:
    """True = filtro prendido."""
    n, cl = gap_diff.get("n") or 0, gap_diff.get("clusters") or 0
    mean, se = gap_diff.get("mean"), gap_diff.get("se")
    if n < MIN_N or cl < MIN_CLUSTERS or mean is None or se is None:
        return True
    if mean + Z95 * se < 0:
        return False
    if not was_on and mean < 0:
        return False
    return True


def region_evidence(rows: list, region: str) -> dict:
    """Función pura. rows: [{match, league, result, market_prob}] de los
    picks que pasaron la regla. gap_diff de la región: brecha del resto − la
    suya (+ = la región es peor y el filtro ayuda), con el error estándar de
    dos muestras; n y partidos de cada grupo."""
    from src.models.side_bias import region_of
    groups = {True: ([], set()), False: ([], set())}
    for x in rows:
        p = x.get("market_prob")
        if x.get("result") not in ("win", "loss") or p is None or p != p:          # p != p: NaN
            continue
        gaps, matches = groups[region_of(x.get("league")) == region]
        gaps.append((1.0 if x["result"] == "win" else 0.0) - float(p))
        matches.add(x.get("match"))
    (g, gm), (r, rm) = groups[True], groups[False]
    out = {"n": len(g), "clusters": len(gm), "n_rest": len(r), "clusters_rest": len(rm),
           "mean": None, "se": None, "region_gap": None, "rest_gap": None}
    if len(g) < 2 or len(r) < 2:
        return out
    mg, mr = statistics.fmean(g), statistics.fmean(r)
    se = (statistics.variance(g) / len(g) + statistics.variance(r) / len(r)) ** 0.5
    return {**out, "mean": mr - mg, "se": se, "region_gap": mg, "rest_gap": mr}


def _decide_region(gd: dict, was_on: bool) -> bool:
    """True = 🎯 de la región en pausa. Se prende con evidencia (IC 95% entero
    > 0); prendido, se apaga en cuanto deja de perder (media ≤ 0); con pocos
    datos se queda como estaba."""
    thin = (gd.get("mean") is None or gd.get("se") is None
            or (gd.get("n") or 0) < MIN_N_GROUP or (gd.get("clusters") or 0) < MIN_CLUSTERS_GROUP
            or (gd.get("n_rest") or 0) < MIN_N_GROUP or (gd.get("clusters_rest") or 0) < MIN_CLUSTERS_GROUP)
    if thin:
        return was_on
    if was_on:
        return gd["mean"] > 0
    return gd["mean"] - Z95 * gd["se"] > 0


def learn_filters(evidence: dict | None, previous: dict | None = None, region_rows: list | None = None) -> dict:
    """Estado nuevo a partir de la evidencia de rule_evidence y, para los
    filtros por región, de los picks del programa (función pura). Sin
    region_rows (no se pudieron leer), las regiones se quedan como estaban."""
    prev = (previous or {}).get("filters") or {}
    measured = (evidence or {}).get("filters") or {}
    out = {}
    for name in FILTERS:
        gd = (measured.get(name) or {}).get("gap_diff") or {}
        was_on = bool((prev.get(name) or {}).get("on", True))
        out[name] = {"on": _decide(gd, was_on), "mean": gd.get("mean"), "se": gd.get("se"),
                     "n": gd.get("n") or 0, "clusters": gd.get("clusters") or 0,
                     "verdict": gd.get("verdict", VERDICT_THIN)}
    for name in REGION_FILTERS:
        before = prev.get(name) or {}
        was_on = bool(before.get("on", False))
        if region_rows is None:
            out[name] = {**before, "on": was_on, "kind": "region"}
            continue
        gd = region_evidence(region_rows, name)
        out[name] = {**gd, "on": _decide_region(gd, was_on), "kind": "region"}
    return {
        "filters": out,
        "off": [n for n in FILTERS if not out[n]["on"]],
        "paused": [n for n in REGION_FILTERS if out[n]["on"]],
        "evidence_at": (evidence or {}).get("updated_at"),
        "policy": {"min_n": MIN_N, "min_clusters": MIN_CLUSTERS, "z": Z95,
                   "region": {"min_n": MIN_N_GROUP, "min_clusters": MIN_CLUSTERS_GROUP,
                              "window_days": REGION_WINDOW_DAYS}},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def changed(previous: dict | None, state: dict) -> bool:
    prev = previous or {}
    return (set(prev.get("off") or []) != set(state.get("off") or [])
            or set(prev.get("paused") or []) != set(state.get("paused") or []))


def load_filter_state() -> dict:
    return load_state(STATE_KEY) or {}


def load_filter_switches() -> dict:
    """{filtro: prendido}. Lo que falte o no se lea queda prendido."""
    learned = load_filter_state().get("filters") or {}
    return {n: bool((learned.get(n) or {}).get("on", True)) for n in FILTERS}


def paused_regions(engine=None) -> tuple:
    """Las regiones con el 🎯 en pausa, leídas con la conexión de quien
    pregunta (snapshot_picks). Si el estado no se lee, ninguna."""
    import json
    from sqlalchemy import text
    if engine is None:
        from config.database import engine
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM model_state WHERE key = :k"), {"k": STATE_KEY}).first()
        v = row[0] if row else None
        state = (json.loads(v) if isinstance(v, str) else dict(v)) if v is not None else {}
    except Exception as e:
        log.warning(f"⚠️  Filtro por región: estado ilegible ({type(e).__name__}) — el 🎯 sale con la regla de siempre")
        return ()
    learned = state.get("filters") or {}
    return tuple(r for r in REGION_FILTERS if (learned.get(r) or {}).get("on"))


REGION_ROWS_SQL = """
    SELECT match, league, result, market_prob FROM program_picks
    WHERE result IN ('win', 'loss') AND market IS NOT NULL
      AND (reason IS NULL OR reason LIKE 'Región % en pausa%')
      AND match_date >= NOW() - make_interval(days => :d)
"""


def _region_rows() -> list | None:
    """Los picks que pasaron la regla (🎯 o en pausa por región) de la ventana."""
    try:
        import pandas as pd
        from sqlalchemy import text
        from config.database import engine
        return pd.read_sql(text(REGION_ROWS_SQL), engine, params={"d": REGION_WINDOW_DAYS}).to_dict("records")
    except Exception as e:
        log.warning(f"⚠️  Filtro por región: no se pudieron leer los picks ({type(e).__name__}) — "
                    f"las regiones se quedan como estaban")
        return None


def run_filter_learning(evidence: dict | None = None, verbose: bool = True, region_rows: list | None = None) -> dict:
    """Paso semanal: lee la evidencia de esta semana, decide y lo guarda."""
    if evidence is None:
        evidence = load_state(EVIDENCE_KEY)
    if region_rows is None:
        region_rows = _region_rows()
    previous = load_filter_state()
    state = learn_filters(evidence, previous, region_rows)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev_off = set((previous or {}).get("off") or [])
    prev_paused = set((previous or {}).get("paused") or [])
    lines = [b("🎛️ FILTROS DE SELECCIÓN (aprendidos con resultados reales)")]
    for name, node in (state.get("filters") or {}).items():
        if node.get("kind") == "region":
            lines.append(_region_line(name, node, prev_paused))
            continue
        if node.get("mean") is None:
            val = "sin datos"
        else:
            ci = f" ± {Z95 * node['se'] * 100:.1f}" if node.get("se") is not None else ""
            val = f"{node['mean'] * 100:+.1f}pt{ci}"
        status = "PRENDIDO" if node.get("on") else "APAGADO"
        if node.get("on") and name in prev_off:
            status += " (vuelve hoy)"
        elif not node.get("on") and name not in prev_off:
            status += " (desde hoy)"
        lines.append(f"  {LABELS.get(name, name)}: {status} — {val}, "
                     f"n={node.get('n', 0)} ({node.get('clusters', 0)} partidos)")
    lines.append(f"(+ = su grupo sí es peor y el filtro ayuda.) Se apaga solo si su grupo acierta "
                 f"mejor que el resto con n ≥ {MIN_N} en {MIN_CLUSTERS} partidos; "
                 f"el filtro Pinnacle sigue igual.")
    if any((n or {}).get("kind") == "region" for n in (state.get("filters") or {}).values()):
        lines.append(f"(Por región: brecha = acierto − lo que esperaba el mercado, picks que pasan la regla de "
                     f"{REGION_WINDOW_DAYS} días. Se prende si la región pierde contra el resto fuera del margen de "
                     f"error, con {MIN_N_GROUP}+ picks en {MIN_CLUSTERS_GROUP}+ partidos por grupo; se apaga en "
                     f"cuanto deja de perder. Prendido: el pick se ve igual, sin 🎯.)")
    return "\n".join(lines)


def _region_line(name: str, node: dict, prev_paused: set) -> str:
    on = bool(node.get("on"))
    status = "PRENDIDO, 🎯 en pausa" if on else "APAGADO"
    if on and name not in prev_paused:
        status += " (desde hoy)"
    elif not on and name in prev_paused:
        status += " (el 🎯 vuelve hoy)"
    if node.get("region_gap") is None:
        val = f"sin datos (n={node.get('n', 0)})"
    else:
        ci = f" ± {Z95 * node['se'] * 100:.1f}" if node.get("se") is not None else ""
        val = (f"región {node['region_gap'] * 100:+.1f}pt (n={node.get('n', 0)}) contra el resto "
               f"{node['rest_gap'] * 100:+.1f}pt (n={node.get('n_rest', 0)}): diferencia {node['mean'] * 100:+.1f}pt{ci}")
    return f"  {LABELS.get(name, name)}: {status} — {val}"
