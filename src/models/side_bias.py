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

Por región y en goles (23-oct-26). Medido el 23-oct con el modelo del 22-oct
(142 partidos): el local salía +3.9 ±1.1 pt sobre Pinnacle en las 5 grandes,
+1.3 ±0.9 en el resto de Europa y −0.6 ±1.2 en América. Una sola corrección
para todas (+1.1) dejaba a las 5 grandes cargadas al local y corregía a
América al revés. En goles, el modelo daba −1.3 ±0.6 pt en más de 2.5 contra
el promedio de las casas (−3.8 ±1.3 en América) y −2.7 ±0.7 en ambos anotan
contra el mejor precio sin margen: los picks se iban al "menos" sin razón.
  - Cada región (region_of) se separa de lo global según su muestra
    (REGION_SHRINK_N); con menos de MIN_N_REGION partidos usa lo global.
  - Más de 2.5 y ambos anotan se aprenden igual, contra la misma referencia
    que usan los picks (mkt_over25, mkt_btts en model_vs_sharp).
  - Todo lo que lee el modelo de model_vs_sharp (que lo guarda CRUDO) lo
    corrige con program_probs: picks, dashboard y el revisor de PlayDoit.
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
GOALS = ("over25", "btts")  # mercados de goles con sesgo aprendido (su "no" es 1 − p)
GOAL_MAX_ABS = 0.06       # tope en goles
MIN_N_REGION = 15         # partidos de una región para separarla de lo global
REGION_SHRINK_N = 60      # a n = 60 la región pesa la mitad frente a lo global
BIG5 = ("soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one")
_AMERICA = ("usa_", "mexico", "brazil", "argentina", "conmebol", "concacaf")
_ASIA = ("japan", "korea", "china", "afc_")
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
# Para el dashboard (21-oct-26): goles esperados y probabilidades del modelo
# de goles y ambos anotan, para ver qué dice el programa de cada partido.
# league_factor (22-oct-26): el reparto de goles por la diferencia de ligas
# (src/features/clubelo.py) en partidos entre ligas; NULL = sin ajuste. Con
# él se recalibra GAMMA y los picks saben qué partido se analizó ajustado.
# mkt_over25 / mkt_btts (23-oct-26): el mercado sin margen de goles y ambos
# anotan con el que se compara el modelo (el mismo de los picks), para
# aprender su sesgo como el del 1X2.
EXTRA_COLUMNS = ("lam_home", "lam_away", "p_over25", "p_btts", "league_factor", "mkt_over25", "mkt_btts")
ALTER_SQL = "; ".join(f"ALTER TABLE model_vs_sharp ADD COLUMN IF NOT EXISTS {c} NUMERIC"
                      for c in EXTRA_COLUMNS)
# skipped_at (22-oct-26): la última corrida ya no evaluó el partido (poca
# historia, sin Dixon-Coles confiable, sin momios) y su análisis anterior deja
# de valer. Sin esto, el pick, el sesgo por lado y el dashboard usaban el
# análisis de una corrida vieja: el 22-oct, 16 partidos (8 de ellos de mañana,
# uno del modelo con el error del xG) que esa mañana el programa rechazó.
# Un análisis nuevo lo limpia (persist).
SKIP_SQL = "ALTER TABLE model_vs_sharp ADD COLUMN IF NOT EXISTS skipped_at TIMESTAMPTZ"
VALID = "skipped_at IS NULL"
# low_history (24-oct-26): el partido se analizó con poca historia (menos de 5
# partidos o Dixon-Coles por debajo de MIN_DC_WEIGHT). Su pick se ve, marcado,
# pero no aprende el sesgo ni cuenta en la auditoría: está lejos de Pinnacle a
# propósito. NULL = análisis normal.
LOW_SQL = "ALTER TABLE model_vs_sharp ADD COLUMN IF NOT EXISTS low_history TEXT"
LEARNABLE = "skipped_at IS NULL AND low_history IS NULL"


# ─────────────────────────────────────────────────────────────────────────────
# Aplicación (pipeline) — funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(p: float) -> float:
    return min(max(p, 0.005), 0.995)


