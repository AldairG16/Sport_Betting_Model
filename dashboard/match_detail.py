"""
dashboard/match_detail.py
=========================
Estadísticas de un partido al abrirlo en el dashboard (v1.10.0, 21-oct-26).

El dueño: "quiero abrir el partido y ver sus últimos 5 encuentros, con
resultados, goles, tiros de esquina y la alineación". Por equipo: sus
últimos 5 partidos (marcador, goles, córners, tarjetas con la roja = 1) con
el once que usó en cada uno, y la alineación del partido (la confirmada si
ya salió; si no, la más probable). Además, los últimos 5 entre ellos.

Funciones puras: dashboard/app.py hace las consultas (matches y lineups).
"""

import json
import math

import pandas as pd

from dashboard.display import league_name, team_name

LAST_N = 5
PROBABLE_FROM = 10          # el once probable sale de sus últimos 10 partidos con alineación
CONFIRMED_WINDOW = pd.Timedelta(hours=3)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _load(v):
    if isinstance(v, (list, dict)):
        return v
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def _corners(row) -> tuple:
    """(local, visitante); (None, None) sin dato. Un 0-0 en córners es un
    hueco de la fuente (Liga MX 2025 venía así), no un partido real."""
    h, a = _num(row.get("home_corners")), _num(row.get("away_corners"))
    if h is None or a is None or h + a == 0:
        return None, None
    return h, a


def _cards(row, side: str):
    y = _num(row.get(f"{side}_yellow"))
    if y is None:
        return None
    return y + (_num(row.get(f"{side}_red")) or 0)        # roja = 1, como PlayDoit


def _xi(row) -> dict | None:
    """Once de una fila de `lineups`: formación y nombres en orden."""
    if row is None:
        return None
    starters = _load(row.get("starters")) or []
    names = {str(k): v for k, v in (_load(row.get("names")) or {}).items()}
    if not starters:
        return None
    return {"formation": row.get("formation") or "",
            "players": [names.get(str(p), str(p)) for p in starters]}


def _lineup_for(lineups: list, team: str, when) -> dict | None:
    """La alineación del equipo en el partido de `when` (±1 día)."""
    d = _ts(when)
    for r in lineups:
        if r["team"] == team and abs(_ts(r["kickoff"]) - d) <= pd.Timedelta(days=1):
            return r
    return None


def team_last(matches: list, lineups: list, team: str, before, n: int = LAST_N) -> dict:
    """Los últimos `n` partidos del equipo antes de `before` y sus promedios."""
    b = _ts(before)
    rows = [m for m in matches if team in (m["h"], m["a"]) and _ts(m["date"]) < b]
    rows.sort(key=lambda m: _ts(m["date"]), reverse=True)
    out, tot = [], {"gf": 0.0, "ga": 0.0, "cf": [], "ca": [], "cards": [], "w": 0, "d": 0, "l": 0}
    for m in rows[:n]:
        home = m["h"] == team
        gf, ga = (m["home_goals"], m["away_goals"]) if home else (m["away_goals"], m["home_goals"])
        gf, ga = int(gf), int(ga)
        cf, ca = _corners(m)
        if not home:
            cf, ca = ca, cf
        ch, ca_ = _cards(m, "home"), _cards(m, "away")
        cards = None if ch is None or ca_ is None else ch + ca_
        res = "G" if gf > ga else ("E" if gf == ga else "P")
        tot["gf"] += gf
        tot["ga"] += ga
        tot[{"G": "w", "E": "d", "P": "l"}[res]] += 1
        if cf is not None and ca is not None:
            tot["cf"].append(cf)
            tot["ca"].append(ca)
        if cards is not None:
            tot["cards"].append(cards)
        out.append({"date": _ts(m["date"]).isoformat(), "league": league_name(m.get("league")) if m.get("league") else "",
                    "opponent": team_name(m["a"] if home else m["h"]), "venue": "L" if home else "V",
                    "score": f"{gf}-{ga}", "result": res,
                    "corners": None if cf is None or ca is None else [int(cf), int(ca)],
                    "cards": None if cards is None else int(cards),
                    "xi": _xi(_lineup_for(lineups, team, m["date"]))})
    k = len(out)
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    return {"team": team_name(team), "matches": out,
            "summary": None if not k else {
                "n": k, "w": tot["w"], "d": tot["d"], "l": tot["l"],
                "goals_for": round(tot["gf"] / k, 1), "goals_against": round(tot["ga"] / k, 1),
                "corners_for": avg(tot["cf"]), "corners_against": avg(tot["ca"]),
                "cards": avg(tot["cards"])}}


def head_to_head(matches: list, home: str, away: str, before, n: int = LAST_N) -> list:
    """Los últimos `n` partidos entre los dos, con el marcador como se jugó."""
    b = _ts(before)
    rows = [m for m in matches if {m["h"], m["a"]} == {home, away} and _ts(m["date"]) < b]
    rows.sort(key=lambda m: _ts(m["date"]), reverse=True)
    out = []
    for m in rows[:n]:
        hc, ac = _corners(m)
        out.append({"date": _ts(m["date"]).isoformat(), "match": f"{team_name(m['h'])} vs {team_name(m['a'])}",
                    "score": f"{int(m['home_goals'])}-{int(m['away_goals'])}",
                    "corners": None if hc is None or ac is None else [int(hc), int(ac)]})
    return out


def match_lineup(lineups: list, team: str, kickoff) -> dict | None:
    """La alineación del partido: la confirmada si ya salió (~1 h antes);
    si no, la probable (los 11 que más han sido titulares en sus últimos
    10 partidos con alineación)."""
    k = _ts(kickoff)
    for r in lineups:
        if r["team"] == team and abs(_ts(r["kickoff"]) - k) <= CONFIRMED_WINDOW:
            xi = _xi(r)
            if xi:
                return {**xi, "confirmed": True}
    past = [r for r in lineups if r["team"] == team and _ts(r["kickoff"]) < k - CONFIRMED_WINDOW]
    past.sort(key=lambda r: _ts(r["kickoff"]), reverse=True)
    past = past[:PROBABLE_FROM]
    starts, names = {}, {}
    for r in past:
        for p in _load(r.get("starters")) or []:
            starts[str(p)] = starts.get(str(p), 0) + 1
        names.update({str(k2): v for k2, v in (_load(r.get("names")) or {}).items()})
    if len(past) < 3 or len(starts) < 11:
        return None
    top = sorted(starts, key=lambda p: -starts[p])[:11]
    return {"formation": past[0].get("formation") or "", "confirmed": False, "from": len(past),
            "players": [f"{names.get(p, p)} ({starts[p]} de {len(past)})" for p in top]}


def match_detail(matches: pd.DataFrame, lineups: pd.DataFrame, home: str, away: str, kickoff) -> dict:
    """Todo lo que muestra el partido abierto. home/away: nombres canónicos
    en minúsculas (los de matches y lineups)."""
    m = matches.to_dict("records") if not matches.empty else []
    lu = lineups.to_dict("records") if not lineups.empty else []
    return {"home": {**team_last(m, lu, home, kickoff), "lineup": match_lineup(lu, home, kickoff)},
            "away": {**team_last(m, lu, away, kickoff), "lineup": match_lineup(lu, away, kickoff)},
            "h2h": head_to_head(m, home, away, kickoff)}
