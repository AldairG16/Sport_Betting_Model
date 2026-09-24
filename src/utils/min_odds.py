"""
src/utils/min_odds.py
=====================
Cuota mínima a la que un pick todavía vale la pena apostarlo, en el formato
en que la muestra PlayDoit: americano (+128 / -140).

Por qué (24-sep-26): la cuota que muestra el sistema es la MEJOR entre
~20 casas europeas (The Odds API). El dueño apuesta en PlayDoit, que no
cubre ninguna API de cuotas (ni The Odds API, ni Odds-API.io, ni OddsPapi)
y casi nunca iguala esa cuota. Con la probabilidad del pick, la cuota
mínima es la que deja un edge ≥ MIN_EDGE_TO_PLACE:

    p − 1/cuota ≥ min_edge   ⇔   cuota ≥ 1 / (p − min_edge)

Es el mismo umbral con el que la revalidación pre-kickoff decide mantener
una bet cuya cuota empeoró (scripts/revalidate_pending_bets.KEEP_EDGE).

Cuota americana (24-sep-26: el dueño no lee cuotas decimales):
    +128 → apuestas 100 para ganar 128   (decimal 2.28)
    -140 → apuestas 140 para ganar 100   (decimal 1.714)
Un número MÁS ALTO siempre paga más: -120 es mejor que -140, +150 mejor
que +128, y cualquier + es mejor que cualquier -. Por eso la regla se
lee igual en los dos signos: "apuesta si paga X o mejor".
"""

import math

MIN_EDGE_TO_PLACE = 0.02

# Una línea para el pie de los mensajes: la dirección de "mejor" con
# números negativos es lo que confunde.
HOW_TO_READ = "Número más alto = paga más: -120 es mejor que -140 y +150 mejor que +130."


def _floor_decimal(prob, min_edge: float) -> float | None:
    """Cuota decimal EXACTA (sin redondear) que deja justo min_edge."""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if not (0 < p < 1) or p - min_edge <= 0:
        return None
    return 1.0 / (p - min_edge)


def min_odds(prob, min_edge: float = MIN_EDGE_TO_PLACE) -> float | None:
    """Cuota mínima decimal, redondeada HACIA ARRIBA a 2 decimales (apostar
    justo en el número mostrado nunca deja menos edge que el umbral). None
    si la probabilidad no alcanza para ningún precio."""
    d = _floor_decimal(prob, min_edge)
    return None if d is None else math.ceil(100.0 * d - 1e-9) / 100.0


def min_american(prob, min_edge: float = MIN_EDGE_TO_PLACE) -> int | None:
    """La PEOR cuota americana que todavía deja edge ≥ min_edge, redondeada
    del lado seguro: a ese número el edge alcanza; un punto peor, ya no."""
    d = _floor_decimal(prob, min_edge)
    if d is None:
        return None
    if d >= 2.0:
        return math.ceil(100.0 * (d - 1.0) - 1e-9)
    n = math.floor(100.0 / (d - 1.0) + 1e-9)
    return 100 if n <= 100 else -n          # -100 y +100 son el mismo precio


def to_american(decimal) -> int | None:
    """Cuota decimal → americana, al entero más cercano (para mostrar un
    precio, no para decidir: para decidir está min_american)."""
    try:
        d = float(decimal)
    except (TypeError, ValueError):
        return None
    if not (d > 1.0 and math.isfinite(d)):
        return None
    if d >= 2.0:
        return int(round(100.0 * (d - 1.0)))
    n = int(round(100.0 / (d - 1.0)))
    return 100 if n <= 100 else -n


def american_to_decimal(american: int) -> float:
    a = float(american)
    return 1.0 + a / 100.0 if a > 0 else 1.0 + 100.0 / -a


def fmt_american(american) -> str:
    """+128 / -140 (con guion normal, como lo escribe PlayDoit); "—" si no hay."""
    if american is None:
        return "—"
    a = int(american)
    return f"+{a}" if a > 0 else str(a)


def _american_exact(d: float) -> float:
    return 100.0 * (d - 1.0) if d >= 2.0 else -100.0 / (d - 1.0)


def rule_examples(american: int) -> tuple[int, int | None]:
    """Un precio que SÍ sirve y uno que NO, redondeados a múltiplos de 5
    (como los publica una casa). Cada uno queda ≥3% del umbral en decimal,
    así que nunca caen del lado equivocado."""
    d = american_to_decimal(american)
    better = math.ceil(_american_exact(d * 1.03) / 5.0) * 5
    worse_d = d / 1.03
    worse = math.floor(_american_exact(worse_d) / 5.0) * 5 if worse_d > 1.01 else None
    return (100 if better == -100 else better), worse


def playdoit_line(prob, min_edge: float = MIN_EDGE_TO_PLACE) -> str:
    """Línea de Telegram con la regla para PlayDoit; "" si no hay precio."""
    a = min_american(prob, min_edge)
    if a is None:
        return ""
    better, worse = rule_examples(a)
    ejemplo = f"{fmt_american(better)} ✅" + (f" · {fmt_american(worse)} ❌" if worse is not None else "")
    return f"🟢 PlayDoit: apuesta si paga {fmt_american(a)} o mejor ({ejemplo})"
