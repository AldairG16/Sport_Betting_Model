"""
Calibration Monitor
===================
Mide si las probabilidades del modelo son REALES o inventadas.

Problema que resuelve:
  Si el modelo dice "65% de probabilidad" y el equipo gana solo el 48%
  de las veces que el modelo dice 65%, hay un sesgo sistemático (sobreconfianza).
  Sin detectar esto, los "edges" del modelo son parcialmente ficticios.

Métricas:
  - Brier Score: MSE entre probabilidades y resultados (0=perfecto, 0.25=azar)
  - Calibration Buckets: para cada rango de prob, ¿qué % real gana?
  - Calibration Factor: actual_win_rate / predicted_win_rate por mercado
    → factor > 1: modelo subconfiado (gana más de lo que predice)
    → factor < 1: modelo sobreconfiado (gana menos de lo que predice)

Los factores se guardan en config/calibration_factors.json y se
aplican automáticamente en el pipeline para corregir el sesgo.

Mínimo recomendado: 50 bets por mercado para que los factores sean estables.
"""

import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import numpy as np

sys.path.append(str(Path(__file__).parent.parent.parent))

from config.database import engine

# MEJORA #5 — Isotonic regression (sklearn) para calibración no-paramétrica.
# Aprende un mapping monótono pred_prob → real_prob a partir de los datos.
# Captura curvas no-lineales de mis-calibración (cuando el bias varía con el
# rango de probabilidad). Solo se entrena con n >= MIN_BETS_FOR_ISOTONIC para
# evitar overfit con samples chicos.
try:
    from sklearn.isotonic import IsotonicRegression
    ISOTONIC_AVAILABLE = True
except ImportError:
    ISOTONIC_AVAILABLE = False

MIN_BETS_FOR_ISOTONIC = 30   # umbral conservador (curva con n<30 puede ser ruido)

CALIBRATION_FILE = Path(__file__).parent.parent.parent / "config" / "calibration_factors.json"

# MIN_BETS bajado de 50 → 10 (06-may-26): con suavizado bayesiano (prior=30)
# y clamp adaptativo, podemos usar samples chicos sin sobre-corregir.
# Con n=10 reales: factor pesa 10/(10+30)=25% del cómputo, prior pesa 75%.
MIN_BETS_FOR_CALIBRATION = 10

# Mercados base. Los `ah_*` se manejan agrupados (ver MEJORA #4 abajo).
MARKETS = ["home_win", "draw", "away_win", "over25", "under25",
           "btts", "btts_no", "dnb_home", "dnb_away",
           "dc_1x", "dc_x2", "dc_12",
           "ah_home_fav", "ah_home_pk", "ah_home_dog",
           "ah_away_fav", "ah_away_pk", "ah_away_dog"]

# MEJORA #1 — Suavizado bayesiano
# El prior actúa como "BETS_PRIOR fantasmas con factor=1.0" agregadas al sample.
# Con n=8 reales y prior=30: el factor real pesa 8/(8+30)=21% del cómputo.
# Con n=200 reales: el factor real pesa 200/230=87% (casi puro empírico).
BETS_PRIOR = 30

# MEJORA #2 — Clamp adaptativo por sample size
# Con poca muestra, clamp estricto (no sobre-corregir por ruido).
# Con muchas, clamp permisivo (corregir bias real grande como BTTS = 0.41).
def _adaptive_clamp(n: int) -> tuple[float, float]:
    if n >= 100: return (0.30, 1.70)   # corrección fuerte (bias estructural)
    if n >= 30:  return (0.50, 1.50)   # corrección media
    return (0.75, 1.30)                # corrección suave (default conservador)

# Defaults globales (legacy, usados por validación externa)
CAL_FACTOR_MIN = 0.30
CAL_FACTOR_MAX = 1.70

