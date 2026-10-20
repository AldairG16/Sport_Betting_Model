"""
src/models/lineup_alerts.py
===========================
Alertas de alineación para PlayDoit (20-oct-26, a pedido del dueño).

Cuando sale la alineación (~1 h antes) y a un equipo le faltan titulares
habituales o su goleador, el rival gana probabilidad. Pinnacle ajusta en
minutos; una casa como PlayDoit puede tardar. La alerta dice el momio
MÍNIMO que debe pagar PlayDoit por el rival (y por "empate o rival") para
que haya valor contra el precio de Pinnacle de ese momento, con el mismo
margen del filtro de dinero real (2 pt, min_odds.min_american).

Lo que se sabe y lo que no (medido el 20-oct, 4,389 partidos 2024-26):
  - al CIERRE la casa ya cuenta las bajas casi del todo (al local con 2+
    titulares fuera le dio 3 pt de más; a precio promedio no alcanza);
  - si PlayDoit tarda en ajustar, solo se puede medir en vivo. Por eso cada
    alerta queda guardada (lineup_alerts) y el weekly mide cuánto valía su
    momio mínimo contra el cierre de Pinnacle y cómo terminó el partido.

Titular habitual: 70%+ de las titularidades en los últimos 15 partidos
terminados del equipo (mínimo 6). Avisa con 4+ titulares habituales fuera
(una rotación grande, 4% de los equipo-partido). Medido el 20-oct contra el
cierre, el rival gana más de lo esperado cuanto más faltan: 2 fuera +0.8 pt,
3 +1.9, 4 +2.6, 5+ +8.2 (n=101). Con 2 fuera saldría en la mitad de los
partidos (rotación normal). La falta del goleador NO avisa: ahí la casa
exagera (el rival ganó 34.4% contra 36.0% esperado); solo se menciona.
Una alerta por equipo, partido y mercado.
"""

import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

REGULAR_SHARE = 0.70
HISTORY_MATCHES = 15
MIN_TEAM_MATCHES = 6
MIN_TOP_GOALS = 3
TRIGGER_MISSING = 4
MAX_PIN_AGE_H = 12          # precio de Pinnacle más viejo que esto: sin alerta
MIN_WIN_PROB = 0.20         # "el rival gana" solo si Pinnacle le da 20%+: los longshots pierden
                            # aun contra el precio justo (buscador de ventajas: −24% con ≤15%)

TABLE = "lineup_alerts"
DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id BIGSERIAL PRIMARY KEY,
        sport_key TEXT NOT NULL,
        kickoff TIMESTAMPTZ NOT NULL,
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        weakened TEXT NOT NULL,
        missing JSONB,
        top_scorer_out TEXT,
        market TEXT NOT NULL,
        pin_prob DOUBLE PRECISION,
        pin_fetched_at TIMESTAMPTZ,
        min_american INT,
        alerted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        pin_close_prob DOUBLE PRECISION,
        result TEXT,
        resolved_at TIMESTAMPTZ,
        UNIQUE (sport_key, kickoff, weakened, market)
    )
"""
# Revisión de Claude (src/models/lineup_review.py): una por equipo y partido;
# el costo va solo en la primera fila del grupo para sumarlo una vez.
ALTER = f"""
    ALTER TABLE {TABLE}
        ADD COLUMN IF NOT EXISTS analyst_verdict TEXT,
        ADD COLUMN IF NOT EXISTS analyst_absences TEXT,
        ADD COLUMN IF NOT EXISTS analyst_reason TEXT,
        ADD COLUMN IF NOT EXISTS analyst_cost DOUBLE PRECISION
