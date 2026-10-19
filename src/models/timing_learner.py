"""
src/models/timing_learner.py
============================
¿A qué hora se sostiene la ventaja contra Pinnacle? (19-oct-26)

El filtro Pinnacle del modo recolección pone dinero real cuando la mejor
cuota supera por ≥ 2 pt la probabilidad sin margen de Pinnacle EN ESE
MOMENTO. Pero Pinnacle se sigue moviendo hasta el kickoff (13-19 oct: 2.4 pt
de media con más de 48 h, 1.2 pt en las últimas 6 h), así que una ventaja
vista dos días antes puede no existir al cierre. Este módulo mide, por horas
antes del kickoff, cuánto valían AL CIERRE de Pinnacle las cuotas que
pasaban el filtro, y bloquea para dinero real el horario que pierde.

Datos: odds_history (cada cambio de cuota antes del kickoff, con la mejor
cuota y Pinnacle) en 1X2 y más/menos 2.5, los mercados con precio directo de
Pinnacle. El cierre es la última foto de los 150 min previos al kickoff (la
ventana de closing_quality); un partido sin esa foto no cuenta.

Una señal es (partido, mercado, horario): la primera foto del horario en que
la cuota pasaba el filtro, que es la que la corrida habría apostado. Su
valor es p_pinnacle_cierre × cuota − 1 por unidad: la prueba estándar de
ventaja, que no espera al resultado.

Política (la de los demás learners):
  - veredicto con n ≥ 100 señales en ≥ 30 partidos e IC 95% robusto por
    partido (rule_evidence);
  - un horario se bloquea solo si pierde con significancia (IC entero < 0);
  - bloqueado, vuelve cuando su media deja de ser negativa;
  - con pocos datos no se bloquea nada.
Bloquear nunca apuesta más: la decisión pasa a una corrida más cercana al
partido. Rige en todos los mercados con precio de Pinnacle (1X2 y sus
derivados: empate anulado, doble oportunidad, hándicap ±0.5; y goles 2.5).
"""

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from config.settings import LEARNING_SINCE
from src.features.pinnacle import pinnacle_prob
from src.models.rule_evidence import MIN_CLUSTERS, MIN_N, Z95, VERDICT_THIN, _rounded, cluster_mean, verdict
from src.utils.closing_quality import CLOSING_MAX_LEAD_MIN
from src.utils.min_odds import MIN_EDGE_TO_PLACE
from src.utils.model_state import load_state, save_state

STATE_KEY = "timing_windows"

CLOSE_LEAD_H = CLOSING_MAX_LEAD_MIN / 60.0
# Horas antes del kickoff (lo, hi]. El primero empieza en 0: una corrida
# dentro de la ventana de cierre cae ahí, aunque no se pueda medir.
WINDOWS = (("0-12h", 0.0, 12.0), ("12-24h", 12.0, 24.0),
           ("24-48h", 24.0, 48.0), ("48h+", 48.0, None))
LABELS = {"0-12h": "menos de 12 h", "12-24h": "12 a 24 h",
          "24-48h": "24 a 48 h", "48h+": "más de 48 h"}
MARKET_COLS = {"home_win": "home_odds", "draw": "draw_odds", "away_win": "away_odds",
               "over25": "over25_odds", "under25": "under25_odds"}
PIN_COLS = ("pin_home_odds", "pin_draw_odds", "pin_away_odds",
            "pin_over25_odds", "pin_under25_odds")
