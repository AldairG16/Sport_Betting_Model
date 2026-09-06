import pandas as pd


# =========================
# EDGE CALCULATION
# =========================

def calculate_edges(prob, odds):

    if odds is None or odds <= 1:
        return None, None

    implied = 1 / odds

    edge_market = prob - implied
    edge_ev = (prob * odds) - 1

    # 🔥 CAP EDGE (evita locuras)
    edge_market = max(min(edge_market, 0.25), -0.25)
    edge_ev = max(min(edge_ev, 0.50), -0.50)

    return edge_market, edge_ev


# =========================
# VALUE BETS (PRO)
# =========================

def find_value_bets(probabilities, odds):

    bets = []

    for market, prob in probabilities.items():

        odd = odds.get(market)

        if odd is None or pd.isna(odd) or odd <= 1:
            continue

        # 🔥 FILTRO PROBABILIDAD (reduce ruido)
        if prob < 0.05 or prob > 0.95:
            continue

        edge_market, edge_ev = calculate_edges(prob, odd)

        if edge_market is None:
            continue

        # =========================
        # FILTROS PRO
        # =========================

        # 🔥 mínimo edge real
        if edge_market < 0.02:
            continue

        # 🔥 EV mínimo
        if edge_ev < 0:
            continue

        # 🔥 penalización odds altas
        odds_penalty = 1.0
        if odd > 5:
            odds_penalty = 0.85
        if odd > 8:
            odds_penalty = 0.7

        # 🔥 score combinado (CLAVE)
        score = edge_ev * prob * odds_penalty

        bets.append({
            "market": market,
            "probability": prob,
            "odds": odd,
            "edge": edge_ev,
            "edge_market": edge_market,
            "score": score
        })

    # 🔥 ordenar por score, no solo edge
    bets = sorted(bets, key=lambda x: x["score"], reverse=True)

    return bets


# =========================
# KELLY (PRO)
# =========================

def kelly_stake(prob, odds, bankroll=100, kelly_fraction=0.25, max_bet_pct=0.02,
                market=None, league=None):
    """
    Kelly PRO (más conservador)

    Mejoras:
    - penaliza odds altas
    - reduce varianza
    - evita sobre-apostar
    - **MEJORA #14 (Sprint 2)**: si se pasan `market` y/o `league`, ajusta
      `kelly_fraction` según el CLV histórico (cargado desde el cache).
      CLV positivo  → aumenta fracción (la línea apretó a tu favor → la
      apuesta era +EV real → escalar más).
      CLV negativo  → reduce fracción (la línea se movió en contra →
      potencial sesgo / value falso → encoger stake).
    """

    if prob is None or odds is None or odds <= 1:
        return 0

    # ── MEJORA #14: ajuste por CLV trailing ─────────────────────────
    # Solo si market está dado y hay datos en cache. Bayesian smoothing
    # con prior=20 protege contra muestras chicas.
    if market is not None:
        try:
            kelly_fraction = _adjusted_kelly_fraction(
                base=kelly_fraction, market=market, league=league
            )
        except Exception:
            pass   # si CLV cache no carga, usar fraction default

    b = odds - 1
    q = 1 - prob

    kelly = (prob * b - q) / b

    if kelly <= 0:
        return 0

    # 🔥 penalización odds altas
    if odds > 5:
        kelly *= 0.8
    if odds > 8:
        kelly *= 0.6

    # 🔥 Kelly fraccional
    kelly *= kelly_fraction

    # 🔥 cap riesgo
    kelly = min(kelly, max_bet_pct)

    stake = bankroll * kelly

    return round(stake, 2)


# ─────────────────────────────────────────────────────────────────────────
# CLV-driven Kelly fraction (MEJORA #14)
# ─────────────────────────────────────────────────────────────────────────
# CLV (Closing Line Value) mide cuánto se movió la línea desde que apostamos
# hasta el cierre. CLV positivo = la línea apretó a nuestro favor = la
# apuesta era genuinamente +EV. CLV es el predictor más robusto de ROI a
# largo plazo (más que el WR, más que el ROI corto).
#
# Lógica del ajuste:
#   • CLV > +1.5%  → boost  kelly_fraction × 1.20  (alta confianza)
#   • CLV < -1.5%  → reduce kelly_fraction × 0.60  (alarma)
#   • Bayesian smoothing con prior=20 — un mercado con n=5 pesa solo 20%.
#   • Cap final: kelly_fraction queda en [0.10, 0.35] (nunca apaga, nunca
#     escala más de 40% del default 0.25).

import json
import os
import time
from pathlib import Path

_CLV_CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "clv_cache.json"
_CLV_PRIOR_N    = 20             # prior para Bayesian smoothing
_CLV_CACHE_TTL  = 6 * 3600       # 6 horas — re-leer DB
_CLV_BOUND      = (0.10, 0.35)   # clamp final del kelly_fraction
_CLV_THRESH     = 0.015          # ±1.5% CLV para boost/reduce

