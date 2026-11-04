"""
dashboard/parlay.py
===================
Pestaña "Parlay" (v2.6.0, 3-nov-26). El dueño: "quiero agregar la función
parley, donde seleccione los partidos más seguros, de las mejores ligas, de
los que más se ganan y los agrupe en una pestaña".

Las piernas las guarda el pipeline (src/models/program_parlays.py: la apuesta
más segura de cada partido de las ligas donde los picks van ganando, las
MAX_LEGS más seguras de cada día). Aquí, funciones puras: el parlay de 2, 3 o
4 (las k piernas más seguras), su momio, la probabilidad de que salgan todas,
cómo va y el historial. Como en Anteriores, una pierna con el final de ESPN
sale ✅/❌ provisional y el parlay no cuenta en los totales hasta que la
liquidación lo confirma.

v2.7.0 (4-nov-26): un parlay por horario (program_parlays.slot), no por día,
para que vea las alineaciones de todos sus partidos. Cada horario dice si ya
las vio (ready) o cuáles faltan (waiting). Lo guardado antes de la columna
toma como horario el primer partido de su día.
"""

import math

import pandas as pd

from dashboard.display import league_name, team_name
from src.utils.min_odds import fmt_american, to_american

SIZES = (2, 3, 4)
WIN_LIKE = ("win", "half_win")
LOSS_LIKE = ("loss", "half_loss")
VOID = ("push", "stale")          # la pierna se anula y el parlay sigue con las demás


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def _us(dec) -> str | None:
    return fmt_american(to_american(dec)) if dec and dec > 1 else None


def _factor(result: str | None, odds: float) -> float | None:
    """Lo que multiplica cada pierna al parlay (None = todavía no se sabe)."""
    if result == "win":
        return odds
    if result == "half_win":
        return (1 + odds) / 2
    if result == "half_loss":
        return 0.5
    if result in VOID:
        return 1.0
    if result == "loss":
        return 0.0
    return None


def leg_view(r: dict, scores: list, now: pd.Timestamp, covered=None) -> dict:
    """Una pierna como la ve la pestaña. scores: los marcadores de su partido,
    el de ESPN (live_scores) y el final oficial (`matches`, board._final_of);
    sin liquidar, el primero que resuelve su mercado la da ✅/❌ provisional.
    covered: las ligas con alineación en ESPN (None = no se sabe)."""
    from src.features.live_scores import provisional_result
    from src.models.program_parlays import lineup_ready
    kick = _utc(r["match_date"])
    h, _, a = str(r["match"]).partition(" vs ")
    odds = float(r["playdoit_odds"])
    result, provisional, status = _str(r.get("result")), False, None
    for sc in ([] if result else [s for s in scores if s]):
        status = status or {"status": sc.get("status"), "clock": _str(sc.get("clock")) or ""}
        if sc.get("status") == "final":
            out, _ = provisional_result(r["market"], sc, odds)
            if out:                                    # sin el dato de su mercado sigue ⏳
                result, provisional = out, True
                break
    # el nombre del feed ("Örgryte IS") si se guardó; si no, el de la clave en minúsculas
    return {"rank": int(r["rank"]), "match": _str(r.get("display")) or f"{team_name(h)} vs {team_name(a)}",
            "key": str(r["match"]),
            "league": league_name(r.get("league")) if r.get("league") else "", "kickoff": kick.isoformat(),
            "started": bool(kick <= now), "label": r.get("label"), "family": r.get("family"),
            "model": _num(r.get("model_prob")), "market": _num(r.get("market_prob")),
            "safety": _num(r.get("safety")), "odds": odds, "odds_us": _us(odds),
            "lineup": _str(r.get("lineup")), "lineup_ok": lineup_ready(r.get("lineup"), r.get("league"), covered),
            "no_lineups": covered is not None and r.get("league") not in covered,
            "result": result or "pending", "provisional": provisional, "live": status}


def combine(legs: list, k: int) -> dict | None:
    """El parlay de las k piernas más seguras del horario (None con menos de 2).
    full: el horario tenía las k (si no, es de las que hay). status: win / loss /
    push (todas anuladas) / pending. settled: todas liquidadas, sin provisional
    (solo esos cuentan en el historial). profit: a 1u."""
    use = sorted(legs, key=lambda x: x["rank"])[:k]
    if len(use) < 2:
        return None
    dec = math.prod(x["odds"] for x in use)
    facs = [_factor(None if x["result"] == "pending" else x["result"], x["odds"]) for x in use]
    if any(f == 0.0 for f in facs):
        status, profit = "loss", -1.0
    elif None in facs:
        status, profit = "pending", None
    elif all(x["result"] in VOID for x in use):
        status, profit = "push", 0.0
    else:
        status, profit = "win", round(math.prod(facs) - 1, 4)
    p_model = math.prod(x["model"] for x in use) if all(x["model"] is not None for x in use) else None
    p_market = math.prod(x["market"] for x in use) if all(x["market"] is not None for x in use) else None
    return {"k": len(use), "full": len(use) == k, "ranks": [x["rank"] for x in use],
            "odds": round(dec, 4), "odds_us": _us(dec),
            "p_model": round(p_model, 3) if p_model is not None else None,
            "p_market": round(p_market, 3) if p_market is not None else None,
            "won_legs": sum(1 for x in use if x["result"] in WIN_LIKE),
            "status": status, "profit": profit,
            "settled": status != "pending" and not any(x["provisional"] for x in use),
            "first_kickoff": min(x["kickoff"] for x in use),
            "locked": any(x["started"] for x in use)}