# MEJORA #4 — Agrupar AH por tipo
# Cada `ah_home_-0.5`, `ah_home_-0.8`, etc. tiene sample chico individual.
# Agruparlos por "tipo de línea" da sample suficiente para calibrar.
AH_GROUP_BOUNDARIES = {
    "fav": (-99.0, -0.25),   # handicap NEGATIVO fuerte (favorito da puntos)
    "pk":  (-0.25,  0.25),   # pickem / cuasi-empate
    "dog": ( 0.25, 99.0),    # handicap POSITIVO (underdog recibe puntos)
}

def _fit_isotonic_knots(probs, outcomes) -> dict | None:
    """
    Entrena IsotonicRegression y devuelve los knots serializables (X, Y).
    Devuelve None si no hay suficientes datos o sklearn no está disponible.
    """
    if not ISOTONIC_AVAILABLE or len(probs) < MIN_BETS_FOR_ISOTONIC:
        return None
    try:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(np.asarray(probs, dtype=float),
                np.asarray(outcomes, dtype=float))
        # Los X_thresholds_ / y_thresholds_ definen la step function aprendida
        x = iso.X_thresholds_.astype(float).tolist()
        y = iso.y_thresholds_.astype(float).tolist()
        return {"x": x, "y": y}
    except Exception:
        return None


def _apply_isotonic(prob: float, knots: dict | None) -> float:
    """
    Aplica la curva isotónica guardada como knots {x: [...], y: [...]}.
    Si knots es None o inválido, devuelve prob original.
    """
    if not knots or "x" not in knots or "y" not in knots:
        return float(prob)
    x = np.asarray(knots["x"], dtype=float)
    y = np.asarray(knots["y"], dtype=float)
    if len(x) == 0 or len(y) == 0:
        return float(prob)
    # Interpolación lineal con clamp en los extremos (igual que IsotonicRegression
    # con out_of_bounds="clip").
    p = float(np.clip(prob, x.min(), x.max()))
    return float(np.interp(p, x, y))


def _ah_group(market: str) -> str | None:
    """
    Mapea 'ah_home_-0.5' → 'ah_home_fav'.
    Devuelve None si no es un mercado AH parametrizado.
    """
    if not (market.startswith("ah_home_") or market.startswith("ah_away_")):
        return None
    side = "ah_home" if market.startswith("ah_home_") else "ah_away"
    try:
        line = float(market.rsplit("_", 1)[-1])
    except ValueError:
        return None
    for tag, (lo, hi) in AH_GROUP_BOUNDARIES.items():
        if lo <= line < hi:
            return f"{side}_{tag}"
    return None

# Ventana temporal para calibración: solo los últimos N días.
# Evita que apuestas viejas (temporadas previas, modelo anterior) contaminen
# el factor actual — si el modelo mejoró, las bets viejas sesgan hacia abajo.
# Subido 60→90 (sprint 1 #2) para que by_league tenga más muestra por mercado.
CALIBRATION_WINDOW_DAYS = 90

# MEJORA #2 (Sprint 1) — umbral RELAJADO para mercados within-league.
# El umbral global (MIN_BETS_FOR_CALIBRATION=10) protege contra ruido en el
# factor global, pero dentro de una liga específica raramente tendremos
# >=10 bets resueltas de un mercado puntual (ej. away_win en La Liga: 4 bets).
# Bayesian smoothing con prior=30 ya estabiliza factores con n=5.
# Si una liga tiene n<5 en un mercado, no se guarda y cae al global.
MIN_BETS_BY_LEAGUE_MARKET = 5

# Ligas internacionales que SIEMPRE merecen calibración propia aunque
# tengan poca muestra (su distribución difiere brutalmente del global).
# Se mantienen como lista forzosa.
LEAGUES_NEEDING_OWN_CALIBRATION = [
    "soccer_fifa_world_cup",
    "soccer_uefa_euro",
    "soccer_conmebol_copa_america",
    "soccer_concacaf_gold_cup",
    "soccer_afcon",
    "soccer_afc_asian_cup",
]

