"""
Cadena de decision determinista, pura y sin dependencias externas.

Extraida de `run_prediction_pipeline()` (src/pipeline/prediction_pipeline.py,
segmento ~1290-1600) para que la secuencia
    filtro de edge minimo -> sizing Kelly -> topes de portfolio
pueda ejecutarse SIN base de datos, SIN settings globales y SIN red.

Ese aislamiento es lo que hace demostrable que una remediacion "no cambio
ninguna apuesta": el fixture dorado se reproduce por `decide_bets()`, la misma
funcion que llama produccion.

Contrato de un bet (dict). Se conservan al menos estas llaves de entrada a
salida:
    match, market, side, odds, prob, edge, edge_market, stake

La probabilidad del modelo se lee de `prob` o, si no esta, de `probability`
(el nombre que usa produccion porque es la columna de bets_history). Aceptar
las dos evita duplicar la llave en el dict que termina en `save_bets()`.

⚠️  `edge` es `edge_ev = prob*odds - 1` (inflado por odds altas).
    `edge_market` = `prob - 1/odds` es el edge REAL.
    Todo filtro se aplica sobre `edge_market` — nunca sobre `edge`.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

__all__ = [
    "apply_min_edge_filter",
    "size_stakes",
    "apply_portfolio_caps",
    "decide_bets",
]

# Umbral de concentracion por mercado y penalizacion aplicada al mercado
# dominante. Valores verbatim del pipeline original (no son configurables:
# cambiarlos altera el fixture dorado).
_CONCENTRATION_LIMIT = 0.60
_CONCENTRATION_MIN_STAKE = 5
_CONCENTRATION_PENALTY = 0.75


def _copy_bet(bet: dict[str, Any]) -> dict[str, Any]:
    """Copia superficial: `decide_bets` nunca muta la lista de entrada.

    Sin esto, llamar dos veces con el mismo input aplicaria la penalizacion
    de concentracion dos veces y el resultado dejaria de ser determinista.
    """
    return dict(bet)


def _real_edge(bet: dict[str, Any]) -> float:
    """El edge real (`edge_market`), con fallback a `edge` si no existe."""
    return bet.get("edge_market", bet["edge"])


def _prob(bet: dict[str, Any]) -> float:
    """La probabilidad del modelo: `prob`, o `probability` en produccion."""
    if "prob" in bet:
        return bet["prob"]
    return bet["probability"]


def apply_min_edge_filter(
    bets: list[dict[str, Any]],
    min_edge_by_market: dict[str, float],
    min_edge_default: float,
    ah_group: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Descarta los bets cuyo edge REAL no alcanza el umbral de su mercado.

    Orden de resolucion del umbral (identico al original, ver el FIX del
    11-may-26 en prediction_pipeline.py):
      1) lookup directo `min_edge_by_market[market]`
      2) si no hay, se mapea el mercado AH parametrizado (`ah_home_-0.5`) a su
         grupo (`ah_home_fav`) via `ah_group` y se busca ese grupo
      3) si tampoco, `min_edge_default`

    Sin el paso 2 los mercados AH caian silenciosamente al default y dejaban
    pasar bets con win rate del 27%.
    """
    kept: list[dict[str, Any]] = []
    for bet in bets:
        mkt = bet["market"]
        edge_real = _real_edge(bet)

        min_edge = min_edge_by_market.get(mkt)
        if min_edge is None:
            grp = ah_group(mkt)
            if grp is not None:
                min_edge = min_edge_by_market.get(grp, min_edge_default)
            else:
                min_edge = min_edge_default

        if edge_real < min_edge:
            continue

        kept.append(_copy_bet(bet))
    return kept