def record(blocks: list, k: int) -> dict:
    """El historial del parlay de k: solo los horarios que tenían las k
    piernas y ya liquidados. Las piernas cuentan aparte (¿qué tan seguras
    fueron?)."""
    done, legs = [], []
    for d in blocks:
        p = d["parlays"].get(str(k))
        if p and p["full"] and p["settled"]:
            done.append(p)
            legs += [x for x in d["legs"] if x["rank"] in p["ranks"] and x["result"] in WIN_LIKE + LOSS_LIKE]
    won = sum(1 for p in done if p["status"] == "win")
    lost = sum(1 for p in done if p["status"] == "loss")
    units = round(sum(p["profit"] for p in done), 2)
    legs_won = sum(1 for x in legs if x["result"] in WIN_LIKE)
    return {"n": len(done), "won": won, "lost": lost, "hit": round(won / (won + lost), 3) if won + lost else None,
            "units": units, "roi": round(units / len(done), 3) if done else None,
            "legs": len(legs), "legs_won": legs_won,
            "legs_hit": round(legs_won / len(legs), 3) if legs else None}


def _live_index(live: list, normalize=None) -> dict:
    canon = (lambda m: m) if normalize is None else (lambda m: " vs ".join(normalize(x) for x in m.split(" vs ", 1)))
    idx = {}
    for r in live:
        idx.setdefault(canon(f"{r['home']} vs {r['away']}").lower(), []).append(r)
    return idx


def _live_for(idx: dict, key: str, kick: pd.Timestamp) -> dict | None:
    for r in idx.get(key, []):
        if abs((_utc(r["kickoff"]) - kick).total_seconds()) <= 3 * 3600 and _num(r.get("home_goals")) is not None:
            return r
    return None


def _official_for(official: list, key: str, kick: pd.Timestamp) -> dict | None:
    """El final de `matches` del partido (±1 día) como lo guarda live_scores."""
    from dashboard.board import _final_of
    for m in official:
        if f"{m['h']} vs {m['a']}" == key and abs((pd.Timestamp(m["date"]) - kick.tz_localize(None).normalize()).days) <= 1:
            return {**_final_of(m), "status": "final"}
    return None


def _slot_of(r: dict, first: dict) -> pd.Timestamp:
    """El horario de la pierna; lo guardado antes de v2.7.0, el primer partido de su día."""
    s = r.get("slot")
    if s is not None and not pd.isna(s):
        return _utc(s)
    return first[pd.Timestamp(r["day"]).strftime("%Y-%m-%d")]


def parlay_board(legs: pd.DataFrame, live: pd.DataFrame | None, leagues: list, now=None, normalize=None,
                 official: pd.DataFrame | None = None, covered=None) -> dict:
    """La pestaña: cada horario con sus piernas y su parlay de 2, 3 y 4, del
    más reciente; si ya vio las alineaciones de todos (ready) y cuáles faltan
    (waiting); el historial de cada tamaño y las ligas que entran hoy.
    official: filas de `matches` (h, a, date, goles, córners, tarjetas) para
    lo que ESPN no cubre. covered: las ligas con alineación en ESPN."""
    from src.models.program_parlays import LOCK_MINUTES
    now = _utc(now if now is not None else pd.Timestamp.now(tz="UTC"))
    idx = _live_index(live.to_dict("records") if live is not None and not live.empty else [], normalize)
    off = official.to_dict("records") if official is not None and not official.empty else []
    rows = legs.to_dict("records") if legs is not None and not legs.empty else []
    first = {}
    for r in rows:
        d = pd.Timestamp(r["day"]).strftime("%Y-%m-%d")
        first[d] = min(first.get(d, _utc(r["match_date"])), _utc(r["match_date"]))
    blocks = {}
    for r in rows:
        key, kick = str(r["match"]).lower(), _utc(r["match_date"])
        scores = [] if _str(r.get("result")) else [_live_for(idx, key, kick), _official_for(off, key, kick)]
        slot = _slot_of(r, first)
        b = blocks.setdefault(slot, {"day": pd.Timestamp(r["day"]).strftime("%Y-%m-%d"), "legs": []})
        b["legs"].append(leg_view(r, scores, now, covered))
    out = []
    for slot in sorted(blocks, reverse=True):
        ls = sorted(blocks[slot]["legs"], key=lambda x: x["rank"])
        out.append({"day": blocks[slot]["day"], "slot": slot.isoformat(),
                    "lock": (slot - pd.Timedelta(minutes=LOCK_MINUTES)).isoformat(),
                    "ready": all(x["lineup_ok"] for x in ls),
                    "waiting": [x["match"] for x in ls if not x["lineup_ok"]],
                    "legs": ls, "parlays": {str(k): combine(ls, k) for k in SIZES}})
    return {"blocks": out, "record": {str(k): record(out, k) for k in SIZES},
            "leagues": [{**x, "name": league_name(x["league"])} for x in leagues]}
