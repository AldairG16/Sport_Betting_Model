"""
src/models/program_picks.py
===========================
El pick del programa en cada partido: una apuesta real, guardada y medida.

21-oct-26: el pick se calculaba al vuelo y no quedaba en ningún lado; desde
entonces se guarda (snapshot_picks) y se liquida (resolve_picks).

22-oct-26, regla nueva. El dueño, sobre los partidos de Champions: "no
siempre es gana local o gana visitante… está la opción de ambos anotan o
2.5 goles o 3.5, también están los córners o tarjetas", y quiere que el pick
sea "lo más real para ver las tendencias" aunque no se juegue dinero. La
regla vieja (el resultado del 1X2 donde el programa más se separaba del
mercado) perdió: 155 de 452, −38.85u. Mientras más se separaba, más perdía
(3-5 pts: 49% contra 44% que esperaba el mercado; más de 20 pts: 25% contra
28%). Ahora, entre todos los mercados con precio real:

  - el programa tiene que inclinarse por esa apuesta: su favorito del 1X2,
    o 50% o más en un mercado de dos (lo que de verdad cree que pasará);
  - ve entre 3 y 8 pts más que el mercado (MIN_GAP, MAX_GAP). Con más, casi
    siempre es un error del programa (datos que faltan, la liga, las
    alineaciones), no una ventaja;
  - el momio estimado de PlayDoit está entre MIN_ODDS y MAX_ODDS;
  - el mercado no se movió 3 pts o más en contra desde que el programa
    analizó el partido (MOVED_AGAINST): el Bayern–Arsenal del 21-oct, Pinnacle
    pasó del 46% al 40% por la rotación y el programa, que no usa
    alineaciones, siguió en 50%;
  - si es un partido entre ligas distintas (Champions, Europa League…), el
    programa lo analizó con la fuerza de cada liga (model_vs_sharp trae
    league_factor; src/features/clubelo.py): sin ese ajuste sobrevalora a
    quien golea en una liga débil (Viking 2.3 goles por partido en Noruega
    contra 1.6 del Aston Villa en la Premier).
De lo que pasa, el de mayor diferencia. Tiros al arco no tiene precio en
ninguna fuente: no es pick.

22-oct-26, pick en todos los partidos. El dueño: "quiero que todos tengan
picks no importa si se perdió o ganó, quiero saber para dónde tiende tu
aprendizaje". La regla ya no esconde el pick: lo marca. Si ninguna apuesta
pasa la regla, el pick es, de lo que el programa cree, donde más se separa
del mercado, guardado con el motivo por el que no la pasa (reason; NULL = la
pasa). Sin momios guardados, el 1X2 contra el Pinnacle de model_vs_sharp:
cuenta acierto, no unidades. Solo un partido que el programa no analizó se
queda sin pick.
"""

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger
from src.utils.min_odds import min_american, to_american

log = get_logger(__name__)

MIN_GAP, MAX_GAP = 0.03, 0.08
MOVED_AGAINST = 0.03
MIN_ODDS, MAX_ODDS = 1.40, 3.80
# Promedio de las casas / mejor momio en mercados de dos (más/menos 2.5: 0.961,
# medido el 22-oct-26): el precio de PlayDoit donde solo hay mejor momio
TWO_WAY_AVG_RATIO = 0.96
RHO = -0.09                  # el del ajuste Dixon-Coles semanal
SNAPSHOT_HOURS = 72          # los mismos 3 días que muestra el dashboard
CROSS_LEAGUE = ("uefa_champs", "uefa_europa", "conference", "libertadores", "sudamericana",
                "concacaf_champions")

# (lado, columna de model_vs_sharp, columna de Pinnacle, mejor momio, promedio, mercado, nombre)
SIDES = (("home", "p_home", "pin_home", "home_odds", "consensus_home_odds", "home_win", "Gana el local"),
         ("draw", "p_draw", "pin_draw", "draw_odds", "consensus_draw_odds", "draw", "Empate"),
         ("away", "p_away", "pin_away", "away_odds", "consensus_away_odds", "away_win", "Gana el visitante"))

