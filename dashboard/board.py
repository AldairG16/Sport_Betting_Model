"""
dashboard/board.py
==================
Dashboard v2.0 (22-oct-26): partidos, equipos y gráficas, sin texto de más.

El dueño: "necesito un mejor dashboard, que sea entendible, sin tanto texto
innecesario… quiero ver solo los partidos, equipos, gráficas y los partidos
anteriores desde septiembre y los próximos". Funciones puras para:
  - past_matches(): los partidos jugados desde SINCE en las ligas que analiza
    el programa, con lo que dijo el programa, el pick y las apuestas reales;
  - weekly_accuracy(): % de acierto del favorito por semana, programa y Pinnacle;
  - team_list() / team_view(): cada equipo con sus partidos y promedios;
  - bankroll_series() y picks_series(): las líneas de las gráficas.
dashboard/app.py hace las consultas.
"""

import math

import pandas as pd

from dashboard.display import league_name, market_name, team_name
from dashboard.visibility import who_was_right

SINCE = "2026-09-01"
# Desde aquí el modelo corregido (PR #44): los picks de antes (reconstruidos,
# 22-oct-26) son del modelo con el error del xG y se muestran aparte
MODEL_FIXED = pd.Timestamp("2026-10-22", tz="UTC")
SIDES = ("home", "draw", "away")
PLAYED = ("win", "loss", "push", "half_win", "half_loss")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _day(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.normalize()


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _corners(r) -> list | None:
    h, a = _num(r.get("home_corners")), _num(r.get("away_corners"))
    if h is None or a is None or h + a == 0:          # 0-0 = hueco de la fuente
        return None
    return [int(h), int(a)]


def _cards(r) -> int | None:
    hy, ay = _num(r.get("home_yellow")), _num(r.get("away_yellow"))
    if hy is None or ay is None:
        return None
    return int(hy + ay + (_num(r.get("home_red")) or 0) + (_num(r.get("away_red")) or 0))   # roja = 1


def _outcome(hg, ag) -> str:
    return "home" if hg > ag else ("draw" if hg == ag else "away")


def _by_match(rows: list, key=lambda r: r["match"]) -> dict:
    """{nombre del partido: [filas]} para buscar con ±1 día."""
    out = {}
    for r in rows:
        out.setdefault(str(key(r)).lower(), []).append(r)
    return out


def _near(index: dict, match: str, day, date_key="match_date") -> list:
    d = _day(day)
    return [r for r in index.get(match, []) if abs((_day(r[date_key]) - d).days) <= 1]


def past_matches(matches: pd.DataFrame, mvs: pd.DataFrame, picks: pd.DataFrame, bets: pd.DataFrame,
                 normalize=None) -> list:
    """Cada partido jugado, del más reciente: marcador, córners, tarjetas,
    el favorito del programa y el de Pinnacle (¿acertaron?), el pick del
    programa y las apuestas reales. Nombres canónicos en minúsculas."""
    canon = (lambda m: m) if normalize is None else (
        lambda m: " vs ".join(normalize(x) for x in str(m).split(" vs ", 1)) if " vs " in str(m) else m)
    mv = _by_match(mvs.to_dict("records") if not mvs.empty else [])
    pk = _by_match(picks.to_dict("records") if not picks.empty else [])
    bt = _by_match(bets.to_dict("records") if not bets.empty else [], key=lambda r: canon(r["match"]))
    out = []
    for r in (matches.to_dict("records") if not matches.empty else []):
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        key = f"{r['h']} vs {r['a']}"
        res = _outcome(hg, ag)
        fav = None
        day = _day(r["date"])
        found = _near(mv, key, r["date"])
        if found:
            m = found[-1]
            # la fecha de `matches` es la de UTC: un 19:00 de México cae al día
            # siguiente. Con la hora del partido, el día de México (UTC-6)
            day = (_day(m["match_date"]) if pd.Timestamp(m["match_date"]).tzinfo is None else
                   (pd.Timestamp(m["match_date"]).tz_convert("UTC") - pd.Timedelta(hours=6)).tz_localize(None).normalize())
            model = [_num(m.get(f"p_{s}")) for s in SIDES]
            pin = [_num(m.get(f"pin_{s}")) for s in SIDES]
            if None not in model:
                mf = SIDES[max(range(3), key=lambda i: model[i])]
                pf = SIDES[max(range(3), key=lambda i: pin[i])] if None not in pin else None
                fav = {"model": mf, "model_p": round(max(model), 3), "model_hit": mf == res,
                       "pin": pf, "pin_p": round(max(pin), 3) if pf else None, "pin_hit": pf == res if pf else None,
                       "probs": [round(x, 3) for x in model],
                       "pin_probs": [round(x, 3) for x in pin] if pf else None}
        pick = None
        for p in _near(pk, key, r["date"]):
            res_p = _str(p.get("result")) or "pending"
            when = pd.Timestamp(p["match_date"])
            when = when.tz_convert("UTC") if when.tzinfo else when.tz_localize("UTC")
            pick = {"label": p.get("label"), "result": res_p, "profit": _num(p.get("profit")),
                    "model": _num(p.get("model_prob")), "market": _num(p.get("market_prob")),
                    "winner": who_was_right(p.get("model_prob"), p.get("market_prob"), res_p),
                    "rebuilt": _str(p.get("source")) == "reconstruido", "old_model": bool(when < MODEL_FIXED)}
        bets_out = [{"market": market_name(b["market"]), "result": _str(b.get("result")) or "pending",
                     "profit": _num(b.get("profit"))}
                    for b in _near(bt, key, r["date"]) if _str(b.get("result")) != "stale"]
        out.append({"date": day.strftime("%Y-%m-%d"), "league": league_name(r["league"]),
                    "league_key": r["league"], "home": team_name(r["h"]), "away": team_name(r["a"]),
                    "key": key, "score": [hg, ag], "corners": _corners(r), "cards": _cards(r),
                    "fav": fav, "pick": pick, "bets": bets_out})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def weekly_accuracy(rows: list) -> list:
    """% de acierto del favorito por semana (lunes), programa y Pinnacle."""
    weeks = {}
    for r in rows:
        f = r.get("fav")
        if not f or f.get("pin_hit") is None:
            continue
        d = pd.Timestamp(r["date"])
        wk = (d - pd.Timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        w = weeks.setdefault(wk, {"week": wk, "n": 0, "model": 0, "pin": 0})
        w["n"] += 1
        w["model"] += int(f["model_hit"])
        w["pin"] += int(f["pin_hit"])
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        out.append({"week": wk, "n": w["n"], "model": round(w["model"] / w["n"], 3), "pin": round(w["pin"] / w["n"], 3)})
    return out


def summary(rows: list) -> dict:
    """Totales de la lista de partidos anteriores."""
    fav = [r["fav"] for r in rows if r.get("fav") and r["fav"].get("pin_hit") is not None]
    picks = [r["pick"] for r in rows if r.get("pick") and r["pick"]["result"] in PLAYED]
    bets = [b for r in rows for b in r["bets"] if b["result"] in PLAYED]

    def rec(ps):
        return {"n": len(ps), "won": sum(1 for p in ps if p["result"] in ("win", "half_win")),
                "profit": round(sum(p["profit"] for p in ps if p["profit"] is not None), 2),
                "priced": sum(1 for p in ps if p["profit"] is not None)}
    return {"matches": len(rows), "analyzed": len(fav),
            "model_hit": round(sum(f["model_hit"] for f in fav) / len(fav), 3) if fav else None,
            "pin_hit": round(sum(f["pin_hit"] for f in fav) / len(fav), 3) if fav else None,
            "picks_fixed": rec([p for p in picks if not p.get("old_model")]),
            "picks_old": rec([p for p in picks if p.get("old_model")]),
            "bets": len(bets), "bets_profit": round(sum(b["profit"] or 0 for b in bets), 2)}


def team_list(matches: pd.DataFrame) -> list:
    """Equipos con partidos desde SINCE: nombre canónico, visible, su liga y cuántos."""
    if matches.empty:
        return []
    teams = {}
    for r in matches.to_dict("records"):
        for t in (r["h"], r["a"]):
            e = teams.setdefault(t, {"key": t, "name": team_name(t), "n": 0, "leagues": {}})
            e["n"] += 1
            e["leagues"][r["league"]] = e["leagues"].get(r["league"], 0) + 1
    out = []
    for e in teams.values():
        lg = max(e["leagues"], key=e["leagues"].get)
        out.append({"key": e["key"], "name": e["name"], "n": e["n"], "league": league_name(lg), "league_key": lg})
    return sorted(out, key=lambda e: (e["league"], e["name"]))


def team_view(team: str, matches: pd.DataFrame, upcoming: pd.DataFrame) -> dict:
    """Los partidos del equipo desde SINCE (para la tabla y las gráficas),
    sus promedios y sus próximos partidos con lo que dice el programa."""
    rows = []
    for r in (matches.to_dict("records") if not matches.empty else []):
        if team not in (r["h"], r["a"]):
            continue
        home = r["h"] == team
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        gf, ga = (hg, ag) if home else (ag, hg)
        c = _corners(r)
        cf, ca = (None, None) if c is None else ((c[0], c[1]) if home else (c[1], c[0]))
        rows.append({"date": _day(r["date"]).strftime("%Y-%m-%d"), "league": league_name(r["league"]),
                     "opponent": team_name(r["a"] if home else r["h"]), "venue": "L" if home else "V",
                     "gf": gf, "ga": ga, "result": "G" if gf > ga else ("E" if gf == ga else "P"),
                     "cf": cf, "ca": ca, "cards": _cards(r),
                     "key": f"{r['h']} vs {r['a']}"})
    rows.sort(key=lambda x: x["date"])
    n = len(rows)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    nxt = []
    for u in (upcoming.to_dict("records") if not upcoming.empty else []):
        h, _, a = str(u["match"]).partition(" vs ")
        if team not in (h, a):
            continue
        probs = [_num(u.get(f"p_{s}")) for s in SIDES]
        nxt.append({"date": pd.Timestamp(u["match_date"]).isoformat(), "league": league_name(u.get("league")),
                    "home": team_name(h), "away": team_name(a), "key": f"{h} vs {a}",
                    "probs": None if None in probs else [round(x, 3) for x in probs]})
    nxt.sort(key=lambda x: x["date"])
    return {"team": team_name(team), "key": team, "matches": rows, "upcoming": nxt,
            "record": {"w": sum(r["result"] == "G" for r in rows), "d": sum(r["result"] == "E" for r in rows),
                       "l": sum(r["result"] == "P" for r in rows)},
            "avg": {"gf": avg([r["gf"] for r in rows]), "ga": avg([r["ga"] for r in rows]),
                    "cf": avg([r["cf"] for r in rows if r["cf"] is not None]),
                    "ca": avg([r["ca"] for r in rows if r["ca"] is not None]),
                    "cards": avg([r["cards"] for r in rows if r["cards"] is not None])} if n else None}


def bankroll_series(hist: pd.DataFrame) -> dict:
    """El bankroll al cierre de cada día (bankroll_history: date, balance)."""
    if hist.empty:
        return {"dates": [], "values": []}
    h = hist.copy()
    h["d"] = pd.to_datetime(h["date"], utc=True).dt.strftime("%Y-%m-%d")
    last = h.groupby("d")["balance"].last()
    return {"dates": list(last.index), "values": [round(float(v), 2) for v in last.values]}


def picks_series(picks: pd.DataFrame) -> dict:
    """Unidades acumuladas de los picks del programa por día: una línea para
    el modelo con error (antes del 22-oct) y otra para el corregido."""
    empty = {"dates": [], "old": [], "fixed": []}
    if picks.empty:
        return empty
    p = picks[picks["result"].isin(PLAYED)].copy()
    p["profit"] = pd.to_numeric(p["profit"], errors="coerce")
    p = p.dropna(subset=["profit"])
    if p.empty:
        return empty
    when = pd.to_datetime(p["match_date"], utc=True)
    p["d"] = when.dt.strftime("%Y-%m-%d")
    p["old"] = when < MODEL_FIXED
    dates = sorted(p["d"].unique())
    out = {"dates": dates}
    for name, mask in (("old", p["old"]), ("fixed", ~p["old"])):
        daily = p[mask].groupby("d")["profit"].sum()
        if daily.empty:
            out[name] = [None] * len(dates)
            continue
        cum, total, first = [], 0.0, daily.index.min()
        for d in dates:
            total += float(daily.get(d, 0.0))
            cum.append(round(total, 2) if d >= first and (name == "fixed" or d <= daily.index.max()) else None)
        out[name] = cum
    return out
