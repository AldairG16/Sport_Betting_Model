"""
src/models/favourite_watch.py
=============================
Los supuestos del 🎯, vigilados cada semana (11-nov-26).

El 🎯 (v2.9.0) no descansa en los pocos 🎯 ya cobrados sino en dos cosas
medidas en el historial grande (historical_odds, ligas con pago anticipado,
2012-26, 21 mil favoritos):

1. El muy favorito gana más de lo que dice Pinnacle sin margen: +3.6 puntos
   el de 70 de cada 100 o más (deja +1.3% al precio promedio de las casas) y
   +1.3 el de 65 a 70 (deja −1.6%: con el pago anticipado queda en tablas).
2. El pago anticipado de PlayDoit (2 goles arriba = ganada) agrega ~1.3 de
   cada 100.

El 11-nov había 17 🎯 de esta regla ya cobrados: por ganados y perdidos haría
falta años para saber si algo dejó de cumplirse. Aquí se mide con TODOS los
muy favoritos que el programa analizó (model_vs_sharp, con pick o sin él), que
son muchos más, y se guarda en model_state. Telegram solo cuando el veredicto
cambia; el chequeo diario avisa si esto deja de correr (output_checks 5,
WEEKLY_KEYS).

No cambia ningún pick: mide y avisa.
"""

import math

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

STATE_KEY = "favourite_watch"
WINDOW_DAYS = 120
STRONG = 0.65                               # el corte del 🎯 (program_picks.STRONG_FAV)
BANDS = (("65-70", 0.65, 0.70), ("70+", 0.70, 1.01))
HISTORY = {"65-70": 1.3, "70+": 3.6}        # puntos de más en el historial (r2_fav60, 11-nov-26)
PA_HISTORY = 1.3                            # de cada 100 apuestas al favorito, las que rescata el pago anticipado
MIN_N = 100
Z = 1.96
VERDICTS = {"en contra": "ganan MENOS de lo que dice Pinnacle: la base del 🎯 dejó de cumplirse",
            "a favor": "ganan más de lo que dice Pinnacle, como en el historial",
            "sin evidencia": "van como dice Pinnacle; todavía no se distingue del azar",
            "insuficiente": "todavía son pocos para opinar"}

ROWS_SQL = """
    SELECT DISTINCT ON (match, match_date) match, match_date, league, pin_home, pin_away
    FROM model_vs_sharp
    WHERE skipped_at IS NULL AND match_date < NOW() - INTERVAL '30 hours'
      AND match_date >= NOW() - make_interval(days => :d) AND GREATEST(pin_home, pin_away) >= :p
    ORDER BY match, match_date, created_at DESC
"""


def _stats(rows: list) -> dict:
    """n, ganados, lo que esperaba Pinnacle, los puntos de más y su z."""
    n = len(rows)
    if not n:
        return {"n": 0}
    won, exp = sum(1 for r in rows if r["won"]), sum(r["p"] for r in rows)
    sd = math.sqrt(sum(r["p"] * (1 - r["p"]) for r in rows))
    return {"n": n, "won": won, "expected": round(exp, 1), "extra_pts": round(100 * (won - exp) / n, 1),
            "z": round((won - exp) / sd, 2) if sd > 0 else 0.0}


def _verdict(s: dict) -> str:
    if s.get("n", 0) < MIN_N:
        return "insuficiente"
    return "en contra" if s["z"] <= -Z else "a favor" if s["z"] >= Z else "sin evidencia"


def evaluate(rows: list, pa_leagues=None, now=None) -> dict:
    """Función pura. rows: [{"p": probabilidad de Pinnacle sin margen del
    favorito, "won": ganó, "league", "lead": su mayor ventaja en el partido o
    None}]. El veredicto es el de los favoritos de las ligas con pago
    anticipado (donde vive el 🎯); los de las demás ligas van aparte."""
    if pa_leagues is None:
        from src.models.early_payout import PA_LEAGUES as pa_leagues
    pa = [r for r in rows if r.get("league") in set(pa_leagues)]
    rest = [r for r in rows if r.get("league") not in set(pa_leagues)]
    scope = _stats(pa)
    known = [r for r in pa if r.get("lead") is not None]
    rescued = sum(1 for r in known if not r["won"] and r["lead"] >= 2)
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    return {"window_days": WINDOW_DAYS, "strong": STRONG, "verdict": _verdict(scope), "scope": scope,
            "bands": {name: _stats([r for r in pa if lo <= r["p"] < hi]) for name, lo, hi in BANDS},
            "other_leagues": _stats(rest),
            "early_payout": {"n": len(known), "rescued": rescued,
                             "per_100": round(100 * rescued / len(known), 1) if known else None},
            "policy": {"min_n": MIN_N, "z": Z, "history": HISTORY, "pa_history": PA_HISTORY},
            "updated_at": now.isoformat()}