"""


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def team_profile(history: list) -> dict:
    """history: partidos TERMINADOS del equipo, del más reciente, cada uno
    {"starters": [ids], "goals": {id: goles}, "names": {id: nombre}}."""
    h = history[:HISTORY_MATCHES]
    n = len(h)
    starts, goals, names = {}, {}, {}
    for m in h:
        for pid in m.get("starters") or []:
            starts[pid] = starts.get(pid, 0) + 1
        for pid, g in (m.get("goals") or {}).items():
            goals[pid] = goals.get(pid, 0) + int(g or 0)
        names.update(m.get("names") or {})
    regulars = {pid for pid, c in starts.items() if n >= MIN_TEAM_MATCHES and c / n >= REGULAR_SHARE}
    top = max(goals, key=goals.get) if goals else None
    if top is not None and goals[top] < MIN_TOP_GOALS:
        top = None
    return {"n": n, "regulars": regulars, "top": top, "top_goals": goals.get(top, 0), "names": names}


def absences(starters: list, profile: dict) -> dict:
    """Titulares habituales que no salen y si falta el goleador."""
    if profile["n"] < MIN_TEAM_MATCHES:
        return {"missing": [], "top_out": None, "known": False}
    xi = set(starters or [])
    missing = sorted(profile["regulars"] - xi)
    top = profile["top"]
    return {"missing": [profile["names"].get(p, p) for p in missing],
            "top_out": profile["names"].get(top, top) if top and top not in xi else None,
            "known": True}


def triggered(a: dict) -> bool:
    return a["known"] and len(a["missing"]) >= TRIGGER_MISSING


def alerts_for_match(fixture: dict, lineups: dict, profiles: dict, now=None) -> list:
    """
    fixture: fila de upcoming_matches (sport_key, home_team_norm, away_team_norm,
    match_date, pin_*, odds_fetched_at). lineups: {equipo: [titulares]}.
    profiles: {equipo: team_profile}. Una alerta por mercado del rival: gana y
    "empate o rival", con el precio de Pinnacle de ese momento.
    """
    from src.features.pinnacle import pinnacle_prob
    from src.utils.min_odds import min_american
    now = now or datetime.now(timezone.utc)
    fetched = fixture.get("odds_fetched_at")
    if fetched is None or pd.isna(fetched):
        return []
    fetched = pd.Timestamp(fetched)
    fetched = fetched.tz_localize("UTC") if fetched.tzinfo is None else fetched
    if (pd.Timestamp(now) - fetched).total_seconds() > MAX_PIN_AGE_H * 3600:
        return []
    home, away = fixture["home_team_norm"], fixture["away_team_norm"]
    out = []
    for team, is_home in ((home, True), (away, False)):
        if team not in lineups or team not in profiles:
            continue
        a = absences(lineups[team], profiles[team])
        if not triggered(a):
            continue
        rival_win, rival_dc = ("away_win", "dc_x2") if is_home else ("home_win", "dc_1x")
        for market in (rival_win, rival_dc):
            p = pinnacle_prob(market, fixture)
            if p is None or (market == rival_win and p < MIN_WIN_PROB):
                continue
            out.append({"sport_key": fixture["sport_key"], "kickoff": fixture["match_date"],
                        "home": home, "away": away, "weakened": team,
                        "missing": a["missing"], "top_scorer_out": a["top_out"], "market": market,
                        "pin_prob": float(p), "pin_fetched_at": fetched.to_pydatetime(),
                        "min_american": min_american(p)})
    return out


def _american(v) -> str:
    return "—" if v is None else (f"+{v}" if v > 0 else str(v))


def format_alerts(alerts: list) -> str:
    """Un mensaje por partido, en palabras del dueño (momios americanos)."""
    if not alerts:
        return ""
    a0 = alerts[0]
    kick = pd.Timestamp(a0["kickoff"]).tz_convert("America/Mexico_City")
    rival = a0["away"] if a0["weakened"] == a0["home"] else a0["home"]
    why = []
    if a0["missing"]:
        why.append(f"no salen {len(a0['missing'])} titulares habituales ({', '.join(a0['missing'][:4])})")
    if a0["top_scorer_out"]:
        why.append(f"tampoco sale su goleador ({a0['top_scorer_out']})")
    age = (datetime.now(timezone.utc) - pd.Timestamp(a0["pin_fetched_at"])).total_seconds() / 60
    lines = [f"📋 <b>ALINEACIÓN — {a0['home'].title()} vs {a0['away'].title()}</b> ({kick:%H:%M})",
             f"A {a0['weakened'].title()} " + " y ".join(why) + "."]
    for a in alerts:
        label = f"{rival.title()} gana" if a["market"] in ("home_win", "away_win") else f"Empate o {rival.title()}"
        lines.append(f"• {label}: apuesta solo si PlayDoit paga <b>{_american(a['min_american'])}</b> o más "
                     f"(Pinnacle lo ve en {a['pin_prob'] * 100:.0f}%)")
    lines.append(f"Precio de Pinnacle de hace {age:.0f} min. Si PlayDoit ya bajó el momio, no hay apuesta.")
    return "\n".join(lines)


def value_at_close(min_american: int | None, pin_close_prob: float | None) -> float | None:
    """Cuánto valía el momio mínimo contra el cierre de Pinnacle (por unidad)."""
    if min_american is None or pin_close_prob is None:
        return None
    dec = 1 + (min_american / 100 if min_american > 0 else 100 / -min_american)
    return pin_close_prob * dec - 1


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

def load_profiles(engine, teams: list) -> dict:
    """team_profile de cada equipo con sus partidos terminados en `lineups`."""
    if not teams:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT team, starters, goals, names FROM lineups
            WHERE completed AND team = ANY(:t) ORDER BY kickoff DESC
        """), {"t": list(teams)}).fetchall()
    hist = {}
    for team, st, gl, nm in rows:
        load = lambda v: v if isinstance(v, (list, dict)) else json.loads(v or "null")
        hist.setdefault(team, []).append({"starters": load(st) or [], "goals": load(gl) or {},
                                          "names": load(nm) or {}})
    return {t: team_profile(h) for t, h in hist.items()}