# MEJORA #2 (Sprint 1) — Descubrimiento DINÁMICO de ligas para by_league
# El bias de away_win es muy distinto por liga: La Liga / Ligue 1 sobre-predicen
# +35pp, mientras Bundesliga / EFL sub-predicen. Antes solo calibrábamos
# ligas internacionales; ahora cualquier liga con >= MIN_BETS_BY_LEAGUE
# bets totales en la ventana se calibra por sí misma. El pipeline aplica
# precedencia: factor por liga (si existe) → factor global → 1.0.
MIN_BETS_BY_LEAGUE = 25       # mínimo TOTAL de bets en la liga (todos mercados)
                              # Si una liga no llega, cae al factor global.
                              # Bayesian smoothing con prior=30 ya protege
                              # los mercados específicos con n<10 dentro de la liga.


# ─────────────────────────────────────────────────────────────
# CARGA / GUARDADO
# ─────────────────────────────────────────────────────────────

_CAL_CACHE: dict | None = None
_CAL_CACHE_MTIME: float = 0.0


def load_calibration_factors() -> dict:
    """
    Carga los factores de calibración guardados.
    Si el archivo no existe, retorna factores neutros (1.0).
    Usa caché en memoria invalidada por mtime para evitar lecturas de disco
    repetidas (apply_calibration lo llama N_markets × N_partidos por ejecución).
    """
    global _CAL_CACHE, _CAL_CACHE_MTIME
    if CALIBRATION_FILE.exists():
        try:
            mtime = CALIBRATION_FILE.stat().st_mtime
            if _CAL_CACHE is not None and mtime == _CAL_CACHE_MTIME:
                return _CAL_CACHE
            with open(CALIBRATION_FILE, "r") as f:
                _CAL_CACHE = json.load(f)
            _CAL_CACHE_MTIME = mtime
            return _CAL_CACHE
        except Exception:
            pass

    return _neutral_factors()


def _neutral_factors() -> dict:
    return {
        market: {"factor": 1.0, "brier": None, "n_bets": 0, "win_rate_actual": None}
        for market in MARKETS
    }


def _save_calibration_factors(factors: dict):
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # tz-aware UTC: evita ambigüedad cuando el script corre en distintas zonas
    factors["updated_at"]    = datetime.now(timezone.utc).isoformat()
    factors["window_days"]   = CALIBRATION_WINDOW_DAYS
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(factors, f, indent=2)


def get_calibration_factor(market: str, league: str | None = None) -> float:
    """
    Retorna el factor de calibración para un mercado específico.
    Usado en el pipeline para ajustar la probabilidad antes de calcular edge.

    Args:
        market: clave del mercado (home_win, draw, ah_home_-0.5, etc.)
        league: opcional — sport_key de la liga. Si se provee y existe un
                factor específico de esa liga (`by_league.<league>.<market>`),
                ese tiene precedencia sobre el factor global. Esto permite
                calibración independiente para Mundial / Euro sin contaminar
                la calibración global de ligas regulares.

    Returns:
        float — 1.0 si no hay suficientes datos (clamp adaptativo por n)

    MEJORA #4: Para mercados `ah_home_-0.5`, `ah_home_-0.8`, etc. busca
    primero el factor agrupado (`ah_home_fav`/`ah_home_pk`/`ah_home_dog`)
    y cae al factor exacto si existe.
    """
    factors = load_calibration_factors()

    # Resolver candidatos: el mercado exacto + grupo AH si aplica
    candidates = [market]
    grp = _ah_group(market)
    if grp is not None:
        candidates.append(grp)   # fallback agrupado

    # 1) Factor específico por liga (si el caller pasa `league`)
    # Umbral RELAJADO dentro de liga: MIN_BETS_BY_LEAGUE_MARKET (5) en vez
    # del global (10). Bayesian smoothing protege contra ruido.
    if league:
        by_league = factors.get("by_league", {})
        league_node = by_league.get(league, {})
        for cand in candidates:
            league_data = league_node.get(cand)
            if isinstance(league_data, dict):
                n = int(league_data.get("n_bets", 0) or 0)
                if n >= MIN_BETS_BY_LEAGUE_MARKET:
                    return float(league_data.get("factor", 1.0))

    # 2) Factor global por mercado (exact → grupo)
    for cand in candidates:
        data = factors.get(cand, {})
        if isinstance(data, dict) and data.get("factor") is not None:
            n = int(data.get("n_bets", 0) or 0)
            if n >= MIN_BETS_FOR_CALIBRATION:
                return float(data.get("factor", 1.0))

    return 1.0