def changed(previous: dict | None, state: dict) -> bool:
    """El veredicto cambió (la primera vez no cuenta salvo que ya salga en contra)."""
    before = (previous or {}).get("verdict")
    if before is None:
        return state.get("verdict") == "en contra"
    return before != state.get("verdict")


def _line(name: str, s: dict, hist: float | None) -> str:
    if not s.get("n"):
        return f"• {name}: todavía ninguno"
    per, said = round(100 * s["won"] / s["n"]), round(100 * s["expected"] / s["n"])
    tail = f" (en el historial ganan {hist:g} más de cada 100)" if hist is not None else ""
    return f"• {name}: ganaron {per} de cada 100 y Pinnacle decía {said}, en {s['n']} partidos{tail}"


def format_report(state: dict, previous: dict | None = None, html: bool = False) -> str:
    """El texto para el dueño (Telegram y log): palabras, no estadística."""
    b = (lambda t: f"<b>{t}</b>") if html else (lambda t: t)
    lines = [b("🔎 Los supuestos del 🎯"),
             f"Todos los muy favoritos que el programa analizó en los últimos {state['window_days']} días, en las "
             f"ligas con pago anticipado (con 🎯 o sin él):",
             _line("Favoritos de 70 de cada 100 o más", state["bands"]["70+"], HISTORY["70+"]),
             _line("Favoritos de 65 a 70", state["bands"]["65-70"], HISTORY["65-70"])]
    ep = state["early_payout"]
    if ep["n"]:
        lines.append(f"• Pago anticipado: en {ep['n']} partidos con la ventaja guardada, {ep['rescued']} veces el "
                     f"favorito iba 2 goles arriba y no ganó (ahí PlayDoit paga igual): {ep['per_100']:g} de cada "
                     f"100; en el historial, {PA_HISTORY:g}")
    said = VERDICTS[state["verdict"]]
    scope = state["scope"]
    if state["verdict"] == "sin evidencia":       # la dirección sí se dice; que todavía puede ser suerte, también
        extra = scope["extra_pts"]
        said = (f"ganan {abs(extra):g} {'más' if extra >= 0 else 'menos'} de cada 100 de lo que dice Pinnacle, pero "
                f"con {scope['n']} partidos todavía puede ser suerte")
    lines.append(f"{b('Veredicto')}: los muy favoritos {said}.")
    before = (previous or {}).get("verdict")
    if before and before != state["verdict"]:
        lines.append(f"Antes: {VERDICTS[before]}.")
    if state["verdict"] == "en contra":
        lines.append("El 🎯 sigue saliendo igual; conviene revisar la regla antes de seguir apostándole.")
    return "\n".join(lines)


def read_rows(engine=None) -> list:
    """Los muy favoritos ya jugados con su resultado y su mayor ventaja."""
    if engine is None:
        from config.database import engine
    from src.models import early_payout as EP
    from src.models.save_bets import _preload_matches, plan_shadow_resolutions
    from src.utils.team_normalizer import normalize_team
    favs = pd.read_sql(text(ROWS_SQL), engine, params={"d": WINDOW_DAYS, "p": STRONG})
    if favs.empty:
        return []
    home = favs["pin_home"].astype(float) >= favs["pin_away"].astype(float)
    favs = favs.assign(id=range(len(favs)), market=home.map({True: "home_win", False: "away_win"}), odds=2.0,
                       p=favs[["pin_home", "pin_away"]].astype(float).max(axis=1))
    plan = {p["id"]: p["result"] for p in plan_shadow_resolutions(favs, _preload_matches(favs["match_date"]))}
    leads = EP.load_leads(engine, pd.to_datetime(favs["match_date"], utc=True).min() - pd.Timedelta(days=1),
                          normalize_team)
    rows = []
    for r in favs.to_dict("records"):
        if plan.get(r["id"]) not in ("win", "loss"):
            continue
        rows.append({"p": float(r["p"]), "won": plan[r["id"]] == "win", "league": r["league"],
                     "lead": EP.lead_of(leads, r["match"], r["match_date"], r["market"], normalize_team)})
    return rows


def load_favourite_watch() -> dict:
    from src.utils.model_state import load_state
    return load_state(STATE_KEY) or {}


def run_favourite_watch(verbose: bool = True, engine=None) -> dict:
    from src.utils.model_state import save_state
    state = evaluate(read_rows(engine))
    save_state(STATE_KEY, state)
    if verbose:
        print(format_report(state))
    return state