def size_stakes(
    bets: list[dict[str, Any]],
    bankroll: float,
    kelly_fn: Callable[..., float],
) -> list[dict[str, Any]]:
    """Asigna `stake` a cada bet delegando en `kelly_fn`.

    `kelly_fn` se invoca con la misma firma que
    `src.models.betting_engine.kelly_stake`, de modo que produccion pueda
    pasar esa funcion tal cual:

        kelly_fn(prob, odds, bankroll=..., market=..., league=...)
    """
    sized: list[dict[str, Any]] = []
    for bet in bets:
        out = _copy_bet(bet)
        out["stake"] = kelly_fn(
            _prob(out),
            out["odds"],
            bankroll=bankroll,
            market=out["market"],
            league=out.get("league", ""),
        )
        sized.append(out)
    return sized


def apply_portfolio_caps(
    bets: list[dict[str, Any]],
    bankroll: float,
    max_total_pct: float,
) -> list[dict[str, Any]]:
    """Topes de cartera: exposicion total y concentracion por mercado.

    Copia verbatim del bloque de prediction_pipeline.py (~1566-1597),
    incluyendo la posicion exacta de cada `round(x, 2)` — los stakes se
    comparan byte a byte contra el fixture dorado.
    """
    capped = [_copy_bet(b) for b in bets]
    if not capped or bankroll <= 0:
        return capped

    total_stake = sum(b["stake"] for b in capped)
    max_total = bankroll * max_total_pct

    # ── Concentracion por mercado ─────────────────────────────────────
    market_stakes: dict[str, float] = {}
    for b in capped:
        mkt = b["market"]
        market_stakes[mkt] = market_stakes.get(mkt, 0) + b["stake"]

    dominant_mkt = max(market_stakes, key=market_stakes.get)
    dominant_stake = market_stakes[dominant_mkt]
    concentration = dominant_stake / total_stake if total_stake > 0 else 0

    # ── Calcular factor de escala ─────────────────────────────────────
    scale = 1.0

    if total_stake > max_total:
        scale = min(scale, max_total / total_stake)

    if concentration > _CONCENTRATION_LIMIT and total_stake > _CONCENTRATION_MIN_STAKE:
        # Penalizar el mercado dominante para reducir concentracion
        penalty = _CONCENTRATION_PENALTY
        for b in capped:
            if b["market"] == dominant_mkt:
                b["stake"] = round(b["stake"] * penalty, 2)
        total_stake = sum(b["stake"] for b in capped)
        if total_stake > max_total:
            scale = min(scale, max_total / total_stake)

    if scale < 1.0:
        for b in capped:
            b["stake"] = round(b["stake"] * scale, 2)

    return capped


def decide_bets(
    scored: Iterable[dict[str, Any]],
    *,
    bankroll: float,
    min_edge_by_market: dict[str, float],
    min_edge_default: float,
    ah_group: Callable[[str], str | None],
    kelly_fn: Callable[..., float],
    max_total_pct: float,
    post_size: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Cadena completa: filtro de edge -> sizing Kelly -> topes de portfolio.

    Pura y determinista respecto de sus argumentos: no muta `scored` y dos
    llamadas con la misma entrada devuelven exactamente la misma salida.

    `post_size` es un gancho OPCIONAL que corre entre el sizing y los topes de
    cartera. Existe porque produccion mete dos pasos ahi (ajuste de correlacion
    por partido y filtro de bets sospechosas) que dependen del slate completo y
    no del contrato puro de este modulo. Sin el gancho, produccion tendria que
    recomponer la cadena a mano y el fixture dorado dejaria de probar la misma
    secuencia que corre de verdad. El fixture no pasa `post_size`, asi que su
    linea base sigue midiendo filtro -> sizing -> topes sin intermediarios.
    """
    bets = apply_min_edge_filter(
        list(scored), min_edge_by_market, min_edge_default, ah_group
    )
    bets = size_stakes(bets, bankroll, kelly_fn)
    if post_size is not None:
        bets = post_size(bets)
    bets = apply_portfolio_caps(bets, bankroll, max_total_pct)
    return bets
