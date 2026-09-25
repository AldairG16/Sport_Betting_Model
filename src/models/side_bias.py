"""
src/models/side_bias.py
=======================
Corrección del sesgo del modelo por LADO (local / empate / visitante),
aprendida contra Pinnacle (25-sep-26).

Por qué: medido el 25-sep sobre las candidatas 1X2 próximas, el modelo daba
en promedio +5.1 pt al local, −2.2 al empate y −3.3 al visitante respecto de
Pinnacle (22 casos por lado). Pinnacle no tiene sesgo por lado, así que es
un error sistemático del modelo: 8 de las 10 apuestas pendientes eran del
local, todas 7-13 pt por encima de Pinnacle.

Cómo:
  1. El pipeline registra, en CADA partido con precio de Pinnacle (no solo
     en las candidatas: eso sesgaría la muestra), el 1X2 CRUDO del modelo y
     el de Pinnacle (tabla model_vs_sharp).
  2. El weekly estima el sesgo medio por lado en una ventana reciente
     (WINDOW_DAYS: el modelo cambia), lo encoge hacia 0 según la muestra,
     lo acota y limita su cambio semanal (mismo espíritu que anchor_learner).
  3. El pipeline lo descuenta del 1X2 del modelo y de los mercados que se
     derivan de él (doble oportunidad, empate anulado, hándicap), ANTES del
     ancla. Lo que queda de desvío es opinión del modelo, no un sesgo fijo.
"""

import math
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

STATE_KEY = "side_bias"
SIDES = ("home", "draw", "away")
WINDOW_DAYS = 21          # el modelo cambia (p. ej. el ajuste DC del 25-sep): ventana reciente
MIN_N = 30                # partidos para estimar
SHRINK_N = 60             # a n = 60 el dato pesa la mitad frente a 0
MAX_ABS = 0.08            # tope del sesgo corregido por lado
MAX_STEP = 0.03           # cambio máximo por semana
_AH = re.compile(r"^ah_(home|away)_([+-]?\d+(?:\.\d+)?)$")

TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS model_vs_sharp (
        match      TEXT NOT NULL,
        match_date TIMESTAMPTZ NOT NULL,
        league     TEXT,
        p_home NUMERIC, p_draw NUMERIC, p_away NUMERIC,
        pin_home NUMERIC, pin_draw NUMERIC, pin_away NUMERIC,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (match, match_date)
    )