def apply_calibration(prob: float, market: str, league: str | None = None) -> float:
    """
    MEJORA #5 — aplica calibración completa: isotónica si está disponible
    (sample suficiente), si no cae al factor escalar.

    Esta es la función PREFERIDA para el pipeline. Reemplaza el patrón:
        prob *= get_calibration_factor(market, league)

    Por el más expresivo:
        prob = apply_calibration(prob, market, league)

    Returns:
        prob calibrada en [0.01, 0.99]
    """
    if not isinstance(prob, (int, float)) or not (0 < prob < 1):
        return float(prob) if prob is not None else 0.5

    factors = load_calibration_factors()

    candidates = [market]
    grp = _ah_group(market)
    if grp is not None:
        candidates.append(grp)

    # Buscar isotonic knots (precedencia: liga específica → global)
    # Umbrales: dentro de liga = MIN_BETS_BY_LEAGUE_MARKET (5),
    #           global       = MIN_BETS_FOR_CALIBRATION    (10).
    def _resolve_node(scope: dict, min_n: int) -> dict | None:
        for cand in candidates:
            data = scope.get(cand)
            if isinstance(data, dict):
                n = int(data.get("n_bets", 0) or 0)
                if n >= min_n:
                    return data
        return None

    node = None
    if league:
        by_league = factors.get("by_league", {})
        node = _resolve_node(by_league.get(league, {}), MIN_BETS_BY_LEAGUE_MARKET)
    if node is None:
        node = _resolve_node(
            {k: v for k, v in factors.items()
             if k not in ("updated_at", "window_days", "by_league")},
            MIN_BETS_FOR_CALIBRATION,
        )

    if node is None:
        return float(np.clip(prob, 0.01, 0.99))

    # 1) Si hay isotonic knots → usar (más expresivo que factor escalar)
    iso_knots = node.get("isotonic")
    if isinstance(iso_knots, dict) and iso_knots.get("x") and iso_knots.get("y"):
        return float(np.clip(_apply_isotonic(prob, iso_knots), 0.01, 0.99))

    # 2) Fallback: factor escalar
    factor = float(node.get("factor", 1.0))
    return float(np.clip(prob * factor, 0.01, 0.99))


# ─────────────────────────────────────────────────────────────
# CÓMPUTO
# ─────────────────────────────────────────────────────────────

