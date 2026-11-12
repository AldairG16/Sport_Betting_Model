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

Desde el 11-nov-26 mide además dos señales que salieron de repetir el
historial (137 mil partidos, tres épocas) y que todavía NO deciden nada:

3. El precio: el favorito de 65 a 70 cuyo momio estimado de PlayDoit no llega
   al mínimo (probabilidad + FAV_EDGE) perdió 6.8 por cada 100 apostados en
   las tres épocas; el que sí llega quedó en +0.5.
4. La "suerte de gol": goles de más contra lo que justifican los tiros a
   puerta en los últimos 10 partidos de liga, favorito menos rival. El
   favorito de 70+ que venía sin suerte ganó 4.8 más de cada 100 de lo que
   decía Pinnacle; el que venía con suerte, 2.6 (prometedor, no probado).

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
PRICE_HISTORY = {"pays": 0.5, "short": -6.8}    # 65-70: lo que deja por cada 100 apostados (m5, 11-nov-26)
LUCK_K = 0.31                               # goles por tiro a puerta (media de historical_odds)
LUCK_GAMES, LUCK_MIN = 10, 8                # últimos partidos de liga; con menos de 8 no hay dato
LUCK_DAYS = 200                             # cuánto hacia atrás se leen los partidos para esos 10
LUCK_CUT = 0.33                             # favorito menos rival, en goles por partido: más = "con suerte"
LUCK_HISTORY = {"low": 4.8, "high": 2.6}    # 70+: puntos de más sin suerte y con suerte (m5, 11-nov-26)
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


def _money(rows: list) -> dict:
    """Lo que dejaron al momio estimado de PlayDoit: por cada 100 apostados y
    en pesos apostando 50 a cada uno."""
    n = len(rows)
    if not n:
        return {"n": 0}
    net = sum((r["odds"] if r["won"] else 0.0) - 1.0 for r in rows)
    return {"n": n, "won": sum(1 for r in rows if r["won"]), "per_100": round(100 * net / n, 1),
            "profit_50": round(50 * net)}


def luck_index(history: list, normalize=None) -> dict:
    """{(liga, equipo): [(fecha, goles de más contra sus tiros a puerta)]} en
    orden de fecha. history: filas de matches con league, date, home, away,
    goles y tiros a puerta. Lo de un partido es (diferencia de goles) −
    LUCK_K × (diferencia de tiros a puerta), visto desde cada equipo."""
    norm = (lambda t: normalize(str(t)).lower()) if normalize else (lambda t: str(t).lower())
    idx: dict = {}
    for r in history:
        try:
            over = (float(r["home_goals"]) - float(r["away_goals"])
                    - LUCK_K * (float(r["home_shots_target"]) - float(r["away_shots_target"])))
            day = pd.Timestamp(r["date"]).tz_localize(None).normalize()
        except (TypeError, ValueError):
            continue
        if over != over:                               # NaN: sin goles o sin tiros
            continue
        idx.setdefault((r["league"], norm(r["home"])), []).append((day, over))
        idx.setdefault((r["league"], norm(r["away"])), []).append((day, -over))
    for games in idx.values():
        games.sort(key=lambda g: g[0])
    return idx


