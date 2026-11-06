"""
src/models/early_payout.py
==========================
El Pago Anticipado de PlayDoit (v2.9.0, 6-nov-26).

Lo revisó la extensión de Claude en el navegador del dueño, sin apostar, el
5-nov-26: "Pago Anticipado Futbol – 2 Goles de Ventaja". Si el equipo al que
se apuesta llega a ir 2 goles arriba en cualquier momento, PlayDoit paga la
apuesta como ganada aunque después no gane. Solo el mercado "Resultado final"
(1X2, 90 min), solo antes del partido, en sencillas, parlays y "Crear
apuesta"; no aplica al empate ni a los momios mejorados. La etiqueta "PA"
salió en las ligas de PA_LEAGUES; en Escocia no (Rangers y Hearts sin ella).
La Europa League no se pudo revisar (sin partidos) y el sistema no la baja.

Cuánto vale (24 mil partidos de ESPN con el minuto de cada gol, Europa 1ª
división): un favorito de 65% o más gana 75.6% y con el pago anticipado
76.8% (+1.26 pts). Al precio promedio de las casas (≈ PlayDoit, medido +1%
en 13 favoritos) eso deja la sencilla en ≈ +1-2% en vez de ≈ 0%.

Para liquidarlo hace falta la ventaja más grande de cada equipo durante el
partido: sale de los goles del marcador de ESPN (live_scores.parse_live,
home_max_lead / away_max_lead), que ya se lee cada closing. ESPN da cada gol
con el equipo al que se le cuenta (en un autogol, el rival del que lo metió;
verificado en 300 partidos, 24 con autogol): si la suma no da el marcador,
la ventaja queda sin dato y no se paga nada por adelantado.
"""

import re

import pandas as pd

PA_LEAD = 2
WAIT_HOURS = 30               # una perdida con pago anticipado espera la ventaja de ESPN hasta 30 h
PA_MARKETS = ("home_win", "away_win")
PA_LEAGUES = ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga",
              "soccer_france_ligue_one", "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
              "soccer_turkey_super_league", "soccer_belgium_first_div", "soccer_greece_super_league",
              "soccer_uefa_champs_league")


def applies(league, market) -> bool:
    """¿PlayDoit le da pago anticipado a esta apuesta?"""
    return str(league or "") in PA_LEAGUES and str(market or "") in PA_MARKETS


def _minute(d: dict) -> float:
    """Minuto del gol para ordenarlos; el descuento del primer tiempo (45'+2')
    va antes del 46'."""
    shown = str((d.get("clock") or {}).get("displayValue") or "")
    m = re.match(r"\s*(\d+)'?\s*(?:\+\s*(\d+))?", shown)
    if m:
        base, extra = int(m.group(1)), int(m.group(2) or 0)
        return base + extra / 100 if extra else float(base)
    try:
        return float((d.get("clock") or {}).get("value") or 0) / 60
    except (TypeError, ValueError):
        return 0.0


def max_leads(details: list, home_id: str, home_goals, away_goals) -> tuple:
    """Función pura: (ventaja más grande del local, del visitante) con los
    goles del marcador de ESPN (details con scoringPlay). (None, None) si la
    suma de los goles no da el marcador."""
    goals = sorted((d for d in (details or []) if d.get("scoringPlay")), key=_minute)
    h = a = mh = ma = 0
    for d in goals:
        if str((d.get("team") or {}).get("id", "")) == str(home_id):
            h += 1
        else:
            a += 1
        mh, ma = max(mh, h - a), max(ma, a - h)
    try:
        ok = (h, a) == (int(home_goals), int(away_goals))
    except (TypeError, ValueError):
        ok = False
    return (mh, ma) if ok else (None, None)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def lead_index(live: list, normalize=None) -> dict:
    """{'local vs visitante' normalizado: [(kickoff UTC, ventaja local, ventaja visitante)]}
    de las filas TERMINADAS de live_scores con la ventaja guardada."""
    canon = (lambda x: x) if normalize is None else normalize
    idx = {}
    for r in live:
        if r.get("status") != "final" or _num(r.get("home_max_lead")) is None:
            continue
        key = f"{canon(r['home'])} vs {canon(r['away'])}".lower()
        idx.setdefault(key, []).append((pd.Timestamp(r["kickoff"]), int(r["home_max_lead"]), int(r["away_max_lead"])))
    return idx


def lead_of(idx: dict, match: str, kickoff, market: str, normalize=None):
    """La ventaja más grande del equipo de la apuesta (None = sin dato)."""
    h, _, a = str(match).partition(" vs ")
    if normalize:
        h, a = normalize(h), normalize(a)
    k = pd.Timestamp(kickoff)
    k = k.tz_convert("UTC") if k.tzinfo else k.tz_localize("UTC")
    for t, lh, la in idx.get(f"{h} vs {a}".lower(), []):
        t = t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")
        if abs((t - k).total_seconds()) <= 3 * 3600:
            return lh if market == "home_win" else la
    return None


def paid_early(result, lead) -> bool:
    """¿La apuesta perdida se cobra por el pago anticipado?"""
    return result == "loss" and lead is not None and lead >= PA_LEAD


def apply_to_plan(plan: list, rows: dict, idx: dict, normalize=None, now=None) -> list:
    """Sobre lo que liquidó plan_shadow_resolutions ([{id, result, profit}]),
    la pierna o el pick con pago anticipado que iba 2 arriba se cobra:
    'win' a su momio, pa_paid True. rows: {id: fila con match, match_date,
    market, league y el momio en 'odds'}. Una perdida con pago anticipado sin
    la ventaja de ESPN espera WAIT_HOURS desde el partido (sale del plan y
    sigue pendiente); después se liquida perdida. Devuelve el plan con pa_paid."""
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    out = []
    for p in plan:
        r = rows.get(p["id"]) or {}
        paid = False
        if applies(r.get("league"), r.get("market")) and p.get("result") == "loss":
            lead = lead_of(idx, r.get("match"), r.get("match_date"), r.get("market"), normalize)
            odds = _num(r.get("odds"))
            if lead is None:
                k = pd.Timestamp(r.get("match_date"))
                k = k.tz_convert("UTC") if k.tzinfo else k.tz_localize("UTC")
                if now - k < pd.Timedelta(hours=WAIT_HOURS):
                    continue
            elif paid_early(p["result"], lead) and odds:
                p = {**p, "result": "win", "profit": round(odds - 1, 4)}
                paid = True
        out.append({**p, "pa_paid": paid})
    return out


def load_leads(engine, since, normalize=None) -> dict:
    """lead_index de live_scores desde `since` (la columna nace con v2.9.0)."""
    from sqlalchemy import text
    try:
        live = pd.read_sql(text("""SELECT home, away, kickoff, status, home_max_lead, away_max_lead FROM live_scores
                                   WHERE kickoff >= :s AND home_max_lead IS NOT NULL"""), engine,
                           params={"s": pd.Timestamp(since).to_pydatetime()})
    except Exception as e:
        if "does not exist" in str(e):
            return {}
        raise
    return lead_index(live.to_dict("records"), normalize)
