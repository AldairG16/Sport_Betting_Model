"""
dashboard/lineup_verdict.py
===========================
Veredicto por partido cuando salen las alineaciones (v1.7.1, 21-oct-26).

Con los onces de ESPN que el closing guarda (tabla lineups) y el perfil de
cada equipo (titulares habituales: 70%+ de sus últimos 15 partidos), dice
por equipo quién falta y si está en la banca, cuánto se movió Pinnacle desde
la corrida de la mañana y el veredicto. Lo que se midió con 40,000
partidos: las alineaciones ya van en el momio (Pinnacle ajusta en minutos);
solo hay jugada si PlayDoit tarda en ajustar, y para eso está la alerta de
alineación (4+ titulares fuera) con su momio mínimo.
Funciones puras: dashboard/app.py hace las consultas.
"""

import pandas as pd

from dashboard.display import league_name
from src.models.lineup_alerts import MIN_TEAM_MATCHES, TRIGGER_MISSING
from src.utils.min_odds import fmt_american

MATCH_WINDOW_MIN = 90          # la alineación es del partido si arranca a ±90 min
SIDE_ES = {"home": "local", "draw": "empate", "away": "visitante"}
MARKET_ES = {"away_win": "Gana {r}", "home_win": "Gana {r}", "dc_x2": "Empate o {r}", "dc_1x": "{r} o empate"}


def _ids(v) -> list:
    return [str(x) for x in (v or [])]


def team_lineup(row: dict | None, profile: dict | None) -> dict:
    """Once publicado de un equipo contra su perfil: bajas, si están en la
    banca o fuera de la convocatoria, y si falta el goleador."""
    if row is None:
        return {"published": False}
    starters = _ids(row.get("starters"))
    names = {str(k): v for k, v in (row.get("names") or {}).items()}
    out = {"published": True, "formation": row.get("formation") or ""}
    if not profile or profile.get("n", 0) < MIN_TEAM_MATCHES:
        return {**out, "known": False, "missing": []}
    xi, bench = set(starters), set(names) - set(starters)
    regulars = {str(p) for p in profile["regulars"]}
    pnames = {str(k): v for k, v in (profile.get("names") or {}).items()}
    missing = [{"name": pnames.get(p) or names.get(p) or p, "bench": p in bench}
               for p in sorted(regulars - xi)]
    top = str(profile["top"]) if profile.get("top") is not None else None
    return {**out, "known": True, "missing": missing,
            "top_out": (pnames.get(top) or top) if top and top not in xi else None}


def _pick_row(rows: list, kickoff) -> dict | None:
    k = pd.Timestamp(kickoff)
    best = None
    for r in rows or []:
        d = abs((pd.Timestamp(r["kickoff"]) - k).total_seconds()) / 60
        if d <= MATCH_WINDOW_MIN and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _fair(odds: list) -> list | None:
    try:
        o = [float(x) for x in odds]
    except (TypeError, ValueError):
        return None
    if not all(x > 1 for x in o):
        return None
    inv = [1 / x for x in o]
    return [x / sum(inv) for x in inv]


def market_move(now_pin: list | None, morning_pin: list | None) -> dict | None:
    """El cambio más grande de Pinnacle desde la corrida de la mañana."""
    if not now_pin or not morning_pin:
        return None
    deltas = [(now_pin[i] - morning_pin[i], s) for i, s in enumerate(("home", "draw", "away"))]
    d, side = max(deltas, key=lambda x: abs(x[0]))
    return {"side": side, "delta": round(d, 3), "now": [round(x, 3) for x in now_pin]}


def _names(missing: list) -> str:
    return ", ".join(m["name"] + (" (banca)" if m["bench"] else "") for m in missing)