def run_lineup_alerts(engine=None, window_min: tuple = (5, 80), send=None, use_review: bool = True) -> list:
    """Closing: lee las alineaciones publicadas, detecta bajas y avisa una sola
    vez por equipo y partido. Devuelve las alertas nuevas."""
    from src.features.espn_lineups import capture_upcoming
    if engine is None:
        from config.database import engine
    rows = capture_upcoming(window_min, engine)
    if not rows:
        return []
    by_match = {}
    for r in rows:
        by_match.setdefault((r["sport_key"], str(r["kickoff"])), {})[r["team"]] = json.loads(r["starters"])
    teams = sorted({t for lu in by_match.values() for t in lu})
    profiles = load_profiles(engine, teams)
    new = []
    with engine.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text(ALTER))
        for (sport_key, kick), lineups in by_match.items():
            fx = conn.execute(text("""
                SELECT DISTINCT ON (home_team_norm, away_team_norm) *
                FROM upcoming_matches
                WHERE sport_key = :s AND match_date = CAST(:k AS timestamptz)
                  AND (home_team_norm = ANY(:t) OR away_team_norm = ANY(:t))
                ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
            """), {"s": sport_key, "k": kick, "t": list(lineups)}).mappings().first()
            if not fx:
                continue
            for a in alerts_for_match(dict(fx), lineups, profiles):
                got = conn.execute(text(f"""
                    INSERT INTO {TABLE} (sport_key, kickoff, home, away, weakened, missing, top_scorer_out,
                                         market, pin_prob, pin_fetched_at, min_american)
                    VALUES (:sport_key, :kickoff, :home, :away, :weakened, CAST(:missing AS jsonb),
                            :top_scorer_out, :market, :pin_prob, :pin_fetched_at, :min_american)
                    ON CONFLICT (sport_key, kickoff, weakened, market) DO NOTHING
                    RETURNING id
                """), {**a, "missing": json.dumps(a["missing"], ensure_ascii=False)}).first()
                if got:
                    new.append({**a, "id": got[0]})
    # Fuera de la transacción: la revisión de Claude tarda ~10-20 s por grupo.
    if new:
        from src.models.lineup_review import review_alert, review_line
        if send is None:
            from scripts.notify_telegram import send_message as send
        groups = {}
        for a in new:                                           # un grupo por equipo y partido
            groups.setdefault((a["sport_key"], str(a["kickoff"]), a["weakened"]), []).append(a)
        for group in groups.values():
            review = review_alert(group, engine) if use_review else None
            if review:
                with engine.begin() as conn:
                    for i, a in enumerate(group):
                        conn.execute(text(f"""
                            UPDATE {TABLE} SET analyst_verdict = :v, analyst_absences = :b,
                                analyst_reason = :r, analyst_cost = :c WHERE id = :id
                        """), {"v": review["verdict"], "b": review["absences"], "r": review["reason"],
                               "c": review["cost"] if i == 0 else None, "id": a["id"]})
            line = review_line(review)
            msg = format_alerts(group)
            send(f"{msg}\n{line}" if line else msg)
    print(f"📋 Alineaciones: {len(rows) // 2} partidos con alineación, {len(new)} alertas nuevas")
    return new