def region_of(league) -> str:
    """big5 | america | asia | europa (el resto, con las copas de la UEFA)."""
    lg = str(league or "").lower()
    if lg in BIG5:
        return "big5"
    if any(k in lg for k in _AMERICA):
        return "america"
    if any(k in lg for k in _ASIA):
        return "asia"
    return "europa"


def bias_for(state: dict | None, league) -> dict:
    """El sesgo que se descuenta en un partido de `league`: el de su región
    si ya se aprendió, si no el global."""
    st = state or {}
    reg = (st.get("regions") or {}).get(region_of(league)) or {}
    return {k: float(reg.get(k, st.get(k)) or 0.0) for k in SIDES + GOALS if k in reg or k in st}


def debias(model_probs: dict, bias: dict | None) -> tuple[dict, dict]:
    """
    Descuenta el sesgo por lado del 1X2 del modelo y de sus derivados, y el
    de goles (más de 2.5, ambos anotan) si viene en `bias` (bias_for).
    Devuelve (probabilidades corregidas, cambios aplicados por lado y mercado).
    Sin sesgo aprendido (o sin 1X2) devuelve las mismas probabilidades.
    """
    g = {m: float((bias or {}).get(m) or 0.0) for m in GOALS}
    out, shift = _debias_1x2(model_probs, bias)
    for m, no in (("over25", "under25"), ("btts", "btts_no")):
        if g[m] and m in out:
            if out is model_probs:
                out = dict(model_probs)
            new = _clamp(float(out[m]) - g[m])
            shift[m] = new - float(out[m])
            out[m] = new
            if no in out:
                out[no] = 1.0 - new
    return out, shift


def _debias_1x2(model_probs: dict, bias: dict | None) -> tuple[dict, dict]:
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


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def market_goal_refs(row) -> dict:
    """Mercado sin margen de más de 2.5 (promedio de las casas; si no, el mejor
    precio) y de ambos anotan (mejor precio): la misma referencia de los picks
    (program_picks.candidates). row: fila de upcoming_matches (dict o Series)."""
    from src.models.program_picks import devig2
    g = (lambda c: row.get(c) if row is not None else None)
    return {"over25": devig2(g("consensus_over25_odds"), g("consensus_under25_odds"))
            or devig2(g("over25_odds"), g("under25_odds")),
            "btts": devig2(g("btts_yes_odds"), g("btts_no_odds"))}


def program_probs(r, state: dict | None) -> dict:
    """El modelo del programa de una fila de model_vs_sharp (que lo guarda
    CRUDO para aprender el sesgo) con el sesgo de su región ya descontado,
    como lo usa el pipeline: {home_win, draw, away_win, over25, btts}, None
    donde falta el dato. Todo lo que lee el modelo de esa tabla pasa por aquí."""
    g = (lambda c: r.get(c) if isinstance(r, dict) else getattr(r, c, None))
    raw = {"home_win": _num(g("p_home")), "draw": _num(g("p_draw")), "away_win": _num(g("p_away")),
           "over25": _num(g("p_over25")), "btts": _num(g("p_btts"))}
    have = {k: v for k, v in raw.items() if v is not None}
    if state:
        have, _ = debias(have, bias_for(state, g("league")))
    return {k: (have.get(k) if raw[k] is not None else None) for k in raw}


def record(match: str, match_date, league, model_probs: dict, pin: dict,
           lambdas: tuple | None = None, league_factor=None, market_goals: dict | None = None,
           low_history: str | None = None) -> dict | None:
    """Fila de model_vs_sharp con el 1X2 CRUDO del modelo y el de Pinnacle,
    (para el dashboard) goles esperados, más de 2.5 y ambos anotan, el
    factor de liga si el partido se ajustó con ClubElo y el mercado de goles
    y ambos anotan (market_goal_refs) con el que se aprende su sesgo."""
    if not all(k in model_probs for k in ("home_win", "draw", "away_win")):
        return None
    if not all(k in (pin or {}) for k in SIDES):
        return None
    lh, la = lambdas if lambdas else (None, None)
    return {"match": match, "match_date": match_date, "league": league,
            "p_home": float(model_probs["home_win"]), "p_draw": float(model_probs["draw"]),
            "p_away": float(model_probs["away_win"]),
            "pin_home": float(pin["home"]), "pin_draw": float(pin["draw"]),
            "pin_away": float(pin["away"]),
            "lam_home": _num(lh), "lam_away": _num(la),
            "p_over25": _num(model_probs.get("over25")), "p_btts": _num(model_probs.get("btts")),
            "league_factor": _num(league_factor),
            "mkt_over25": _num((market_goals or {}).get("over25")),
            "mkt_btts": _num((market_goals or {}).get("btts")),
            "low_history": low_history or None}


