"""
dashboard/parlay.py
===================
Pestaña "Parlay" (v2.6.0, 3-nov-26). El dueño: "quiero agregar la función
parley, donde seleccione los partidos más seguros, de las mejores ligas, de
los que más se ganan y los agrupe en una pestaña".

Las piernas las guarda el pipeline (src/models/program_parlays.py). Aquí,
funciones puras: cada parlay, su momio con el bono de PlayDoit, la
probabilidad de que salgan todas, cómo va y el historial. Como en
Anteriores, una pierna con el final de ESPN sale ✅/❌ provisional y el
parlay no cuenta en los totales hasta que la liquidación lo confirma.

v2.9.0 (6-nov-26), el parlay con bono: favoritos de 65%+ con pago
anticipado; el del día (4 a 6) y el de un horario (3 o más que empiezan
juntos, con alineación). El bono de PlayDoit se paga sobre la ganancia
(program_parlays.payout) y una pierna que fue 2 goles arriba se cobra por el
pago anticipado (⚡). Solo se muestran los de la regla nueva (rule 'bono');
los de antes siguen en la tabla.
"""

import math

import pandas as pd

from dashboard.display import league_name, team_name
from src.utils.min_odds import fmt_american, to_american

WIN_LIKE = ("win", "half_win")
LOSS_LIKE = ("loss", "half_loss")
VOID = ("push", "stale")          # la pierna se anula y el parlay sigue con las demás
KINDS = ("dia", "horario")


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
    el de ESPN (live_scores, con la ventaja más grande de cada equipo) y el
    final oficial (`matches`, board._final_of); sin liquidar, el primero que
    resuelve su mercado la da ✅/❌ provisional, con el pago anticipado si fue
    2 goles arriba. covered: las ligas con alineación en ESPN."""
    from src.features.live_scores import provisional_result
    from src.models.early_payout import applies, paid_early
    from src.models.program_parlays import lineup_ready
    kick = _utc(r["match_date"])
    h, _, a = str(r["match"]).partition(" vs ")
    odds = float(r["playdoit_odds"])
    result, provisional, status = _str(r.get("result")), False, None
    pa_paid = bool(r.get("pa_paid")) if r.get("pa_paid") is not None and not pd.isna(r.get("pa_paid")) else False
    for sc in ([] if result else [s for s in scores if s]):
        status = status or {"status": sc.get("status"), "clock": _str(sc.get("clock")) or ""}
        if sc.get("status") == "final":
            out, _ = provisional_result(r["market"], sc, odds)
            if out:                                    # sin el dato de su mercado sigue ⏳
                lead = _num(sc.get("home_max_lead" if r["market"] == "home_win" else "away_max_lead"))
                if applies(r.get("league"), r["market"]) and paid_early(out, lead):
                    out, pa_paid = "win", True
                result, provisional = out, True
                break
    pa = r.get("pa")
    return {"rank": int(r["rank"]), "match": _str(r.get("display")) or f"{team_name(h)} vs {team_name(a)}",
            "key": str(r["match"]),
            "league": league_name(r.get("league")) if r.get("league") else "", "kickoff": kick.isoformat(),
            "started": bool(kick <= now), "label": r.get("label"), "family": r.get("family"),
            "model": _num(r.get("model_prob")), "market": _num(r.get("market_prob")),
            "odds": odds, "odds_us": _us(odds),
            "pa": bool(pa) if pa is not None and not pd.isna(pa) else False, "pa_paid": pa_paid,
            "lineup": _str(r.get("lineup")), "lineup_ok": lineup_ready(r.get("lineup"), r.get("league"), covered),
            "no_lineups": covered is not None and r.get("league") not in covered,
            "result": result or "pending", "provisional": provisional, "live": status}


def combine(legs: list) -> dict | None:
    """El parlay con todas sus piernas: momio, bono de PlayDoit sobre la
    ganancia (por las piernas que no se anularon), probabilidad de que salgan
    todas (Pinnacle sin margen) y cómo va. settled: todas liquidadas, sin
    provisional (solo esos cuentan en el historial). profit: a 1u, con bono."""
    from src.models.program_parlays import bonus_for, payout
    if not legs:
        return None
    pay = payout([x["odds"] for x in legs])
    facs = [_factor(None if x["result"] == "pending" else x["result"], x["odds"]) for x in legs]
    valid = sum(1 for x in legs if x["result"] not in VOID)
    if any(f == 0.0 for f in facs):
        status, profit = "loss", -1.0
    elif None in facs:
        status, profit = "pending", None
    elif valid == 0:
        status, profit = "push", 0.0
    else:
        gain = math.prod(facs) - 1
        status, profit = "win", round(gain * (1 + bonus_for(valid)) if gain > 0 else gain, 4)
    p_market = math.prod(x["market"] for x in legs) if all(x["market"] is not None for x in legs) else None
    return {"k": len(legs), "odds": round(pay["odds"], 4), "odds_us": _us(pay["odds"]),
            "bonus": pay["bonus"], "odds_bonus": round(pay["with_bonus"], 4), "odds_bonus_us": _us(pay["with_bonus"]),
            "p_market": round(p_market, 3) if p_market is not None else None,
            "won_legs": sum(1 for x in legs if x["result"] in WIN_LIKE),
            "pa_legs": sum(1 for x in legs if x["pa_paid"]),
            "status": status, "profit": profit,
            "settled": status != "pending" and not any(x["provisional"] for x in legs),
            "first_kickoff": min(x["kickoff"] for x in legs),
            "locked": any(x["started"] for x in legs)}


def record(parlays: list, kind: str | None = None) -> dict:
    """El historial de los parlays ya liquidados (de un tipo o de todos). Las
    piernas cuentan aparte (¿qué tan seguras fueron?) y las cobradas por el
    pago anticipado."""
    done = [d for d in parlays if d["parlay"]["settled"] and (kind is None or d["kind"] == kind)]
    legs = [x for d in done for x in d["legs"] if x["result"] in WIN_LIKE + LOSS_LIKE]
    won = sum(1 for d in done if d["parlay"]["status"] == "win")
    lost = sum(1 for d in done if d["parlay"]["status"] == "loss")
    units = round(sum(d["parlay"]["profit"] for d in done), 2)
    legs_won = sum(1 for x in legs if x["result"] in WIN_LIKE)
    return {"n": len(done), "won": won, "lost": lost, "hit": round(won / (won + lost), 3) if won + lost else None,
            "units": units, "roi": round(units / len(done), 3) if done else None,
            "legs": len(legs), "legs_won": legs_won, "pa": sum(1 for x in legs if x["pa_paid"]),
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


def parlay_board(legs: pd.DataFrame, live: pd.DataFrame | None, now=None, normalize=None,
                 official: pd.DataFrame | None = None, covered=None) -> dict:
    """La pestaña: cada parlay con bono (del día y de un horario) con sus
    piernas, del más reciente; si ya vio las alineaciones que necesita (ready)
    y cuáles faltan (waiting); el historial de todos y por tipo.
    official: filas de `matches` (h, a, date, goles, córners, tarjetas) para
    lo que ESPN no cubre. covered: las ligas con alineación en ESPN."""
    from src.models.program_parlays import LOCK_MINUTES, RULE
    now = _utc(now if now is not None else pd.Timestamp.now(tz="UTC"))
    idx = _live_index(live.to_dict("records") if live is not None and not live.empty else [], normalize)
    off = official.to_dict("records") if official is not None and not official.empty else []
    rows = legs.to_dict("records") if legs is not None and not legs.empty else []
    groups = {}
    for r in rows:
        if r.get("rule") != RULE or r.get("slot") is None or pd.isna(r.get("slot")):
            continue
        key, kick = str(r["match"]).lower(), _utc(r["match_date"])
        scores = [] if _str(r.get("result")) else [_live_for(idx, key, kick), _official_for(off, key, kick)]
        gk = (_utc(r["slot"]), str(r.get("kind") or "horario"), int(r.get("seq") or 1))
        g = groups.setdefault(gk, {"day": pd.Timestamp(r["day"]).strftime("%Y-%m-%d"), "legs": []})
        g["legs"].append(leg_view(r, scores, now, covered))
    out = []
    for (slot, kind, seq) in sorted(groups, key=lambda k: (k[0], KINDS.index(k[1]) if k[1] in KINDS else 9, k[2]),
                                    reverse=True):
        ls = sorted(groups[(slot, kind, seq)]["legs"], key=lambda x: (x["kickoff"], x["rank"]))
        first = min(_utc(x["kickoff"]) for x in ls)
        need = [x for x in ls if kind == "horario" or _utc(x["kickoff"]) - first <= pd.Timedelta(minutes=30)]
        out.append({"day": groups[(slot, kind, seq)]["day"], "slot": slot.isoformat(), "kind": kind, "seq": seq,
                    "lock": (slot - pd.Timedelta(minutes=LOCK_MINUTES)).isoformat(),
                    "ready": all(x["lineup_ok"] for x in need),
                    "waiting": [x["match"] for x in need if not x["lineup_ok"]],
                    "legs": ls, "parlay": combine(ls)})
    return {"parlays": out, "record": {"all": record(out), **{k: record(out, k) for k in KINDS}}}