def compute_calibration(min_bets: int = MIN_BETS_FOR_CALIBRATION,
                        verbose: bool = True) -> dict:
    """
    Lee bets_history, calcula Brier Score y factores de calibración por mercado.

    Args:
        min_bets: mínimo de bets por mercado para calcular factor (evita ruido)
        verbose:  imprimir reporte en consola

    Returns:
        dict con factores por mercado y métricas
    """
    try:
        # Filtro de ventana temporal: sólo últimos CALIBRATION_WINDOW_DAYS días.
        # Esto desacopla la calibración del modelo *actual* del histórico lejano
        # (bets generadas con thresholds/features distintos).
        df = pd.read_sql(f"""
            SELECT market, probability, result, league
            FROM bets_history
            WHERE result IN ('win', 'loss')
              AND probability IS NOT NULL
              AND probability > 0
              AND probability < 1
              AND match_date >= NOW() - INTERVAL '{CALIBRATION_WINDOW_DAYS} days'
        """, engine)
    except Exception as e:
        print(f"❌ calibration_monitor: no se pudo leer bets_history: {e}")
        return _neutral_factors()

    if df.empty:
        if verbose:
            print("⚠️  Sin bets resueltas para calibración")
        return _neutral_factors()

    # Resultado binario: 1 = win, 0 = loss
    df["outcome"] = (df["result"] == "win").astype(int)

    # MEJORA #4 — añadir columna `market_grp` para agregar AH variantes:
    df["market_grp"] = df["market"].apply(lambda m: _ah_group(m) or m)

    factors = {}
    summary_lines = [
        "\n📐 CALIBRACIÓN DEL MODELO (suavizado bayesiano + clamp adaptativo)",
        f"{'Mercado':<14} {'N':>5} {'Brier':>7} {'Pred%':>7} {'Real%':>7} "
        f"{'Raw':>6} {'Factor':>7} {'Estado':>14}",
        "─" * 75,
    ]

    # Lista de mercados a calibrar: los base + grupos AH detectados en datos.
    markets_to_calibrate = list(MARKETS)
    detected_groups = sorted(set(df["market_grp"].unique()) - set(MARKETS))
    # Solo agrega los que existen en MARKETS (evita basura como nombres mal-formed)
    markets_to_calibrate += [m for m in detected_groups if m in MARKETS]
    # Dedupe preservando orden
    seen = set()
    markets_to_calibrate = [x for x in markets_to_calibrate if not (x in seen or seen.add(x))]

    for market in markets_to_calibrate:
        # Para grupos AH (ah_home_fav, etc.) usa la columna market_grp.
        # Para otros, usa la columna market exacto.
        if market.startswith("ah_") and market.split("_")[-1] in {"fav","pk","dog"}:
            mdf = df[df["market_grp"] == market].copy()
        else:
            mdf = df[df["market"] == market].copy()

        if len(mdf) < min_bets:
            factors[market] = {
                "factor":           1.0,
                "brier":            None,
                "n_bets":           len(mdf),
                "win_rate_actual":  None,
                "win_rate_pred":    None,
            }
            summary_lines.append(
                f"{market:<14} {len(mdf):>5}   {'—':>7}   {'—':>7}   {'—':>7} "
                f"  {'—':>6}   {'1.000':>7}  datos insuf."
            )
            continue

        prob    = mdf["probability"].values
        outcome = mdf["outcome"].values

        brier = float(np.mean((prob - outcome) ** 2))
        win_rate_pred   = float(np.mean(prob))
        win_rate_actual = float(np.mean(outcome))

        raw_factor = (win_rate_actual / win_rate_pred) if win_rate_pred > 0 else 1.0

        # MEJORA #1 — Suavizado bayesiano (en vez de "smoothing = n/500").
        # Mezcla raw_factor con prior=1.0 ponderado por BETS_PRIOR.
        n = len(mdf)
        factor_raw_smooth = (1.0 * BETS_PRIOR + raw_factor * n) / (BETS_PRIOR + n)

        # MEJORA #2 — Clamp adaptativo según sample size
        cmin, cmax = _adaptive_clamp(n)
        factor = round(max(cmin, min(cmax, factor_raw_smooth)), 4)

        if abs(factor - 1.0) < 0.03:
            estado = "calibrado ✅"
        elif factor > 1.0:
            estado = f"subconf +{(factor-1)*100:.0f}%"
        else:
            estado = f"sobreconf -{(1-factor)*100:.0f}%"

        # MEJORA #5 — entrenar isotonic regression si hay sample suficiente
        iso_knots = _fit_isotonic_knots(prob, outcome)

        factors[market] = {
            "factor":           factor,
            "brier":            round(brier, 5),
            "n_bets":           n,
            "win_rate_actual":  round(win_rate_actual, 4),
            "win_rate_pred":    round(win_rate_pred,   4),
            "raw_factor":       round(raw_factor, 4),
            "clamp":            [cmin, cmax],
            "isotonic":         iso_knots,   # null si n < 30 o sklearn no disp.
        }

        iso_tag = " 📈iso" if iso_knots else ""
        summary_lines.append(
            f"{market:<14} {n:>5} {brier:>7.4f} "
            f"{win_rate_pred*100:>6.1f}% {win_rate_actual*100:>6.1f}% "
            f"{raw_factor:>6.3f} {factor:>7.3f}  {estado}{iso_tag}"
        )

    # ── Brier global ─────────────────────────────────────────────────────
    df["outcome_int"] = (df["result"] == "win").astype(int)
    global_brier = float(np.mean((df["probability"] - df["outcome_int"]) ** 2))
    summary_lines += [
        "─" * 60,
        f"Brier global: {global_brier:.4f}  "
        f"(ref: aleatorio=0.250, perfecto=0.000)",
        f"Total bets analizadas: {len(df)}",
    ]

    if verbose:
        print("\n".join(summary_lines))

    # ── Calibración por liga (Mejora #2 — Sprint 1) ────────────────────────
    # Antes: solo ligas internacionales (LEAGUES_NEEDING_OWN_CALIBRATION).
    # Ahora: descubrimiento dinámico — toda liga con n >= MIN_BETS_BY_LEAGUE
    # se calibra individualmente. Esto resuelve el sesgo brutal de away_win
    # en La Liga/Ligue 1/Eredivisie (+35pp pred vs real, ROI -100%) que el
    # factor global no podía capturar porque el promedio mezclaba ligas
    # con sesgo opuesto (Bundesliga -67pp, ROI +245%).
    # Mejora #4 (ampliada): elegimos liga si
    #   (a) está en LEAGUES_NEEDING_OWN_CALIBRATION (forzosa), o
    #   (b) tiene >= MIN_BETS_BY_LEAGUE total, o
    #   (c) tiene >= MIN_BETS_BY_LEAGUE_MARKET en al menos UN mercado/grupo
    #       (captura ligas chicas con concentración en un solo mercado, ej.
    #        una liga muy de overs cuya muestra global es 18 pero tiene 12
    #        bets de over25 → conviene calibrar over25 ahí).
    leagues_observed = sorted(
        set(df[df["league"].notna()]["league"].unique())
    )
    league_keys_to_calibrate = []
    for lk in leagues_observed:
        ldf_chk = df[df["league"] == lk]
        n_in_league = int(len(ldf_chk))
        if lk in LEAGUES_NEEDING_OWN_CALIBRATION:
            league_keys_to_calibrate.append(lk); continue
        if n_in_league >= MIN_BETS_BY_LEAGUE:
            league_keys_to_calibrate.append(lk); continue
        # Caso (c): muestra concentrada en un mercado o grupo
        # Usa market_grp (que ya agrupa AH variantes) para no perder los AH.
        max_per_mkt = ldf_chk["market_grp"].value_counts().max() if not ldf_chk.empty else 0
        if max_per_mkt >= MIN_BETS_BY_LEAGUE_MARKET:
            league_keys_to_calibrate.append(lk)

    if verbose and league_keys_to_calibrate:
        print(f"\n📐 Calibración por liga: {len(league_keys_to_calibrate)} ligas "
              f"con n >= {MIN_BETS_BY_LEAGUE} bets")

    by_league: dict = {}
    for league_key in league_keys_to_calibrate:
        ldf = df[df["league"] == league_key].copy()
        # market_grp ya viene de df pero asegurar columna por si .copy() lo perdió
        if "market_grp" not in ldf.columns:
            ldf["market_grp"] = ldf["market"].apply(lambda m: _ah_group(m) or m)
        # NOTA: ya NO filtramos por len(ldf) < min_bets aquí — el descubrimiento
        # de league_keys_to_calibrate ya garantiza suficiente muestra (caso a/b/c).
        # El gate per-mercado (MIN_BETS_BY_LEAGUE_MARKET) se aplica abajo.

        league_factors: dict = {}
        for market in markets_to_calibrate:
            if market.startswith("ah_") and market.split("_")[-1] in {"fav","pk","dog"}:
                mdf = ldf[ldf["market_grp"] == market]
            else:
                mdf = ldf[ldf["market"] == market]
            # Mejora #2: dentro de liga, usar umbral relajado (n=5) ya que el
            # bias estructural por liga (away_win La Liga +35pp) es brutal y
            # amerita corrección incluso con muestra chica. Bayesian smoothing
            # protege: con n=5 reales y prior=30, el factor pesa 14% real / 86% prior.
            if len(mdf) < MIN_BETS_BY_LEAGUE_MARKET:
                continue

            prob    = mdf["probability"].values
            outcome = (mdf["result"] == "win").astype(int).values

            brier = float(np.mean((prob - outcome) ** 2))
            wr_pred   = float(np.mean(prob))
            wr_actual = float(np.mean(outcome))

            raw_factor = wr_actual / wr_pred if wr_pred > 0 else 1.0
            n = len(mdf)
            factor_smooth = (1.0 * BETS_PRIOR + raw_factor * n) / (BETS_PRIOR + n)
            cmin, cmax = _adaptive_clamp(n)
            factor = round(max(cmin, min(cmax, factor_smooth)), 4)

            iso_knots = _fit_isotonic_knots(prob, outcome)
            league_factors[market] = {
                "factor":          factor,
                "brier":           round(brier, 5),
                "n_bets":          n,
                "win_rate_actual": round(wr_actual, 4),
                "win_rate_pred":   round(wr_pred, 4),
                "raw_factor":      round(raw_factor, 4),
                "clamp":           [cmin, cmax],
                "isotonic":        iso_knots,
            }

        if league_factors:
            by_league[league_key] = league_factors
            if verbose:
                markets_str = ", ".join(f"{m}={f['factor']:.2f}"
                                         for m, f in league_factors.items())
                print(f"\n📐 Calibración liga «{league_key}»:")
                print(f"   {markets_str}")

    if by_league:
        factors["by_league"] = by_league

    # ── Guardar ───────────────────────────────────────────────────────────
    _save_calibration_factors(factors)

    return factors