def luck_of(idx: dict, league, team, before, normalize=None) -> float | None:
    """La suerte de gol de un equipo: la media de sus últimos LUCK_GAMES
    partidos de liga ANTERIORES al día `before`. None con menos de LUCK_MIN."""
    key = (league, normalize(str(team)).lower() if normalize else str(team).lower())
    day = pd.Timestamp(before)
    day = (day.tz_convert("UTC").tz_localize(None) if day.tzinfo else day).normalize()
    last = [over for d, over in idx.get(key, []) if d < day][-LUCK_GAMES:]
    return sum(last) / len(last) if len(last) >= LUCK_MIN else None


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
    priced = [r for r in pa if r["p"] < 0.70 and r.get("pays") is not None and r.get("odds")]
    lucky = [r for r in pa if r["p"] >= 0.70 and r.get("luck") is not None]
    return {"window_days": WINDOW_DAYS, "strong": STRONG, "verdict": _verdict(scope), "scope": scope,
            "bands": {name: _stats([r for r in pa if lo <= r["p"] < hi]) for name, lo, hi in BANDS},
            "other_leagues": _stats(rest),
            "price": {"pays": _money([r for r in priced if r["pays"]]),
                      "short": _money([r for r in priced if not r["pays"]])},
            "luck": {"cut": LUCK_CUT, "low": _stats([r for r in lucky if r["luck"] <= LUCK_CUT]),
                     "high": _stats([r for r in lucky if r["luck"] > LUCK_CUT]),
                     # el visitante aparte (12-nov-26): en el historial es donde más se nota; aquí se confirma o no
                     "away_low": _stats([r for r in lucky if r.get("away") and r["luck"] <= LUCK_CUT]),
                     "away_high": _stats([r for r in lucky if r.get("away") and r["luck"] > LUCK_CUT])},
            "early_payout": {"n": len(known), "rescued": rescued,
                             "per_100": round(100 * rescued / len(known), 1) if known else None},
            "policy": {"min_n": MIN_N, "z": Z, "history": HISTORY, "pa_history": PA_HISTORY,
                       "price_history": PRICE_HISTORY, "luck_history": LUCK_HISTORY},
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


def _pesos(v) -> str:
    return f"{'+' if v >= 0 else '−'}{abs(int(v))} pesos"


def _extra_lines(state: dict) -> list:
    """Las dos señales en observación (precio y suerte de gol), en palabras del dueño."""
    lines = []
    price = state.get("price") or {}
    pays, short = price.get("pays") or {}, price.get("short") or {}
    if pays.get("n") or short.get("n"):
        parts = []
        if pays.get("n"):
            parts.append(f"cuando el momio estimado de PlayDoit llegaba al mínimo, {pays['n']} partidos: "
                         f"{_pesos(pays['profit_50'])} apostando 50 a cada uno")
        if short.get("n"):
            parts.append(f"cuando no llegaba, {short['n']} partidos: {_pesos(short['profit_50'])}")
        lines.append("• En observación, el precio (favoritos de 65 a 70): " + "; ".join(parts)
                     + f" (en el historial, los que no llegan pierden {abs(PRICE_HISTORY['short']):g} por cada 100 "
                       f"apostados)")
    luck = state.get("luck") or {}
    low, high = luck.get("low") or {}, luck.get("high") or {}
    if low.get("n") or high.get("n"):
        def said(s):
            return (f"ganaron {round(100 * s['won'] / s['n'])} de cada 100 y Pinnacle decía "
                    f"{round(100 * s['expected'] / s['n'])}, en {s['n']} partidos")
        parts = []
        if low.get("n"):
            parts.append("los que venían sin suerte " + said(low))
        if high.get("n"):
            parts.append("los que venían con suerte " + said(high))
        lines.append("• En observación, la suerte de gol (favoritos de 70 o más; goles de más contra sus tiros a "
                     "puerta): " + "; ".join(parts)
                     + f" (en el historial ganan {LUCK_HISTORY['low']:g} y {LUCK_HISTORY['high']:g} más de cada 100)")
    return lines


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
    lines += _extra_lines(state)
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


PICKS_SQL = """
    SELECT match, match_date, market, market_prob, playdoit_odds
    FROM program_picks
    WHERE match_date >= NOW() - make_interval(days => :d) AND market IN ('home_win', 'away_win')
      AND playdoit_odds IS NOT NULL AND market_prob IS NOT NULL
"""


def _price_of(picks: dict, match, kickoff, market, edge: float) -> tuple:
    """(momio estimado de PlayDoit del favorito, ¿llegaba al mínimo?) según el
    pick guardado de ese partido; (None, None) si el pick fue a otro lado."""
    k = pd.Timestamp(kickoff)
    k = k.tz_convert("UTC") if k.tzinfo else k.tz_localize("UTC")
    for t, mkt, prob, odds in picks.get(str(match).lower(), []):
        t = t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")
        if mkt == market and abs((t - k).total_seconds()) <= 36 * 3600:
            return float(odds), bool(float(odds) * (float(prob) + edge) >= 1)
    return None, None


def _read_picks(engine) -> dict:
    picks = pd.read_sql(text(PICKS_SQL), engine, params={"d": WINDOW_DAYS + 2})
    out: dict = {}
    for r in picks.to_dict("records"):
        out.setdefault(str(r["match"]).lower(), []).append(
            (pd.Timestamp(r["match_date"]), r["market"], r["market_prob"], r["playdoit_odds"]))
    return out


def _read_history(engine, since, leagues) -> list:
    """Los partidos de liga con goles y tiros a puerta desde `since` (para la suerte de gol)."""
    leagues = sorted({str(x) for x in leagues if x})
    if not leagues:
        return []
    marks = ", ".join(f":l{i}" for i in range(len(leagues)))
    hist = pd.read_sql(text(f"""
        SELECT league, date, LOWER(home_team) AS home, LOWER(away_team) AS away, home_goals, away_goals,
               home_shots_target, away_shots_target
        FROM matches
        WHERE date >= :s AND league IN ({marks}) AND home_goals IS NOT NULL AND away_goals IS NOT NULL
          AND home_shots_target IS NOT NULL AND away_shots_target IS NOT NULL
          AND home_shots_target + away_shots_target > 0"""), engine,
        params={"s": pd.Timestamp(since).strftime("%Y-%m-%d"), **{f"l{i}": x for i, x in enumerate(leagues)}})
    return hist.to_dict("records")


def read_rows(engine=None) -> list:
    """Los muy favoritos ya jugados con su resultado, su mayor ventaja, si su
    momio estimado llegaba al mínimo y su suerte de gol (favorito menos rival)."""
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
    first = pd.to_datetime(favs["match_date"], utc=True).min()
    leads = EP.load_leads(engine, first - pd.Timedelta(days=1), normalize_team)
    from src.models.program_picks import FAV_EDGE
    picks = _read_picks(engine)
    luck = luck_index(_read_history(engine, first - pd.Timedelta(days=LUCK_DAYS),
                                    [x for x in favs["league"].unique() if x in EP.PA_LEAGUES]), normalize_team)
    rows = []
    for r in favs.to_dict("records"):
        if plan.get(r["id"]) not in ("win", "loss"):
            continue
        home, _, away = str(r["match"]).partition(" vs ")
        fav, rival = (home, away) if r["market"] == "home_win" else (away, home)
        mine = luck_of(luck, r["league"], fav, r["match_date"], normalize_team)
        theirs = luck_of(luck, r["league"], rival, r["match_date"], normalize_team)
        odds, pays = _price_of(picks, r["match"], r["match_date"], r["market"], FAV_EDGE)
        rows.append({"p": float(r["p"]), "won": plan[r["id"]] == "win", "league": r["league"],
                     "lead": EP.lead_of(leads, r["match"], r["match_date"], r["market"], normalize_team),
                     "odds": odds, "pays": pays, "away": r["market"] == "away_win",
                     "luck": round(mine - theirs, 3) if mine is not None and theirs is not None else None})
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