# ─────────────────────────────────────────────────────────────────────────────
# Aprendizaje (weekly) — función pura
# ─────────────────────────────────────────────────────────────────────────────

def _step(target: float, prev: float, cap: float) -> float:
    """Cambio semanal ≤ MAX_STEP y acotado a ±cap (los dos topes se cumplen siempre)."""
    v = float(np.clip(target, prev - MAX_STEP, prev + MAX_STEP))
    return round(float(np.clip(v, -cap, cap)), 5)


def _diff(df: pd.DataFrame, model_col: str, ref_col: str) -> pd.Series:
    if model_col not in df.columns or ref_col not in df.columns:
        return pd.Series(dtype=float)
    return (pd.to_numeric(df[model_col], errors="coerce") - pd.to_numeric(df[ref_col], errors="coerce")).dropna()


def _mean_se(d: pd.Series) -> dict:
    se = float(d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else None
    return {"mean": round(float(d.mean()), 5), "se": None if se is None else round(se, 5), "n": int(len(d))}


def learn(df: pd.DataFrame, previous: dict | None = None) -> dict:
    """
    Sesgo medio (modelo − Pinnacle) por lado, encogido hacia 0 según n, con
    cambio semanal ≤ MAX_STEP y acotado a ±MAX_ABS (los dos topes se cumplen
    siempre). Con n < MIN_N se mantiene el anterior. Los tres suman ~0 (los
    dos tríos suman 1); si un tope corta, debias() renormaliza igual.

    Igual en goles (modelo − mercado de más de 2.5 y ambos anotan, tope
    GOAL_MAX_ABS) y por región: cada una se separa de lo global en
    (su media − la global)·n_r/(n_r + REGION_SHRINK_N), con n_r ≥ MIN_N_REGION
    (si no, lo global); su paso semanal parte de su valor anterior o, la
    primera vez, del global anterior (lo que se le venía aplicando).
    """
    previous = previous or {}
    prev = {s: float(previous.get(s) or 0.0) for s in SIDES}
    goals_prev = {m: float(previous.get(m) or 0.0) for m in GOALS}
    regions_prev = previous.get("regions") or {}
    n = int(len(df))
    stats = {}
    if n < MIN_N:
        keep = {k: previous[k] for k in GOALS + ("regions",) if k in previous}
        return {**prev, **keep, "n": n, "source": "prior" if not previous else "anterior",
                "stats": stats, "updated_at": datetime.now(timezone.utc).isoformat()}
    target, means = {}, {}
    for s in SIDES:
        d = _diff(df, f"p_{s}", f"pin_{s}")
        stats[s] = {k: v for k, v in _mean_se(d).items() if k != "n"}
        means[s] = float(d.mean())
        target[s] = means[s] * n / (n + SHRINK_N)
    center = sum(target.values()) / 3.0           # ~0; absorbe filas con datos faltantes
    new = {s: _step(target[s] - center, prev[s], MAX_ABS) for s in SIDES}

    goal_target = {}
    for m in GOALS:
        d = _diff(df, f"p_{m}", f"mkt_{m}")
        if len(d) < MIN_N:
            new[m] = goals_prev[m]                # se mantiene el anterior
            continue
        stats[m] = _mean_se(d)
        means[m] = float(d.mean())
        goal_target[m] = means[m] * len(d) / (len(d) + SHRINK_N)
        new[m] = _step(goal_target[m], goals_prev[m], GOAL_MAX_ABS)

    reg = (df["league"].map(region_of) if "league" in df.columns
           else pd.Series("europa", index=df.index))
    regions = {}
    for r in sorted(set(reg.unique()) | set(regions_prev)):
        g, rp = df[reg == r], regions_prev.get(r) or {}
        out = {"n": int(len(g)), "measured": {}}
        w = len(g) / (len(g) + REGION_SHRINK_N) if len(g) >= MIN_N_REGION else 0.0
        t = {}
        for s in SIDES:
            d = _diff(g, f"p_{s}", f"pin_{s}")
            if len(d):
                out["measured"][s] = round(float(d.mean()), 5)
            t[s] = target[s] + ((float(d.mean()) - means[s]) * w if w and len(d) else 0.0)
        c = sum(t.values()) / 3.0
        out.update({s: _step(t[s] - c, float(rp.get(s, prev[s])), MAX_ABS) for s in SIDES})
        for m in GOALS:
            if m not in goal_target:                # sin muestra global: se mantiene
                out[m] = float(rp.get(m, goals_prev[m]))
                continue
            d = _diff(g, f"p_{m}", f"mkt_{m}")
            out[f"n_{m}"] = int(len(d))
            if len(d):
                out["measured"][m] = round(float(d.mean()), 5)
            wm = len(d) / (len(d) + REGION_SHRINK_N) if len(d) >= MIN_N_REGION else 0.0
            tm = goal_target[m] + ((float(d.mean()) - means[m]) * wm if wm else 0.0)
            out[m] = _step(tm, float(rp.get(m, goals_prev[m])), GOAL_MAX_ABS)
        regions[r] = out
    return {**new, "regions": regions, "n": n, "source": "datos", "stats": stats,
            "policy": {"window_days": WINDOW_DAYS, "min_n": MIN_N, "shrink_n": SHRINK_N,
                       "max_abs": MAX_ABS, "max_step": MAX_STEP, "goal_max_abs": GOAL_MAX_ABS,
                       "min_n_region": MIN_N_REGION, "region_shrink_n": REGION_SHRINK_N},
            "updated_at": datetime.now(timezone.utc).isoformat()}


def moved(state: dict | None, previous: dict | None) -> bool:
    """La corrección cambió (global, de goles o de alguna región)."""
    st, pv = state or {}, previous or {}
    keys = SIDES + GOALS

    def differ(a, b):
        return any(abs(float(a.get(k) or 0) - float(b.get(k) or 0)) > 1e-9 for k in keys)
    if differ(st, pv):
        return True
    regions = set(st.get("regions") or {}) | set(pv.get("regions") or {})
    return any(differ(bias_for(st, r_league), bias_for(pv, r_league))
               for r_league in (_REGION_LEAGUE[r] for r in regions if r in _REGION_LEAGUE))


# una liga de cada región, para preguntar por región con bias_for
_REGION_LEAGUE = {"big5": "soccer_epl", "america": "soccer_usa_mls",
                  "asia": "soccer_japan_j_league", "europa": "soccer_netherlands_eredivisie"}


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
        conn.execute(text(ALTER_SQL))
        conn.execute(text(SKIP_SQL))
        conn.execute(text(LOW_SQL))
        for r in rows:
            r = {**{c: None for c in EXTRA_COLUMNS}, "low_history": None, **r}
            conn.execute(text("""
                INSERT INTO model_vs_sharp
                    (match, match_date, league, p_home, p_draw, p_away, pin_home, pin_draw, pin_away,
                     lam_home, lam_away, p_over25, p_btts, league_factor, mkt_over25, mkt_btts, low_history)
                VALUES (:match, :match_date, :league, :p_home, :p_draw, :p_away,
                        :pin_home, :pin_draw, :pin_away, :lam_home, :lam_away, :p_over25, :p_btts,
                        :league_factor, :mkt_over25, :mkt_btts, :low_history)
                ON CONFLICT (match, match_date) DO UPDATE SET
                    p_home = EXCLUDED.p_home, p_draw = EXCLUDED.p_draw, p_away = EXCLUDED.p_away,
                    pin_home = EXCLUDED.pin_home, pin_draw = EXCLUDED.pin_draw,
                    pin_away = EXCLUDED.pin_away, lam_home = EXCLUDED.lam_home,
                    lam_away = EXCLUDED.lam_away, p_over25 = EXCLUDED.p_over25,
                    p_btts = EXCLUDED.p_btts, league_factor = EXCLUDED.league_factor,
                    mkt_over25 = EXCLUDED.mkt_over25, mkt_btts = EXCLUDED.mkt_btts,
                    low_history = EXCLUDED.low_history,
                    skipped_at = NULL, created_at = NOW()
            """), r)
    return len(rows)


def mark_skipped(skips: list) -> int:
    """Marca (skipped_at) los partidos que esta corrida ya no evaluó: su
    análisis anterior deja de valer. skips: [{"match", "match_date"}] con el
    mismo nombre y fecha que record()."""
    rows = [s for s in skips if s]
    if not rows:
        return 0
    from config.database import engine
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))
        conn.execute(text(SKIP_SQL))
        conn.execute(text("UPDATE model_vs_sharp SET skipped_at = NOW() "
                          "WHERE match = :match AND match_date = :match_date"), rows)
    return len(rows)