PICKS_DDL = """
    CREATE TABLE IF NOT EXISTS program_picks (
        id SERIAL PRIMARY KEY,
        match TEXT NOT NULL, match_date TIMESTAMPTZ NOT NULL, league TEXT, display TEXT,
        market TEXT NOT NULL, label TEXT, model_prob NUMERIC, market_prob NUMERIC,
        lean BOOLEAN, min_us INT, best_odds NUMERIC, playdoit_odds NUMERIC, value BOOLEAN,
        picked_at TIMESTAMPTZ DEFAULT NOW(),
        result TEXT, profit NUMERIC, resolved_at TIMESTAMPTZ,
        UNIQUE (match, match_date)
    )
"""
# source: 'reconstruido' = hecho después con lo guardado antes del partido
# (backfill_picks); NULL = guardado en su momento. family: Ganador, Goles,
# Ambos anotan, Córners, Tarjetas. reason: por qué el pick no pasa la regla
# (NULL = la pasa). Sin pick (el programa no se inclina por nada con
# precio): market NULL y reason. lineup: NULL = el pick todavía no vio la
# alineación; "confirmada" (y quién trae 4+ fuera) = se recalculó con ella.
PICKS_ALTERS = ("ALTER TABLE program_picks ADD COLUMN IF NOT EXISTS source TEXT",
                "ALTER TABLE program_picks ADD COLUMN IF NOT EXISTS family TEXT",
                "ALTER TABLE program_picks ADD COLUMN IF NOT EXISTS reason TEXT",
                "ALTER TABLE program_picks ALTER COLUMN market DROP NOT NULL",
                "ALTER TABLE program_picks ADD COLUMN IF NOT EXISTS lineup TEXT")
_COLS = ("match", "match_date", "league", "display", "market", "label", "family", "reason", "model_prob",
         "market_prob", "lean", "min_us", "best_odds", "playdoit_odds", "value", "lineup")