def verdict(home: str, away: str, lh: dict, la: dict, alerts: list, move: dict | None) -> dict:
    """Veredicto del partido: pending / alert / weak / rotation / normal."""
    if not lh.get("published") or not la.get("published"):
        return {"level": "pending", "text": "⏳ Alineación todavía no publicada (sale ~1 h antes del partido)."}
    mv = ""
    if move and abs(move["delta"]) >= 0.02:
        mv = (f" Pinnacle {'subió' if move['delta'] > 0 else 'bajó'} {abs(move['delta']) * 100:.0f} pts "
              f"al {SIDE_ES[move['side']]} desde la mañana: el mercado ya lo ajustó.")
    if alerts:
        a = alerts[0]
        lines = []
        for x in alerts:
            mk = MARKET_ES.get(x["market"], x["market"]).format(r=x["rival_name"])
            lines.append(f"{mk}: solo si PlayDoit paga {fmt_american(x['min_american'])} o mejor")
        text = f"⚠️ {a['weakened_name']} sale sin {a['n_missing']} titulares. " + "; ".join(lines) + "."
        if a.get("analyst_verdict"):
            text += f" Claude revisó noticias: {a['analyst_verdict'].lower()}"
            text += f" — {a['analyst_reason']}" if a.get("analyst_reason") else ""
        return {"level": "alert", "text": text + mv}
    worst = max(((t, side) for side, t in (("home", lh), ("away", la))),
                key=lambda ts: len(ts[0].get("missing") or []))
    team, side = worst
    n = len(team.get("missing") or [])
    tname = home if side == "home" else away
    if n >= TRIGGER_MISSING:
        return {"level": "weak", "text": f"⚠️ {tname} sale sin {n} titulares ({_names(team['missing'])}), "
                f"pero su rival tiene menos de 20% según Pinnacle: no hay alerta (los momios largos pierden).{mv}"}
    if n >= 2:
        return {"level": "rotation", "text": f"🔄 Rotación parcial: {tname} sin {_names(team['missing'])}. "
                f"Lo medido: el momio de cierre ya lo cuenta. Sin apuesta.{mv}"}
    unknown = [nm for nm, t in ((home, lh), (away, la)) if not t.get("known")]
    extra = f" (sin historial de alineaciones de {', '.join(unknown)} para comparar)" if unknown else ""
    return {"level": "normal", "text": f"✅ Alineaciones normales{extra}. Se mantiene el análisis: sin apuesta.{mv}"}


def lineup_verdicts(fixtures: pd.DataFrame, lineups: dict, profiles: dict, alerts: list,
                    morning: dict, now=None) -> list:
    """
    fixtures: partidos de hoy (upcoming_matches: nombres normalizados y de la
    API, sport_key, match_date, pin_*). lineups: {equipo: [filas de lineups]}.
    profiles: {equipo: team_profile}. alerts: filas de lineup_alerts de hoy.
    morning: {"local vs visitante": [p_home, p_draw, p_away] de Pinnacle en la
    corrida de la mañana (model_vs_sharp)}.
    """
    out = []
    for f in fixtures.itertuples(index=False):
        h, a = str(f.home_team_norm), str(f.away_team_norm)
        lh = team_lineup(_pick_row(lineups.get(h), f.match_date), profiles.get(h))
        la = team_lineup(_pick_row(lineups.get(a), f.match_date), profiles.get(a))
        mine = []
        for x in alerts:
            if {str(x.get("home")), str(x.get("away"))} == {h, a}:
                weak_norm = str(x.get("weakened"))
                mine.append({**x, "weakened_name": f.home_team if weak_norm == h else f.away_team,
                             "rival_name": f.away_team if weak_norm == h else f.home_team,
                             "n_missing": len(x.get("missing") or [])})
        now_pin = _fair([getattr(f, "pin_home_odds", None), getattr(f, "pin_draw_odds", None),
                         getattr(f, "pin_away_odds", None)])
        started = now is not None and pd.Timestamp(f.match_date) <= pd.Timestamp(now)
        # ya empezado, el momio es de en vivo: el movimiento no dice nada
        move = None if started else market_move(now_pin, morning.get(f"{h} vs {a}"))
        v = verdict(f.home_team, f.away_team, lh, la, mine, move)
        out.append({"match_date": pd.Timestamp(f.match_date).isoformat(),
                    "match": f"{f.home_team} vs {f.away_team}",
                    "league": league_name(f.sport_key) if f.sport_key else "",
                    "home": lh, "away": la, "move": move, **v})
    return out