# ─────────────────────────────────────────────────────────────
# ALERTA TELEGRAM
# ─────────────────────────────────────────────────────────────

def check_calibration_alert(factors: dict) -> str | None:
    """
    Retorna un mensaje de alerta si la calibración es preocupante.
    Usado en el ciclo semanal para enviar alerta a Telegram si es necesario.

    Returns:
        str con el mensaje de alerta, o None si todo está bien
    """
    bad_markets = []

    for market, data in factors.items():
        if not isinstance(data, dict):
            continue
        factor = data.get("factor", 1.0)
        n      = data.get("n_bets", 0)
        brier  = data.get("brier")

        if n < MIN_BETS_FOR_CALIBRATION:
            continue

        # Factor muy lejos de 1.0 → problema sistemático
        if factor < 0.82 or factor > 1.20:
            bad_markets.append(
                f"  {market}: factor={factor:.2f}  Brier={brier:.4f}"
            )

    if not bad_markets:
        return None

    return (
        "⚠️ <b>ALERTA CALIBRACIÓN</b>\n\n"
        "Los siguientes mercados tienen sesgo sistemático:\n"
        + "\n".join(bad_markets)
        + "\n\n<i>Considera revisar los thresholds o reentrenar el modelo.</i>"
    )


# ─────────────────────────────────────────────────────────────
# REPORTE RÁPIDO
# ─────────────────────────────────────────────────────────────

def calibration_summary_for_telegram() -> str:
    """Genera un resumen de calibración para incluir en el reporte semanal."""
    factors = load_calibration_factors()

    lines = ["— <b>CALIBRACIÓN</b> —"]
    has_data = False

    for market in MARKETS:
        data = factors.get(market, {})
        if not isinstance(data, dict):
            continue
        factor = data.get("factor", 1.0)
        brier  = data.get("brier")
        n      = data.get("n_bets", 0)

        if n < MIN_BETS_FOR_CALIBRATION:
            continue

        has_data = True
        emoji = "✅" if abs(factor - 1.0) < 0.05 else ("📈" if factor > 1.0 else "📉")
        from scripts.notify_telegram import MARKET_LABELS
        label = MARKET_LABELS.get(market, market)
        brier_str = f" Brier={brier:.3f}" if brier else ""
        lines.append(f"  {emoji} {label}: {factor:.2f}x{brier_str} (n={n})")

    if not has_data:
        return ""

    return "\n".join(lines)


if __name__ == "__main__":
    factors = compute_calibration(verbose=True)
    alert = check_calibration_alert(factors)
    if alert:
        print(f"\n{alert}")
    else:
        print("\n✅ Calibración dentro de rangos aceptables")