_SIGNAL_COLS = ["match_key", "market", "window", "hours", "value"]


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def window_of(hours) -> str | None:
    """Horario de una corrida según las horas que faltan al kickoff."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if not h > 0:
        return None
    for label, lo, hi in WINDOWS:
        if h > lo and (hi is None or h <= hi):
            return label
    return None


def hours_until(kickoff, now=None) -> float | None:
    """Horas desde `now` (UTC) hasta el kickoff. None si la fecha no parsea."""
    try:
        ko = pd.Timestamp(kickoff)
    except (TypeError, ValueError):
        return None
    if pd.isna(ko):
        return None
    ko = ko.tz_localize("UTC") if ko.tzinfo is None else ko.tz_convert("UTC")
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    return (ko - now).total_seconds() / 3600.0


def is_blocked(hours, blocked) -> bool:
    """¿Una apuesta a esta distancia del kickoff cae en un horario bloqueado?"""
    return bool(blocked) and window_of(hours) in blocked


def _odds(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 and f == f else None


def build_signals(snaps: pd.DataFrame, min_edge: float = MIN_EDGE_TO_PLACE) -> pd.DataFrame:
    """Fotos de odds_history → una señal por (partido, mercado, horario)."""
    if snaps is None or snaps.empty:
        return pd.DataFrame(columns=_SIGNAL_COLS)
    d = snaps.copy()
    d["captured_at"] = pd.to_datetime(d["captured_at"], utc=True, errors="coerce")
    d["kickoff"] = pd.to_datetime(d["kickoff"], utc=True, errors="coerce")
    d["hours"] = (d["kickoff"] - d["captured_at"]).dt.total_seconds() / 3600.0
    d = d[d["hours"] > 0].sort_values("captured_at")

    closes = {}
    for _, row in d[d["hours"] <= CLOSE_LEAD_H].groupby("match_key").tail(1).iterrows():
        closes[row["match_key"]] = {m: pinnacle_prob(m, row) for m in MARKET_COLS}

    rows = []
    for _, row in d[d["hours"] > CLOSE_LEAD_H].iterrows():
        close = closes.get(row["match_key"])
        if not close:
            continue
        for market, col in MARKET_COLS.items():
            odds, p_close = _odds(row.get(col)), close.get(market)
            if odds is None or p_close is None:
                continue
            p_now = pinnacle_prob(market, row)
            if p_now is None or p_now - 1.0 / odds < min_edge:
                continue
            rows.append({"match_key": row["match_key"], "market": market,
                         "window": window_of(row["hours"]), "hours": float(row["hours"]),
                         "value": float(p_close) * odds - 1.0})
    if not rows:
        return pd.DataFrame(columns=_SIGNAL_COLS)
    # la primera foto del horario con señal: la que se habría apostado
    return (pd.DataFrame(rows)
            .drop_duplicates(["match_key", "market", "window"], keep="first")
            .reset_index(drop=True))


def _decide(stat: dict, was_blocked: bool) -> bool:
    """Bloqueado si pierde contra el cierre con significancia; ya bloqueado,
    sigue así mientras su media sea negativa. Con pocos datos, nunca."""
    if (stat.get("n", 0) < MIN_N or stat.get("clusters", 0) < MIN_CLUSTERS
            or stat.get("mean") is None or stat.get("se") is None):
        return False
    if stat["mean"] + Z95 * stat["se"] < 0:
        return True
    return was_blocked and stat["mean"] < 0


def learn_timing(snaps: pd.DataFrame, previous: dict | None = None,
                 min_edge: float = MIN_EDGE_TO_PLACE) -> dict:
    """Estado nuevo a partir de las fotos (función pura: no toca la DB)."""
    sig = build_signals(snaps, min_edge)
    prev_blocked = set((previous or {}).get("blocked") or [])
    windows = {}
    for label, lo, hi in WINDOWS:
        s = sig[sig["window"] == label]
        st = cluster_mean(s["value"], s["match_key"])
        st["verdict"] = verdict(st, lower_supports=False)
        st = _rounded(st)
        st["blocked"] = _decide(st, label in prev_blocked)
        windows[label] = st
    return {
        "since": LEARNING_SINCE,
        "min_edge": min_edge,
        "n_signals": int(len(sig)),
        "n_matches": int(sig["match_key"].nunique()) if len(sig) else 0,
        "windows": windows,
        "blocked": [w for w, st in windows.items() if st["blocked"]],
        "policy": {"min_n": MIN_N, "min_clusters": MIN_CLUSTERS, "z": Z95,
                   "close_lead_min": CLOSING_MAX_LEAD_MIN},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def changed(previous: dict | None, state: dict) -> bool:
    return set((previous or {}).get("blocked") or []) != set(state.get("blocked") or [])


def has_verdict(state: dict) -> bool:
    return any(st.get("verdict") != VERDICT_THIN for st in (state.get("windows") or {}).values())


# ─────────────────────────────────────────────────────────────────────────────
# DB y reporte
# ─────────────────────────────────────────────────────────────────────────────

def load_timing_state() -> dict:
    return load_state(STATE_KEY) or {}


def load_blocked_windows() -> set:
    """Horarios bloqueados para dinero real (los que el pipeline conoce)."""
    return {w for w in (load_timing_state().get("blocked") or []) if w in LABELS}


def read_snapshots() -> pd.DataFrame:
    """Solo lectura: fotos de la cohorte de partidos que ya empezaron."""
    from config.database import engine
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('odds_history')")).scalar() is None:
            return pd.DataFrame(columns=["match_key", "captured_at", "kickoff",
                                         *MARKET_COLS.values(), *PIN_COLS])
    return pd.read_sql(text(f"""
        SELECT match_key, captured_at, kickoff, {", ".join(MARKET_COLS.values())},
               {", ".join(PIN_COLS)}
        FROM odds_history
        WHERE kickoff <= NOW() AND captured_at < kickoff
          AND captured_at >= CAST(:since AS timestamptz)
          AND (pin_home_odds IS NOT NULL OR pin_over25_odds IS NOT NULL)
    """), engine, params={"since": LEARNING_SINCE})


def run_timing_learning(verbose: bool = True) -> dict:
    """Paso semanal: aprende, guarda en model_state e imprime."""
    previous = load_timing_state()
    state = learn_timing(read_snapshots(), previous)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev_blocked = set((previous or {}).get("blocked") or [])
    lines = [b("⏱️ HORARIO DE APUESTA (aprendido contra el cierre de Pinnacle)"),
             f"Cuotas que le ganaban a Pinnacle por {state.get('min_edge', MIN_EDGE_TO_PLACE) * 100:.0f} pt: "
             f"lo que valían al cierre ({state.get('n_signals', 0)} señales en "
             f"{state.get('n_matches', 0)} partidos)."]
    for label, st in (state.get("windows") or {}).items():
        if st.get("mean") is None:
            val = "sin datos"
        else:
            ci = f" ± {Z95 * st['se'] * 100:.1f}" if st.get("se") is not None else ""
            val = f"{st['mean'] * 100:+.1f}%{ci}"
        tag = ""
        if st.get("blocked"):
            tag = "  🚫 BLOQUEADO" + (" (desde hoy)" if label not in prev_blocked else "")
        elif label in prev_blocked:
            tag = "  ✅ desbloqueado hoy"
        lines.append(f"  {LABELS.get(label, label):<13} {val}  n={st.get('n', 0)} "
                     f"({st.get('clusters', 0)} partidos) → {st.get('verdict')}{tag}")
    blocked = state.get("blocked") or []
    lines.append("Sin dinero real en: " + (", ".join(LABELS.get(w, w) for w in blocked)
                                           if blocked else "ningún horario"))
    lines.append(f"Se bloquea un horario solo si pierde contra el cierre con n ≥ {MIN_N} en "
                 f"{MIN_CLUSTERS} partidos; vuelve solo cuando deja de perder.")
    return "\n".join(lines)
