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
"""

from datetime import datetime, timezone

from src.models.rule_evidence import MIN_CLUSTERS, MIN_N, Z95, VERDICT_THIN
from src.models.rule_evidence import STATE_KEY as EVIDENCE_KEY
from src.utils.model_state import load_state, save_state

STATE_KEY = "filter_switches"

FILTERS = ("entre_semana", "fuera_de_sweet_spot", "liga_dura")
LABELS = {"entre_semana": "partidos entre semana",
          "fuera_de_sweet_spot": "cuota fuera del rango del mercado",
          "liga_dura": "ligas difíciles (Serie A, MLS, Ligue 1)"}


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


def learn_filters(evidence: dict | None, previous: dict | None = None) -> dict:
    """Estado nuevo a partir de la evidencia de rule_evidence (función pura)."""
    prev = (previous or {}).get("filters") or {}
    measured = (evidence or {}).get("filters") or {}
    out = {}
    for name in FILTERS:
        gd = (measured.get(name) or {}).get("gap_diff") or {}
        was_on = bool((prev.get(name) or {}).get("on", True))
        out[name] = {"on": _decide(gd, was_on), "mean": gd.get("mean"), "se": gd.get("se"),
                     "n": gd.get("n") or 0, "clusters": gd.get("clusters") or 0,
                     "verdict": gd.get("verdict", VERDICT_THIN)}
    return {
        "filters": out,
        "off": [n for n in FILTERS if not out[n]["on"]],
        "evidence_at": (evidence or {}).get("updated_at"),
        "policy": {"min_n": MIN_N, "min_clusters": MIN_CLUSTERS, "z": Z95},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def changed(previous: dict | None, state: dict) -> bool:
    return set((previous or {}).get("off") or []) != set(state.get("off") or [])


def load_filter_state() -> dict:
    return load_state(STATE_KEY) or {}


def load_filter_switches() -> dict:
    """{filtro: prendido}. Lo que falte o no se lea queda prendido."""
    learned = load_filter_state().get("filters") or {}
    return {n: bool((learned.get(n) or {}).get("on", True)) for n in FILTERS}


def run_filter_learning(evidence: dict | None = None, verbose: bool = True) -> dict:
    """Paso semanal: lee la evidencia de esta semana, decide y lo guarda."""
    if evidence is None:
        evidence = load_state(EVIDENCE_KEY)
    previous = load_filter_state()
    state = learn_filters(evidence, previous)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev_off = set((previous or {}).get("off") or [])
    lines = [b("🎛️ FILTROS DE SELECCIÓN (aprendidos con resultados reales)")]
    for name, node in (state.get("filters") or {}).items():
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
    return "\n".join(lines)