NO_EDGE = "Sin ventaja: el programa ve lo mismo que el mercado"
TOO_FAR = "El programa se separa demasiado del mercado: probablemente se equivoca"
MOVED = "El mercado se movió en contra después del análisis (alineaciones o noticias)"
CROSS = "Equipos de ligas distintas: falta ajustar por la fuerza de cada liga"
NO_PRICE = "Sin momio en rango para lo que el programa cree"
STALE = "El programa ya no lo evalúa: poca historia de estos equipos para opinar"
# 24-oct-26 (el dueño: "aunque tenga poca historia quiero ver el pick"): el
# partido se analiza igual y su pick se ve, nunca 🎯, con el detalle de
# model_vs_sharp.low_history
LOW_HISTORY = "Poca historia para apostar"
# 26-oct-26, el dueño: "ponemos los picks mucho antes de que la lista de
# alineaciones esté lista… no es correcto poner que un equipo ganará cuando no
# tiene a jugadores importantes o sale con equipo suplente". El closing guarda
# la alineación (~1 h antes) y DESPUÉS recalcula el pick: si al equipo que el
# pick da por ganador le faltan 4+ titulares habituales (el umbral de las
# alertas: contra el cierre su rival gana +2.6 pt con 4 fuera y +8.2 con 5+),
# ese pick no es 🎯 y el pick pasa a lo mejor que quede de lo que el programa cree.
WEAKENED = "Alineación: a {team} le faltan {n} titulares habituales ({names})"
LINEUP_OK = "confirmada"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def devig2(a, b) -> float | None:
    """Probabilidad sin margen del primer lado de un mercado de dos."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 1 or b <= 1:
        return None
    return (1 / a) / (1 / a + 1 / b)


def fair_1x2(u) -> list | None:
    """1X2 de Pinnacle sin margen con la última recarga de momios."""
    if u is None:
        return None
    odds = [_num(getattr(u, c, None)) for c in ("pin_home_odds", "pin_draw_odds", "pin_away_odds")]
    if not all(o and o > 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    return [x / sum(inv) for x in inv]


def playdoit_estimate(consensus, ratio=None):
    """Momio estimado de PlayDoit: el promedio de las casas por lo que PlayDoit
    paga frente al promedio, aprendido de las capturas (playdoit_prices);
    sin capturas suficientes, el promedio tal cual. Decimal o None."""
    c = _num(consensus)
    if not c or c <= 1:
        return None
    r = _num(ratio) or 1.0
    return max(1.01, c * r)


def cross_league(league) -> bool:
    return any(k in str(league or "").lower() for k in CROSS_LEAGUE)


def goals_matrix(lam_h, lam_a, model_1x2, p_over25=None):
    """Matriz de marcadores del programa: la Poisson de sus goles esperados,
    escalada para dar su mismo más de 2.5 y repartida en su mismo 1X2. Así
    las líneas 1.5 y 3.5 cuadran con lo que ya dijo. None sin datos."""
    from src.models.asian_handicap_model import consistent_score_matrix
    lh, la = _num(lam_h), _num(lam_a)
    if not lh or not la or None in model_1x2:
        return None
    po = _num(p_over25)
    k = 1.0
    if po is not None and 0.05 < po < 0.95:
        lo, hi = 0.3, 3.0
        for _ in range(40):
            k = (lo + hi) / 2
            m = consistent_score_matrix(lh * k, la * k, model_1x2, rho=RHO)
            if _over(m, 2.5) < po:
                lo = k
            else:
                hi = k
    return consistent_score_matrix(lh * k, la * k, model_1x2, rho=RHO)


def _over(m, line: float) -> float:
    i, j = np.indices(m.shape)
    return float(m[(i + j) > line].sum())


def _cand(market, label, family, p, mk, best, playdoit):
    return {"market": market, "label": label, "family": family, "model": p, "market_prob": mk,
            "best_odds": _num(best), "playdoit_odds": _num(playdoit)}


def candidates(model: list, lam: tuple, p_over25, p_btts, u, props: dict | None = None, ratio=None) -> list:
    """Cada apuesta con precio real del partido: probabilidad del programa,
    la del mercado sin margen y el momio estimado de PlayDoit."""
    g = (lambda col: getattr(u, col, None) if u is not None else None)
    out = []
    pin = fair_1x2(u)
    if pin and None not in model:
        for (_, _, _, best_col, cons_col, market, label), p, mk in zip(SIDES, model, pin):
            out.append(_cand(market, label, "Ganador", p, mk, g(best_col), playdoit_estimate(g(cons_col), ratio)))
    po = _num(p_over25)
    mk = devig2(g("consensus_over25_odds"), g("consensus_under25_odds")) or devig2(g("over25_odds"), g("under25_odds"))
    if po is not None and mk is not None:
        out.append(_cand("over25", "Más de 2.5 goles", "Goles", po, mk, g("over25_odds"),
                         playdoit_estimate(g("consensus_over25_odds"), ratio)))
        out.append(_cand("under25", "Menos de 2.5 goles", "Goles", 1 - po, 1 - mk, g("under25_odds"),
                         playdoit_estimate(g("consensus_under25_odds"), ratio)))
    line = _num(g("pin_total_line"))
    if line in (1.5, 3.5):
        mk = devig2(g("pin_total_over_odds"), g("pin_total_under_odds"))
        mat = goals_matrix(lam[0], lam[1], model, po)
        if mk is not None and mat is not None:
            pov = _over(mat, line)
            est = lambda o: _num(o) * TWO_WAY_AVG_RATIO if _num(o) else None
            out.append(_cand(f"over_{line}", f"Más de {line} goles", "Goles", pov, mk, None, est(g("pin_total_over_odds"))))
            out.append(_cand(f"under_{line}", f"Menos de {line} goles", "Goles", 1 - pov, 1 - mk, None,
                             est(g("pin_total_under_odds"))))
    pb = _num(p_btts)
    mk = devig2(g("btts_yes_odds"), g("btts_no_odds"))
    if pb is not None and mk is not None:
        out.append(_cand("btts", "Ambos anotan", "Ambos anotan", pb, mk, g("btts_yes_odds"),
                         _num(g("btts_yes_odds")) * TWO_WAY_AVG_RATIO))
        out.append(_cand("btts_no", "No anotan ambos", "Ambos anotan", 1 - pb, 1 - mk, g("btts_no_odds"),
                         _num(g("btts_no_odds")) * TWO_WAY_AVG_RATIO))
    for prop, fam, name in (("corners", "Córners", "córners"), ("cards", "Tarjetas", "tarjetas")):
        p_over = (props or {}).get(prop)              # fn(línea) → P(más de)
        line = _num(g(f"{prop}_line"))
        mk = devig2(g(f"{prop}_over_odds"), g(f"{prop}_under_odds"))
        if p_over is None or line is None or mk is None:
            continue
        pov = p_over(line)
        out.append(_cand(f"{prop}_over_{line}", f"Más de {line} {name}", fam, pov, mk, g(f"{prop}_over_odds"),
                         _num(g(f"{prop}_over_odds")) * TWO_WAY_AVG_RATIO))
        out.append(_cand(f"{prop}_under_{line}", f"Menos de {line} {name}", fam, 1 - pov, 1 - mk,
                         g(f"{prop}_under_odds"), _num(g(f"{prop}_under_odds")) * TWO_WAY_AVG_RATIO))
    return out


def _why(c: dict, then: dict | None, unadjusted_cross: bool) -> str | None:
    """Por qué el candidato no pasa la regla; None si la pasa."""
    if unadjusted_cross:
        return CROSS
    gap = c["model"] - c["market_prob"]
    if gap < MIN_GAP:
        return NO_EDGE
    if gap > MAX_GAP:
        return TOO_FAR
    before = (then or {}).get(c["market"])
    if before is not None and before - c["market_prob"] >= MOVED_AGAINST:
        return MOVED
    if c["playdoit_odds"] is None or not (MIN_ODDS <= c["playdoit_odds"] <= MAX_ODDS):
        return NO_PRICE
    return None


def choose(cands: list, then: dict | None = None, league=None, adjusted: bool = False,
           weak: dict | None = None) -> tuple:
    """(pick, motivo). then: {mercado: probabilidad del mercado cuando el
    programa analizó el partido}, para la regla del movimiento en contra.
    adjusted: el partido entre ligas se analizó con la fuerza de cada liga.
    weak: {mercado: motivo} de los ganadores con la alineación debilitada
    (lineup_check). De lo que pasa la regla, el de mayor diferencia (motivo
    None). Si nada la pasa, igual hay pick: de lo que el programa cree, donde
    más se separa del mercado, con el motivo por el que no pasa; el equipo
    debilitado solo si no queda otra cosa. Sin nada que el programa crea con
    precio: (None, NO_PRICE)."""
    weak = weak or {}
    fav = max((c["model"] for c in cands if c["family"] == "Ganador"), default=None)
    leaning = [c for c in cands if (c["model"] >= fav - 1e-9 if c["family"] == "Ganador" else c["model"] >= 0.5)]
    if not leaning:
        return None, NO_PRICE
    unadjusted = cross_league(league) and not adjusted
    why = [(c, weak.get(c["market"]) or _why(c, then, unadjusted)) for c in leaning]
    ok = [c for c, w in why if w is None]
    gap = (lambda c: c["model"] - c["market_prob"])
    if ok:
        return max(ok, key=gap), None
    rest = [cw for cw in why if cw[0]["market"] not in weak] or why
    return max(rest, key=lambda cw: gap(cw[0]))


def lineup_check(home: str, away: str, absent: dict | None) -> tuple:
    """(weak, nota) de un partido con la alineación publicada. absent:
    {"home": absences(), "away": absences()} (lineup_alerts.absences; un lado
    que falta = ese equipo no tiene alineación todavía). weak: {mercado:
    motivo} del ganador al que le faltan 4+ titulares habituales; nota: lo que
    se guarda en program_picks.lineup (None = el pick no vio la alineación)."""
    from src.models.lineup_alerts import TRIGGER_MISSING
    from dashboard.display import team_name
    if not absent or absent.get("home") is None or absent.get("away") is None:
        return {}, None
    weak, notes = {}, []
    for side, team, market in (("home", home, "home_win"), ("away", away, "away_win")):
        a = absent[side]
        if a.get("known") and len(a["missing"]) >= TRIGGER_MISSING:
            n = len(a["missing"])
            names = ", ".join(a["missing"][:6]) + ("…" if n > 6 else "")
            weak[market] = WEAKENED.format(team=team_name(team), n=n, names=names)
            notes.append(f"{team_name(team)} con {n} fuera")
    return weak, LINEUP_OK + (": " + "; ".join(notes) if notes else "")


def _row(r: dict, kick, display, pick_: dict | None, reason: str | None, source=None, lineup=None) -> dict:
    base = {"match": str(r["match"]).lower(), "match_date": pd.Timestamp(kick).to_pydatetime(),
            "league": r.get("league"), "display": display, "source": source, "lineup": lineup}
    if not pick_:
        return {**base, **{c: None for c in ("market", "label", "family", "model_prob", "market_prob", "lean",
                                             "min_us", "best_odds", "playdoit_odds", "value")}, "reason": reason}
    min_us = min_american(pick_["model"], pin_prob=pick_["market_prob"])
    est_us = to_american(pick_["playdoit_odds"]) if pick_["playdoit_odds"] else None
    return {**base, "market": pick_["market"], "label": pick_["label"], "family": pick_["family"], "reason": reason,
            "model_prob": round(pick_["model"], 3), "market_prob": round(pick_["market_prob"], 3), "lean": True,
            "min_us": min_us, "best_odds": pick_["best_odds"], "playdoit_odds": pick_["playdoit_odds"],
            "value": bool(min_us is not None and est_us is not None and est_us >= min_us)}


def match_key(home, away, normalize=None) -> str:
    if normalize:
        home, away = normalize(home), normalize(away)
    return f"{str(home).lower()} vs {str(away).lower()}"


def _market_then(r: dict, then_snap, ratio=None) -> dict:
    """{mercado: probabilidad del mercado} en la foto de cuando se analizó."""
    if then_snap is None:
        return {}
    model = [_num(r.get(c)) for _, c, *_ in SIDES]
    cs = candidates(model, (r.get("lam_home"), r.get("lam_away")), r.get("p_over25"), r.get("p_btts"),
                    SimpleNamespace(**then_snap) if isinstance(then_snap, dict) else then_snap, None, ratio)
    return {c["market"]: c["market_prob"] for c in cs}


def model_1x2(r: dict, bias: dict | None = None) -> list:
    """El 1X2 del programa de una fila de model_vs_sharp (que lo guarda CRUDO,
    para aprender el sesgo) con el sesgo de su región ya descontado, como lo
    usa el pipeline (side_bias.program_probs; 22-oct-26: el pick usaba el crudo)."""
    from src.models.side_bias import program_probs
    p = program_probs(r, bias)
    return [p["home_win"], p["draw"], p["away_win"]]


def decide(r: dict, u, then_snap=None, props=None, ratio=None, bias=None, weak=None) -> tuple:
    """(pick, motivo) para una fila de model_vs_sharp con los momios de ahora
    (u) y los de cuando se analizó (then_snap). bias: sesgo por lado aprendido.
    weak: los ganadores con la alineación debilitada (lineup_check)."""
    from src.models.side_bias import is_skipped
    if is_skipped(r):                  # la última corrida ya no lo evaluó (22-oct-26)
        return None, STALE
    cs = _candidates_of(r, u, props, ratio, bias)
    if cs is None:
        return None, NO_EDGE
    pick, why = choose(cs, _market_then(r, then_snap, ratio), r.get("league"), _num(r.get("league_factor")) is not None,
                       weak)
    low = r.get("low_history")
    if pick and low and not (isinstance(low, float) and pd.isna(low)):
        why = f"{LOW_HISTORY}: {low}"                     # se ve el pick, nunca 🎯
    return pick, why


def _candidates_of(r: dict, u, props=None, ratio=None, bias=None) -> list | None:
    """Los candidatos de un partido de model_vs_sharp con el modelo corregido
    (None sin 1X2 del programa). Los mismos para el pick y para la vista."""
    from src.models.side_bias import program_probs
    probs = program_probs(r, bias)                 # 1X2, goles y ambos anotan ya corregidos (23-oct-26)
    model = [probs["home_win"], probs["draw"], probs["away_win"]]
    if None in model:
        return None
    cs = candidates(model, (r.get("lam_home"), r.get("lam_away")), probs["over25"], probs["btts"], u, props, ratio)
    pin = [_num(r.get(c)) for _, _, c, *_ in SIDES]
    if not any(c["family"] == "Ganador" for c in cs) and None not in pin:
        # sin momios del 1X2: el Pinnacle de cuando se analizó, sin precio
        cs += [_cand(market, label, "Ganador", p, mk, None, None)
               for (_, _, _, _, _, market, label), p, mk in zip(SIDES, model, pin)]
    return cs


EST_LINES = (1.5, 3.5)


def _poisson_cdf(lam: float, k: int) -> float:
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return total


def _market_goals_lambda(p_over25: float) -> float | None:
    """Los goles totales (Poisson) que dan el más de 2.5 del mercado."""
    if not 0.02 < p_over25 < 0.98:
        return None
    lo, hi = 0.1, 9.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if 1 - _poisson_cdf(mid, 2) < p_over25:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def estimated_lines(r: dict, u, bias=None, have=()) -> list:
    """Más y menos de 1.5 y 3.5 sin momio real (26-oct-26, el dueño: "también
    se deben valorar el 2.5, ambos anotan o el menos 3.5"). El programa, con su
    matriz de marcadores (la misma de los picks); el mercado, ESTIMADO de su
    más/menos 2.5 sin margen (Poisson con el mismo más de 2.5). Solo para
    verlos: sin precio real no son candidatos al pick. have: los mercados que
    ya tienen momio real (la línea principal de Pinnacle)."""
    from src.models.side_bias import program_probs
    probs = program_probs(r, bias)
    model = [probs["home_win"], probs["draw"], probs["away_win"]]
    g = (lambda col: getattr(u, col, None) if u is not None else None)
    mk25 = devig2(g("consensus_over25_odds"), g("consensus_under25_odds")) or devig2(g("over25_odds"), g("under25_odds"))
    if None in model or probs["over25"] is None or mk25 is None:
        return []
    mat = goals_matrix(r.get("lam_home"), r.get("lam_away"), model, probs["over25"])
    lam_m = _market_goals_lambda(mk25)
    if mat is None or lam_m is None:
        return []
    out = []
    for line in EST_LINES:
        if f"over_{line}" in have:
            continue
        pov, mov = _over(mat, line), 1 - _poisson_cdf(lam_m, int(line))
        out += [{"label": f"Más de {line} goles", "family": "Goles", "market": f"over_{line}", "model": pov,
                 "market_prob": mov, "estimated": True},
                {"label": f"Menos de {line} goles", "family": "Goles", "market": f"under_{line}", "model": 1 - pov,
                 "market_prob": 1 - mov, "estimated": True}]
    return out


def market_view(r: dict, u, ratio=None, bias=None) -> list:
    """Lo que vio el programa en cada mercado de un partido (26-oct-26, el
    dueño: "pusiste que ganaba el local… yo hubiera propuesto ambos anotan"):
    programa, mercado sin margen y diferencia, de la más a favor a la más en
    contra, con el momio mínimo de PlayDoit donde el programa ve más que el
    mercado. El pick sale de los que el programa cree (choose); aquí se ven
    todos, también los que descartó, y las líneas 1.5 y 3.5 estimadas."""
    cs = _candidates_of(r, u, None, ratio, bias) or []
    cs = cs + estimated_lines(r, u, bias, {c["market"] for c in cs})
    out = []
    for c in cs:
        m, mk = float(c["model"]), float(c["market_prob"])
        out.append({"label": c["label"], "family": c["family"], "market": c["market"],
                    "model": round(m, 3), "market_prob": round(mk, 3), "gap": round(m - mk, 3),
                    "estimated": bool(c.get("estimated")),
                    "min_us": min_american(m, pin_prob=mk) if m > mk else None})
    return sorted(out, key=lambda c: -c["gap"])


def _snap_index(snaps: pd.DataFrame, normalize=None) -> dict:
    idx = {}
    for s in (snaps.to_dict("records") if not snaps.empty else []):
        h, _, a = str(s["match"]).partition(" vs ")
        idx.setdefault(match_key(h, a, normalize), []).append(s)
    return idx


def _snap_before(idx: dict, key: str, kick, before) -> dict | None:
    """La foto de momios más reciente del partido antes de `before`."""
    k, b = pd.Timestamp(kick), pd.Timestamp(before)
    c = [s for s in idx.get(key, []) if abs((pd.Timestamp(s["kickoff"]) - k).total_seconds()) <= 3 * 3600
         and pd.Timestamp(s["captured_at"]) < b and pd.Timestamp(s["captured_at"]) < pd.Timestamp(s["kickoff"])]
    return max(c, key=lambda s: pd.Timestamp(s["captured_at"])) if c else None


def build_picks(mvs: pd.DataFrame, upcoming: pd.DataFrame, snaps: pd.DataFrame | None = None, ratio=None,
                normalize=None, props_for=None, bias=None, lineups_for=None) -> list:
    """Función pura: el pick (o "sin pick" con su motivo) de cada partido de
    model_vs_sharp (la fila más reciente) con la última fila de
    upcoming_matches. snaps: odds_history, para los momios de cuando se
    analizó. props_for(home, away, league, kickoff) → {prop: fn(línea)}.
    bias: sesgo aprendido (side_bias: por lado, región y goles).
    lineups_for(home, away, kickoff) → (weak, nota) de lineup_check."""
    up = {match_key(r.home_team_norm, r.away_team_norm, normalize): r for r in upcoming.itertuples(index=False)}
    idx = _snap_index(snaps if snaps is not None else pd.DataFrame(), normalize)
    latest = {}
    order = mvs.sort_values("created_at") if "created_at" in mvs.columns else mvs
    for r in order.to_dict("records"):
        h, _, a = str(r["match"]).partition(" vs ")
        latest[(match_key(h, a, normalize), str(r["match_date"]))] = r
    rows = []
    for (key, _), r in latest.items():
        u = up.get(key)
        then = _snap_before(idx, key, r["match_date"], r["created_at"]) if r.get("created_at") is not None else None
        h, _, a = key.partition(" vs ")
        props = props_for(h, a, r.get("league"), r["match_date"]) if props_for else None
        weak, lineup = lineups_for(h, a, r["match_date"]) if lineups_for else ({}, None)
        pk, why = decide(r, u, then, props, ratio, bias, weak)
        rows.append(_row(r, r["match_date"], f"{u.home_team} vs {u.away_team}" if u is not None else None, pk, why,
                         lineup=lineup))
    return rows


_UPCOMING_SQL = """
    SELECT DISTINCT ON (home_team_norm, away_team_norm) *
    FROM upcoming_matches
    WHERE match_date BETWEEN NOW() AND NOW() + make_interval(hours => :h)
    ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
"""
_SNAPS_SQL = """
    SELECT match, kickoff, captured_at, home_odds, draw_odds, away_odds, over25_odds, under25_odds,
           btts_yes_odds, btts_no_odds, pin_home_odds, pin_draw_odds, pin_away_odds,
           consensus_home_odds, consensus_draw_odds, consensus_away_odds,
           consensus_over25_odds, consensus_under25_odds
    FROM odds_history
    WHERE kickoff BETWEEN CAST(:a AS timestamptz) AND CAST(:b AS timestamptz) AND captured_at < kickoff
"""


def _props_for(engine):
    """fn(home, away, league, kickoff) → {"corners"|"cards": fn(línea)} con el
    modelo calibrado de córners y tarjetas (props_fair), lo de antes del partido."""
    from src.models import props_fair as PF
    try:
        hist = PF.load_history(engine)
    except Exception as e:
        log.warning(f"⚠️  Córners y tarjetas sin historia: {type(e).__name__}")
        return None

    def fn(home, away, league, kickoff):
        mp = PF.match_props(hist, home, away, league, pd.Timestamp(kickoff).tz_localize(None)
                            if pd.Timestamp(kickoff).tzinfo else pd.Timestamp(kickoff))
        return {p: (lambda line, mu=v["mu"], p=p: PF.prob_over(mu, line, PF.PROPS[p]["dispersion"]))
                for p, v in mp.items()}
    return fn


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def _lineups_for(engine, normalize=None):
    """fn(home, away, kickoff) → (weak, nota) con las alineaciones publicadas
    de los próximos partidos (lineups sin terminar: el closing las guarda
    ~1 h antes, justo antes de este paso). None si no hay ninguna."""
    import json
    from src.models import lineup_alerts as LA
    try:
        lu = pd.read_sql(text("""
            SELECT team, opponent, is_home, kickoff, starters FROM lineups
            WHERE NOT completed AND kickoff BETWEEN NOW() - INTERVAL '3 hours' AND NOW() + INTERVAL '1 day'
        """), engine)
        if lu.empty:
            return None
        profiles = LA.load_profiles(engine, sorted(set(lu["team"])))
    except Exception as e:
        # sin alineaciones el pick sale igual, pero que se vea: el dueño cuenta con que el pick las vio
        log.error(f"❌ Picks sin alineaciones ({type(e).__name__}: {str(e)[:120]}): no se revisan las bajas")
        return None
    idx = {}
    for r in lu.itertuples(index=False):
        home, away = (r.team, r.opponent) if r.is_home else (r.opponent, r.team)
        xi = r.starters if isinstance(r.starters, list) else json.loads(r.starters or "[]")
        prof = profiles.get(r.team)
        ab = LA.absences(xi, prof) if prof else {"missing": [], "top_out": None, "known": False}
        idx.setdefault(match_key(home, away, normalize), []).append((_utc(r.kickoff), "home" if r.is_home else "away", ab))

    def fn(home, away, kickoff):
        k = _utc(kickoff)
        got = {side: ab for t, side, ab in idx.get(match_key(home, away, normalize), [])
               if abs((t - k).total_seconds()) <= 3 * 3600}
        return lineup_check(home, away, got)
    return fn


def load_bias(engine=None) -> dict | None:
    """El sesgo que descuenta el pipeline (model_state side_bias: por lado,
    región y goles)."""
    import json
    from src.models.side_bias import STATE_KEY
    if engine is None:
        from config.database import engine
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM model_state WHERE key = :k"), {"k": STATE_KEY}).first()
    except Exception as e:
        log.warning(f"⚠️  Picks sin sesgo por lado ({type(e).__name__}): se usa el 1X2 crudo")
        return None
    if not row or row[0] is None:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])


def _ratio(engine):
    from src.models.playdoit_prices import playdoit_ratio
    try:
        rows = pd.read_sql(text("""SELECT decimal, consensus_decimal, boosted FROM playdoit_prices
                                   WHERE captured_at >= NOW() - INTERVAL '60 days'"""), engine)
        return playdoit_ratio(rows.to_dict("records")) if not rows.empty else None
    except Exception:
        return None                       # sin capturas todavía: el promedio tal cual


def _upsert(conn, rows: list, where: str) -> None:
    cols = _COLS + ("source",)
    for i in range(0, len(rows), 200):
        part = rows[i:i + 200]
        values, params = [], {}
        for k, r in enumerate(part):
            values.append("(" + ", ".join(f":{c}{k}" for c in cols) + ")")
            params.update({f"{c}{k}": r[c] for c in cols})
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("match", "match_date"))
        conn.execute(text(f"""
            INSERT INTO program_picks ({", ".join(cols)}) VALUES {", ".join(values)}
            ON CONFLICT (match, match_date) DO UPDATE SET {sets}, picked_at = NOW(),
                result = NULL, profit = NULL, resolved_at = NULL
            WHERE {where}
        """), params)


def _ensure(conn) -> None:
    conn.execute(text(PICKS_DDL))
    for a in PICKS_ALTERS:
        conn.execute(text(a))


def snapshot_picks(hours: int = SNAPSHOT_HOURS, engine=None) -> int:
    """Guarda el pick (o "sin pick") de los partidos de las próximas `hours`
    horas con los momios de ahora. Un partido ya empezado no se toca."""
    if engine is None:
        from config.database import engine
    from src.utils.team_normalizer import normalize_team
    try:
        mvs = pd.read_sql(text("""
            SELECT * FROM model_vs_sharp
            WHERE match_date BETWEEN NOW() + INTERVAL '5 minutes' AND NOW() + make_interval(hours => :h)
        """), engine, params={"h": hours})
        if mvs.empty:
            return 0
        up = pd.read_sql(text(_UPCOMING_SQL), engine, params={"h": hours})
        now = pd.Timestamp.now(tz="UTC")
        snaps = pd.read_sql(text(_SNAPS_SQL), engine, params={"a": now.to_pydatetime(),
                                                               "b": (now + pd.Timedelta(hours=hours)).to_pydatetime()})
        rows = build_picks(mvs, up, snaps, _ratio(engine), normalize_team, _props_for(engine), load_bias(engine),
                           _lineups_for(engine, normalize_team))
        if not rows:
            return 0
        with engine.begin() as conn:
            _ensure(conn)
            _upsert(conn, rows, "program_picks.result IS NULL AND program_picks.match_date > NOW()")
    except Exception as e:
        log.error(f"❌ Picks del programa no guardados: {type(e).__name__}: {str(e)[:200]}")
        return 0
    n = sum(1 for r in rows if r["market"])
    print(f"🎯 Picks del programa: {n} de {len(rows)} partidos")
    return len(rows)


def resolve_picks(engine=None) -> dict:
    """Liquida los picks ya jugados con la misma función que las apuestas
    (stake 1, al momio estimado de PlayDoit). Sin datos a los
    SHADOW_STALE_DAYS días quedan 'stale'. "Sin pick" no se liquida."""
    if engine is None:
        from config.database import engine
    from src.models.save_bets import SHADOW_STALE_DAYS, _preload_matches, plan_shadow_resolutions
    with engine.begin() as conn:
        _ensure(conn)
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, playdoit_odds FROM program_picks
        WHERE result IS NULL AND market IS NOT NULL AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)
    if pending.empty:
        return {"pending": 0, "resolved": 0}
    no_price = set(pending.loc[pending["playdoit_odds"].isna(), "id"])
    # sin momio de PlayDoit el resultado cuenta igual; las unidades no
    plan = plan_shadow_resolutions(pending.assign(odds=pending["playdoit_odds"].fillna(2.0).astype(float)),
                                   _preload_matches(pending["match_date"]))
    for p in plan:
        if p["id"] in no_price:
            p["profit"] = None
    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE program_picks SET result = :result, profit = :profit, resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE program_picks SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL AND market IS NOT NULL AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount
    print(f"🎯 Picks del programa liquidados: {len(plan)}/{len(pending)}"
          + (f", {stale} sin datos → stale" if stale else ""))
    return {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}


# ─────────────────────────────────────────────────────────────────────────────
# Picks reconstruidos (22-oct-26)
# ─────────────────────────────────────────────────────────────────────────────
# El dueño: "por qué ninguno de los partidos anteriores tiene un pick". Se
# guardan desde el 21-oct. Con lo que había ANTES de cada partido (la
# predicción de model_vs_sharp y las fotos de odds_history) se aplica la
# misma regla, sin mirar el resultado. model_vs_sharp guarda goles y ambos
# anotan solo desde el 21-oct: antes, solo el 1X2. odds_history guarda el
# promedio de las casas desde el 12-oct: antes no hay momio de PlayDoit y,
# sin precio, no hay pick. Córners y tarjetas no tienen momio guardado.

def build_past_picks(mvs: pd.DataFrame, snaps: pd.DataFrame, normalize=None) -> list:
    """Función pura: el pick de cada partido ya jugado con la última foto
    ANTERIOR al inicio y la de cuando el programa lo analizó."""
    idx = _snap_index(snaps, normalize)
    rows = []
    for r in (mvs.to_dict("records") if not mvs.empty else []):
        kick = pd.Timestamp(r["match_date"])
        h, _, a = str(r["match"]).partition(" vs ")
        key = match_key(h, a, normalize)
        snap = _snap_before(idx, key, kick, kick)
        then = _snap_before(idx, key, kick, r["created_at"]) if r.get("created_at") is not None else None
        pk, why = decide(r, SimpleNamespace(**snap) if snap else None, then)
        rows.append(_row(r, kick, snap["match"] if snap else None, pk, why, source="reconstruido"))
    return rows


def backfill_picks(since: str = "2026-09-01", engine=None, overwrite: bool = False) -> int:
    """Rehace con la regla de hoy los picks de los partidos ya jugados y los
    liquida. Solo toca partidos ya jugados: los que no tienen pick o, con
    overwrite, todos (para pisar los de una regla vieja)."""
    if engine is None:
        from config.database import engine
    from src.utils.team_normalizer import normalize_team
    # sin los análisis que ya no valen (skipped_at): los que la última corrida
    # ya no evaluó y la fecha vieja de un reprogramado (src/models/rescheduled.py)
    mvs = pd.read_sql(text("""
        SELECT * FROM model_vs_sharp
        WHERE match_date >= CAST(:s AS date) AND match_date < NOW() - INTERVAL '3 hours'
          AND skipped_at IS NULL
    """), engine, params={"s": since})
    snaps = pd.read_sql(text(_SNAPS_SQL), engine, params={
        "a": pd.Timestamp(since, tz="UTC").to_pydatetime(), "b": pd.Timestamp.now(tz="UTC").to_pydatetime()})
    rows = build_past_picks(mvs, snaps, normalize_team)
    if not rows:
        return 0
    with engine.begin() as conn:
        _ensure(conn)
        _upsert(conn, rows, "program_picks.match_date < NOW() - INTERVAL '3 hours'"
                + ("" if overwrite else " AND program_picks.market IS NULL"))
    print(f"🎯 Picks reconstruidos: {sum(1 for r in rows if r['market'])} de {len(rows)} partidos desde {since}")
    resolve_picks(engine=engine)
    return len(rows)