def resolve_alerts(engine=None) -> int:
    """Weekly: cierre de Pinnacle (última foto de odds_history en los 150 min
    previos) y resultado de cada alerta ya jugada."""
    from src.features.pinnacle import pinnacle_prob
    from src.utils.closing_quality import CLOSING_MAX_LEAD_MIN
    if engine is None:
        from config.database import engine
    with engine.begin() as conn:
        conn.execute(text(DDL))
        pend = conn.execute(text(f"""
            SELECT id, sport_key, kickoff, home, away, market FROM {TABLE}
            WHERE result IS NULL AND kickoff < NOW() - INTERVAL '3 hours'
        """)).mappings().all()
        done = 0
        for a in pend:
            snap = conn.execute(text(f"""
                SELECT pin_home_odds, pin_draw_odds, pin_away_odds FROM odds_history
                WHERE match_key LIKE :mk AND captured_at < :k
                  AND captured_at >= CAST(:k AS timestamptz) - INTERVAL '{CLOSING_MAX_LEAD_MIN} minutes'
                  AND pin_home_odds IS NOT NULL
                ORDER BY captured_at DESC LIMIT 1
            """), {"mk": f"{a['home']}_{a['away']}%", "k": a["kickoff"]}).mappings().first()
            close = pinnacle_prob(a["market"], dict(snap)) if snap else None
            res = conn.execute(text("""
                SELECT home_goals, away_goals FROM matches
                WHERE LOWER(home_team) = :h AND LOWER(away_team) = :a
                  AND date BETWEEN CAST(:k AS date) - 1 AND CAST(:k AS date) + 1
                  AND home_goals IS NOT NULL LIMIT 1
            """), {"h": a["home"], "a": a["away"], "k": a["kickoff"]}).first()
            if res is None:
                continue
            hg, ag = res
            won = {"home_win": hg > ag, "away_win": ag > hg, "dc_1x": hg >= ag, "dc_x2": ag >= hg}[a["market"]]
            conn.execute(text(f"""
                UPDATE {TABLE} SET pin_close_prob = :c, result = :r, resolved_at = NOW() WHERE id = :id
            """), {"c": close, "r": "win" if won else "loss", "id": a["id"]})
            done += 1
    return done


def weekly_lines(engine=None) -> list:
    """Bloque del reporte: cuántas alertas y cuánto valía su momio mínimo."""
    if engine is None:
        from config.database import engine
    with engine.connect() as conn:
        if conn.execute(text(f"SELECT to_regclass('{TABLE}')")).scalar() is None:
            return []
        df = pd.read_sql(text(f"SELECT * FROM {TABLE} WHERE result IS NOT NULL AND min_american IS NOT NULL"), conn)
    if df.empty:
        return ["", "📋 <b>Alertas de alineación</b>: todavía ninguna jugada."]
    df["v"] = [value_at_close(m, c) for m, c in zip(df["min_american"], df["pin_close_prob"])]
    v = df["v"].dropna()
    dec = df["min_american"].map(lambda m: 1 + (m / 100 if m > 0 else 100 / -m))
    profit = (dec - 1).where(df["result"] == "win", -1.0)
    lines = ["", f"📋 <b>Alertas de alineación</b> ({len(df)} jugadas, a su momio mínimo):",
             f"  {int((df['result'] == 'win').sum())}G/{int((df['result'] == 'loss').sum())}P · "
             f"{profit.sum():+.2f}u si PlayDoit pagaba el mínimo"]
    if len(v):
        lines.append(f"  contra el cierre de Pinnacle: {v.mean() * 100:+.1f}% (n={len(v)}). "
                     "Positivo = el momio mínimo seguía teniendo valor al cierre.")
    # ¿sirve la revisión de Claude? (src/models/lineup_review.py)
    if "analyst_verdict" in df.columns and df["analyst_verdict"].notna().any():
        label = {"CONFIRMA": "Claude confirmó", "DUDA": "Claude sin info", "DESCARTA": "Claude descartó"}
        for verdict, g in df.groupby("analyst_verdict"):
            pg = profit[g.index]
            lines.append(f"  {label.get(verdict, verdict)}: {len(g)} · "
                         f"{int((g['result'] == 'win').sum())}G/{int((g['result'] == 'loss').sum())}P · "
                         f"{pg.sum():+.2f}u")
        cost = pd.to_numeric(df.get("analyst_cost"), errors="coerce").sum()
        lines.append(f"  revisiones de Claude: ${cost:.2f} en total")
    return lines