_clv_cache_mem: dict | None = None
_clv_cache_loaded_at: float = 0


def _load_clv_cache() -> dict:
    """Carga el cache de CLV por (market, league). Refresh cada 6h."""
    global _clv_cache_mem, _clv_cache_loaded_at
    now = time.time()
    if _clv_cache_mem is not None and (now - _clv_cache_loaded_at) < _CLV_CACHE_TTL:
        return _clv_cache_mem
    if not _CLV_CACHE_FILE.exists():
        _clv_cache_mem = {}
        _clv_cache_loaded_at = now
        return _clv_cache_mem
    try:
        with open(_CLV_CACHE_FILE, "r") as f:
            _clv_cache_mem = json.load(f)
        _clv_cache_loaded_at = now
    except Exception:
        _clv_cache_mem = {}
        _clv_cache_loaded_at = now
    return _clv_cache_mem


def _adjusted_kelly_fraction(base: float, market: str, league: str | None) -> float:
    """
    Ajusta kelly_fraction según CLV histórico del (market, league).

    Precedencia:
      1. (market, league) específico si n >= 5
      2. (market, *)      global  si n >= 10
      3. base sin cambios

    Bayesian smoothing: clv_smooth = (0 * prior + clv_raw * n) / (prior + n)
    """
    cache = _load_clv_cache()
    if not cache:
        return base

    by_market = cache.get("by_market", {})
    node = None

    # 1) Por liga
    if league:
        ml = by_market.get(market, {}).get("by_league", {}).get(league)
        if isinstance(ml, dict) and ml.get("n", 0) >= 5:
            node = ml

    # 2) Global por mercado
    if node is None:
        m = by_market.get(market, {})
        if isinstance(m, dict) and m.get("n", 0) >= 10:
            node = m

    if node is None:
        return base

    n        = int(node.get("n", 0))
    clv_raw  = float(node.get("avg_clv", 0.0) or 0.0)
    # Smooth hacia 0 (CLV neutro = sin info)
    clv_smooth = (0.0 * _CLV_PRIOR_N + clv_raw * n) / (_CLV_PRIOR_N + n)

    if   clv_smooth >  _CLV_THRESH: factor = 1.20    # CLV bueno → escalar
    elif clv_smooth < -_CLV_THRESH: factor = 0.60    # CLV malo → encoger
    else:                           factor = 1.00    # neutro

    adjusted = base * factor
    return float(min(_CLV_BOUND[1], max(_CLV_BOUND[0], adjusted)))


def refresh_clv_cache() -> dict:
    """
    Lee bets_history y refresca data/clv_cache.json. Llamado por el ciclo
    weekly. Read-only sobre la DB; escribe solo el cache local.

    Estructura del cache:
        {
          "by_market": {
            "home_win": {"n": 125, "avg_clv": 0.0123,
                         "by_league": {
                           "soccer_brazil_campeonato": {"n": 24, "avg_clv": -0.004},
                           ...
                         }},
            ...
          },
          "updated_at": "2026-05-07T..."
        }
    """
    try:
        # Import diferido — config.database puede tardar y no queremos
        # cargarla en cada `import betting_engine`.
        from config.database import engine
        import pandas as pd
        from datetime import datetime

        df = pd.read_sql("""
            SELECT market, league, odds, closing_odds, result
            FROM bets_history
            WHERE result IN ('win','loss')
              AND closing_odds IS NOT NULL
              AND closing_odds > 0
              AND match_date >= NOW() - INTERVAL '120 days'
        """, engine)
    except Exception as e:
        return {"error": str(e)}

    if df.empty:
        return {"by_market": {}, "updated_at": None}

    df["clv"] = df["odds"] / df["closing_odds"] - 1
    by_market: dict = {}
    for mkt, sub in df.groupby("market"):
        per_league: dict = {}
        for lg, lsub in sub.groupby("league"):
            if len(lsub) < 5:
                continue
            per_league[str(lg)] = {
                "n":       int(len(lsub)),
                "avg_clv": round(float(lsub["clv"].mean()), 5),
            }
        by_market[str(mkt)] = {
            "n":         int(len(sub)),
            "avg_clv":   round(float(sub["clv"].mean()), 5),
            "by_league": per_league,
        }

    out = {
        "by_market":  by_market,
        "updated_at": datetime.utcnow().isoformat(),
    }
    _CLV_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CLV_CACHE_FILE, "w") as f:
        json.dump(out, f, indent=2)

    # Invalidar el cache en memoria
    global _clv_cache_mem, _clv_cache_loaded_at
    _clv_cache_mem = out
    _clv_cache_loaded_at = time.time()
    return out