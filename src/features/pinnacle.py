"""
src/features/pinnacle.py
========================
Precio de Pinnacle como referencia "sharp" (24-sep-26). SOLO MEDICIÓN: nada
de aquí entra en las probabilidades del modelo ni en los picks.

Por qué: la cuota que guarda el sistema es la MEJOR entre ~28 casas
europeas, y su devig mezcla precios desviados de casas blandas (el booksum
de esos máximos cae bajo 1.00 una de cada cuatro veces). Pinnacle viene en
la misma descarga (región "eu" de The Odds API, sin créditos extra) y es la
casa de referencia de los profesionales: su precio sin margen es el mejor
estimador público de la probabilidad real, y su cierre es el patrón con el
que se mide si alguien tiene ventaja (src/models/sharp_reference.py).

Se guardan sus cuotas de 1X2, su línea principal de goles y la de 2.5 si
es esa (fetch "featured": su frescura es odds_fetched_at). De ahí salen:
  - empate anulado: P(local | no hay empate) = p_local / (p_local + p_visita),
    la misma definición que usa el modelo (asian_handicap_model.prob_dnb_home);
  - hándicap ±0.5 (la línea embebida es la del local, como en ah_line):
    local −0.5 = gana el local; local +0.5 = local o empate;
  - doble oportunidad: 1X = local + empate, etc.;
  - más/menos 2.5: directo si Pinnacle cotiza 2.5; si no (86% de los
    partidos: su línea principal suele ser 2.25, 2.75, 3...), derivado de
    su línea principal con Poisson (over25_from_line). Aproximado, pero del
    mercado más preciso.
Los demás mercados quedan sin referencia (None).
"""

import math
import re

import pandas as pd

from src.features.market_odds import market_probabilities

PINNACLE_KEY = "pinnacle"
PIN_COLS = ("pin_home_odds", "pin_draw_odds", "pin_away_odds",
            "pin_over25_odds", "pin_under25_odds",
            "pin_total_line", "pin_total_over_odds", "pin_total_under_odds")

_AH = re.compile(r"^ah_(home|away)_([+-]?\d+(?:\.\d+)?)$")


def extract_pinnacle(bookmakers, home: str, away: str) -> dict:
    """Cuotas de Pinnacle de un evento de The Odds API (None si no cotiza)."""
    out = dict.fromkeys(PIN_COLS)
    for bk in bookmakers or []:
        if bk.get("key") != PINNACLE_KEY:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") == "h2h":
                for o in mk.get("outcomes", []):
                    name, price = o.get("name"), o.get("price")
                    if name == home:
                        out["pin_home_odds"] = price
                    elif name == away:
                        out["pin_away_odds"] = price
                    elif str(name).lower() == "draw":
                        out["pin_draw_odds"] = price
            elif mk.get("key") == "totals":
                # /odds trae UNA línea por casa (la principal de Pinnacle)
                by_point: dict = {}
                for o in mk.get("outcomes", []):
                    side = str(o.get("name", "")).lower()
                    if side in ("over", "under") and o.get("point") is not None:
                        by_point.setdefault(float(o["point"]), {})[side] = o.get("price")
                for point, sides in by_point.items():
                    if "over" in sides and "under" in sides:
                        out.update(pin_total_line=point, pin_total_over_odds=sides["over"],
                                   pin_total_under_odds=sides["under"])
                        if point == 2.5:
                            out.update(pin_over25_odds=sides["over"], pin_under25_odds=sides["under"])
                        break
    return out


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) if k >= 0 else 0.0


def _poisson_sf(k: int, lam: float) -> float:
    """P(X ≥ k)."""
    return 1.0 - sum(_poisson_pmf(i, lam) for i in range(max(k, 0)))


def over_return(line: float, fair_odds: float, lam: float) -> float:
    """Retorno esperado de 1 unidad al más de `line` (línea asiática) con
    goles totales ~ Poisson(λ): con devolución en líneas enteras y mitad y
    mitad en las de cuarto."""
    k = math.floor(line)
    frac = round(line - k, 2)
    if frac == 0.5:
        return _poisson_sf(k + 1, lam) * fair_odds
    if frac == 0.0:                                  # entera: push si X = k
        return _poisson_sf(k + 1, lam) * fair_odds + _poisson_pmf(k, lam)
    if frac == 0.25:                                 # k y k+0.5: X = k → media devolución
        return _poisson_sf(k + 1, lam) * fair_odds + 0.5 * _poisson_pmf(k, lam)
    if frac == 0.75:                                 # k+0.5 y k+1: X = k+1 → media gana, media push
        return _poisson_sf(k + 2, lam) * fair_odds + _poisson_pmf(k + 1, lam) * (0.5 * fair_odds + 0.5)
    raise ValueError(f"línea no asiática: {line}")