"""


# ─────────────────────────────────────────────────────────────────────────────
# Aplicación (pipeline) — funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(p: float) -> float:
    return min(max(p, 0.005), 0.995)


def debias(model_probs: dict, bias: dict | None) -> tuple[dict, dict]:
    """
    Descuenta el sesgo por lado del 1X2 del modelo y de sus derivados.
    Devuelve (probabilidades corregidas, cambios aplicados por lado).
    Sin sesgo aprendido (o sin 1X2) devuelve las mismas probabilidades.
    """
    b = {s: float((bias or {}).get(s) or 0.0) for s in SIDES}
    keys = ("home_win", "draw", "away_win")
    if not any(b.values()) or not all(k in model_probs for k in keys):
        return model_probs, {s: 0.0 for s in SIDES}
    old = [float(model_probs[k]) for k in keys]
    raw = [max(p - b[s], 0.005) for p, s in zip(old, SIDES)]
    tot = sum(raw)
    new = [p / tot for p in raw]
    dh, dd, da = (n - o for n, o in zip(new, old))

    out = dict(model_probs)
    out.update(home_win=new[0], draw=new[1], away_win=new[2])
    for k, v in (("dc_1x", new[0] + new[1]), ("dc_x2", new[1] + new[2]), ("dc_12", new[0] + new[2])):
        if k in out:
            out[k] = _clamp(v)
    # empate anulado: se desplaza lo que cambia P(local | no empate)
    dnb_shift = new[0] / (new[0] + new[2]) - old[0] / (old[0] + old[2])
    if "dnb_home" in out:
        out["dnb_home"] = _clamp(out["dnb_home"] + dnb_shift)
    if "dnb_away" in out:
        out["dnb_away"] = _clamp(out["dnb_away"] - dnb_shift)
    # hándicap (línea del local): −0.5 = gana el local; +0.5 = no pierde el
    # local; otras líneas, aproximado por el desplazamiento del empate anulado
    for k in list(out):
        m = _AH.match(k)
        if not m or m.group(1) != "home":
            continue
        line = float(m.group(2))
        home_shift = dh if abs(line + 0.5) < 1e-9 else (-da if abs(line - 0.5) < 1e-9 else dnb_shift)
        out[k] = _clamp(out[k] + home_shift)
        away_key = f"ah_away_{m.group(2)}"
        if away_key in out:
            out[away_key] = _clamp(out[away_key] - home_shift)
    return out, {"home": dh, "draw": dd, "away": da}


def record(match: str, match_date, league, model_probs: dict, pin: dict) -> dict | None:
    """Fila de model_vs_sharp con el 1X2 CRUDO del modelo y el de Pinnacle."""
    if not all(k in model_probs for k in ("home_win", "draw", "away_win")):
        return None
    if not all(k in (pin or {}) for k in SIDES):
        return None
    return {"match": match, "match_date": match_date, "league": league,
            "p_home": float(model_probs["home_win"]), "p_draw": float(model_probs["draw"]),
            "p_away": float(model_probs["away_win"]),
            "pin_home": float(pin["home"]), "pin_draw": float(pin["draw"]),
            "pin_away": float(pin["away"])}


# ─────────────────────────────────────────────────────────────────────────────
# Aprendizaje (weekly) — función pura
# ─────────────────────────────────────────────────────────────────────────────

def learn(df: pd.DataFrame, previous: dict | None = None) -> dict:
    """
    Sesgo medio (modelo − Pinnacle) por lado, encogido hacia 0 según n, con
    cambio semanal ≤ MAX_STEP y acotado a ±MAX_ABS (los dos topes se cumplen
    siempre). Con n < MIN_N se mantiene el anterior. Los tres suman ~0 (los
    dos tríos suman 1); si un tope corta, debias() renormaliza igual.
    """
    prev = {s: float((previous or {}).get(s) or 0.0) for s in SIDES}
    n = int(len(df))
    stats = {}
    if n < MIN_N:
        return {**prev, "n": n, "source": "prior" if not previous else "anterior",
                "stats": stats, "updated_at": datetime.now(timezone.utc).isoformat()}
    target = {}
    for s in SIDES:
        d = pd.to_numeric(df[f"p_{s}"], errors="coerce") - pd.to_numeric(df[f"pin_{s}"], errors="coerce")
        d = d.dropna()
        mean = float(d.mean())
        se = float(d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else None
        stats[s] = {"mean": round(mean, 5), "se": None if se is None else round(se, 5)}
        target[s] = mean * n / (n + SHRINK_N)
    center = sum(target.values()) / 3.0           # ~0; absorbe filas con datos faltantes
    new = {}
    for s in SIDES:
        v = float(np.clip(target[s] - center, prev[s] - MAX_STEP, prev[s] + MAX_STEP))
        new[s] = round(float(np.clip(v, -MAX_ABS, MAX_ABS)), 5)
    return {**new, "n": n, "source": "datos", "stats": stats,
            "policy": {"window_days": WINDOW_DAYS, "min_n": MIN_N, "shrink_n": SHRINK_N,
                       "max_abs": MAX_ABS, "max_step": MAX_STEP},
            "updated_at": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

def persist(records: list) -> int:
    """Upsert de las filas del pipeline (una por partido; la última manda)."""
    rows = [r for r in records if r]
    if not rows:
        return 0
    from config.database import engine
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))
        for r in rows:
            conn.execute(text("""
                INSERT INTO model_vs_sharp
                    (match, match_date, league, p_home, p_draw, p_away, pin_home, pin_draw, pin_away)
                VALUES (:match, :match_date, :league, :p_home, :p_draw, :p_away,
                        :pin_home, :pin_draw, :pin_away)
                ON CONFLICT (match, match_date) DO UPDATE SET
                    p_home = EXCLUDED.p_home, p_draw = EXCLUDED.p_draw, p_away = EXCLUDED.p_away,
                    pin_home = EXCLUDED.pin_home, pin_draw = EXCLUDED.pin_draw,
                    pin_away = EXCLUDED.pin_away, created_at = NOW()
            """), r)
    return len(rows)


def load_side_bias() -> dict:
    from src.utils.model_state import load_state
    return load_state(STATE_KEY) or {}


def read_pairs() -> pd.DataFrame:
    """Modelo vs Pinnacle de la ventana reciente (crea la tabla si falta)."""
    from config.database import engine
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))
    return pd.read_sql(text(f"""
        SELECT * FROM model_vs_sharp
        WHERE match_date >= NOW() - INTERVAL '{WINDOW_DAYS} days'
    """), engine)


def run_side_bias(verbose: bool = True) -> dict:
    """Paso semanal: aprende el sesgo por lado y lo guarda en model_state."""
    from src.utils.model_state import save_state
    df = read_pairs()
    previous = load_side_bias()
    state = learn(df, previous)
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state, previous))
    return state


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    prev = previous or {}
    names = {"home": "local", "draw": "empate", "away": "visitante"}
    lines = [b("⚖️ SESGO POR LADO vs PINNACLE (se descuenta del modelo)"),
             f"Partidos: {state.get('n', 0)} (últimos {WINDOW_DAYS} días) · fuente: {state.get('source')}"]
    for s in SIDES:
        st = (state.get("stats") or {}).get(s, {})
        medido = f"{st['mean'] * 100:+.1f}pt" if st.get("mean") is not None else "—"
        lines.append(f"  {names[s]:<10} medido {medido} → corrección "
                     f"{(prev.get(s) or 0) * 100:+.1f} → {(state.get(s) or 0) * 100:+.1f}pt")
    return "\n".join(lines)
