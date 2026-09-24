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

Se guardan sus cuotas de 1X2 y de más/menos 2.5 (fetch "featured": su
frescura es odds_fetched_at). De ahí se derivan exactamente:
  - empate anulado: P(local | no hay empate) = p_local / (p_local + p_visita),
    la misma definición que usa el modelo (asian_handicap_model.prob_dnb_home);
  - hándicap ±0.5 (la línea embebida es la del local, como en ah_line):
    local −0.5 = gana el local; local +0.5 = local o empate.
Los demás mercados quedan sin referencia (None).
"""

import math
import re

import pandas as pd

from src.features.market_odds import market_probabilities

PINNACLE_KEY = "pinnacle"
PIN_COLS = ("pin_home_odds", "pin_draw_odds", "pin_away_odds",
            "pin_over25_odds", "pin_under25_odds")

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
                for o in mk.get("outcomes", []):
                    if o.get("point") != 2.5:
                        continue
                    side = str(o.get("name", "")).lower()
                    if side == "over":
                        out["pin_over25_odds"] = o.get("price")
                    elif side == "under":
                        out["pin_under25_odds"] = o.get("price")
    return out


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
    ah = _AH.match(m)
    if ah:
        side, line = ah.group(1), float(ah.group(2))
        if abs(line + 0.5) < 1e-9:          # local −0.5: gana el local
            return ph if side == "home" else 1.0 - ph
        if abs(line - 0.5) < 1e-9:          # local +0.5: local o empate
            return ph + px if side == "home" else pa
    return None


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