def over25_from_line(line, over_odds, under_odds) -> float | None:
    """
    P(más de 2.5 goles) desde la línea principal de Pinnacle: se quita el
    margen del par, se despeja el λ de Poisson que hace justa la cuota del
    más, y se evalúa P(X ≥ 3). Directo si la línea ya es 2.5.
    """
    o, u = _odds(over_odds), _odds(under_odds)
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if not (o and u) or not math.isfinite(line):
        return None
    io, iu = 1.0 / o, 1.0 / u
    if not 0.95 <= io + iu <= 1.15:
        return None
    q_over = io / (io + iu)
    if line == 2.5:
        return q_over
    fair = 1.0 / q_over
    try:
        from scipy.optimize import brentq
        lam = brentq(lambda x: over_return(line, fair, x) - 1.0, 0.05, 10.0)
    except (ValueError, RuntimeError):
        return None
    return _poisson_sf(3, lam)


def _get(row, col):
    getter = getattr(row, "get", None)
    return getter(col) if callable(getter) else getattr(row, col, None)


def _odds(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 1.0 else None


def pinnacle_probs(row) -> dict:
    """Probabilidades sin margen de Pinnacle: home/draw/away (Shin, como el
    1X2 del sistema) y over25 (proporcional). Solo las que se pueden calcular."""
    out = {}
    h, d, a = (_odds(_get(row, c)) for c in ("pin_home_odds", "pin_draw_odds", "pin_away_odds"))
    if h and d and a:
        ph, px, pa = market_probabilities(h, d, a)
        if ph is not None:
            out.update(home=float(ph), draw=float(px), away=float(pa))
    o, u = _odds(_get(row, "pin_over25_odds")), _odds(_get(row, "pin_under25_odds"))
    if o and u:
        io, iu = 1.0 / o, 1.0 / u
        if 0.95 <= io + iu <= 1.15:
            out["over25"] = io / (io + iu)
    if "over25" not in out:
        derived = over25_from_line(_get(row, "pin_total_line"), _get(row, "pin_total_over_odds"),
                                   _get(row, "pin_total_under_odds"))
        if derived is not None:
            out["over25"] = derived
    return out


def pinnacle_prob(market, row) -> float | None:
    """Probabilidad sin margen de Pinnacle para un mercado del sistema."""
    p = pinnacle_probs(row)
    m = str(market or "")
    if m in ("home_win", "draw", "away_win"):
        return p.get({"home_win": "home", "draw": "draw", "away_win": "away"}[m])
    if m in ("over25", "under25"):
        po = p.get("over25")
        return None if po is None else (po if m == "over25" else 1.0 - po)
    if "home" not in p:
        return None
    ph, px, pa = p["home"], p["draw"], p["away"]
    if m in ("dnb_home", "dnb_away"):
        return ph / (ph + pa) if m == "dnb_home" else pa / (ph + pa)
    if m in ("dc_1x", "dc_x2", "dc_12"):
        return {"dc_1x": ph + px, "dc_x2": px + pa, "dc_12": ph + pa}[m]
    ah = _AH.match(m)
    if ah:
        side, line = ah.group(1), float(ah.group(2))
        if abs(line + 0.5) < 1e-9:          # local −0.5: gana el local
            return ph if side == "home" else 1.0 - ph
        if abs(line - 0.5) < 1e-9:          # local +0.5: local o empate
            return ph + px if side == "home" else pa
    return None


def no_reference_group(market) -> str:
    """Grupo de reactivación de un mercado sin referencia de Pinnacle:
    'sinref:<familia>' (btts, corners_cards, halftime...). Sin precio sharp,
    su dinero real espera a la evidencia de sus candidatas contra el cierre
    (scripts/clv_gate.run_shadow_reactivation)."""
    from src.models.anchor_learner import market_family
    return "sinref:" + (market_family(str(market or "")) or "otros")


def pinnacle_quote_for(market, row) -> tuple[float | None, object]:
    """(probabilidad de Pinnacle, cuándo se descargó) desde una fila de
    upcoming_matches. Sin hora conocida no sirve de cierre: (None, None)."""
    p = pinnacle_prob(market, row)
    at = _get(row, "odds_fetched_at")
    try:
        if at is not None and pd.isna(at):
            at = None
    except (TypeError, ValueError):
        pass
    if p is None or at is None:
        return None, None
    return float(p), at