def is_skipped(r) -> bool:
    """La fila de model_vs_sharp es un análisis que la última corrida ya no evaluó."""
    v = r.get("skipped_at") if isinstance(r, dict) else getattr(r, "skipped_at", None)
    return v is not None and not pd.isna(v)


def load_side_bias() -> dict:
    from src.utils.model_state import load_state
    return load_state(STATE_KEY) or {}


def read_pairs() -> pd.DataFrame:
    """Modelo vs Pinnacle de la ventana reciente (crea la tabla si falta)."""
    from config.database import engine
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))
        conn.execute(text(SKIP_SQL))
        conn.execute(text(LOW_SQL))
    # solo partidos desde que el modelo dejó de estar roto (MODEL_SINCE), que
    # la última corrida sí evaluó y con historia suficiente (no los de
    # poca historia: no son la opinión normal del modelo)
    from config.settings import MODEL_SINCE
    return pd.read_sql(text(f"""
        SELECT * FROM model_vs_sharp
        WHERE match_date >= NOW() - INTERVAL '{WINDOW_DAYS} days'
          AND match_date >= CAST(:since AS timestamptz) AND {LEARNABLE}
    """), engine, params={"since": MODEL_SINCE})


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
    goal_names = {"over25": "más de 2.5", "btts": "ambos anotan"}
    for m in GOALS:
        st = (state.get("stats") or {}).get(m, {})
        if st.get("mean") is None and not state.get(m):
            continue
        medido = f"{st['mean'] * 100:+.1f}pt (n={st.get('n')})" if st.get("mean") is not None else "—"
        lines.append(f"  {goal_names[m]:<10} medido {medido} → corrección "
                     f"{(prev.get(m) or 0) * 100:+.1f} → {(state.get(m) or 0) * 100:+.1f}pt")
    region_names = {"big5": "5 grandes", "europa": "resto de Europa", "america": "América", "asia": "Asia"}
    regions = state.get("regions") or {}
    if regions:
        lines.append("Por región (local / empate / visitante · más de 2.5 · ambos anotan), corrección:")
        for r in ("big5", "europa", "america", "asia"):
            if r not in regions:
                continue
            g = regions[r]
            fmt = (lambda k: f"{(g.get(k) or 0) * 100:+.1f}")
            lines.append(f"  {region_names[r]} (n={g.get('n', 0)}): {fmt('home')} / {fmt('draw')} / {fmt('away')}"
                         f" · {fmt('over25')} · {fmt('btts')} pt")
    return "\n".join(lines)


def residuals(df: pd.DataFrame, state: dict | None) -> list:
    """El sesgo que queda DESPUÉS de la corrección (program_probs − referencia),
    global y por región, de cada lado del 1X2, más de 2.5 y ambos anotan:
    [{scope, market, mean, se, n}]. Lo vigila la auditoría del sistema."""
    if df is None or df.empty:
        return []
    refs = {"home_win": "pin_home", "draw": "pin_draw", "away_win": "pin_away",
            "over25": "mkt_over25", "btts": "mkt_btts"}
    rows = []
    for r in df.to_dict("records"):
        p = program_probs(r, state)
        rows.append({"region": region_of(r.get("league")),
                     **{m: (p[m] - _num(r.get(c))) if p[m] is not None and _num(r.get(c)) is not None else None
                        for m, c in refs.items()}})
    d = pd.DataFrame(rows)
    out = []
    for scope, g in [("global", d)] + list(d.groupby("region")):
        for m in refs:
            x = pd.to_numeric(g[m], errors="coerce").dropna()
            if len(x) > 1:
                out.append({"scope": scope, "market": m, "n": int(len(x)), "mean": float(x.mean()),
                            "se": float(x.std(ddof=1) / math.sqrt(len(x)))})
    return out
