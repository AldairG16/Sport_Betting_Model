"""
src/features/live_scores.py
===========================
Marcadores del día desde ESPN (24-oct-26, a pedido del dueño: "también
quiero ver si se ganó o perdió").

El marcador oficial entra a `matches` en la noche (evening y late_results):
durante el día, los partidos que ya empezaron salían en el dashboard con ⏳.
Cada closing (cada 30 min) lee de ESPN (gratis, sin créditos) el marcador de
los partidos del sistema que empezaron en las últimas horas y lo guarda en
`live_scores`, aparte: `matches` y la liquidación oficial no cambian. El
dashboard muestra el marcador ("en vivo" o "final") y, con el final, si el
pick se ganó o se perdió, como resultado provisional (con la misma regla de
liquidación, save_bets.resolve_market) hasta que la noche lo confirma.

El emparejamiento es el estricto de las alineaciones (espn_lineups.assign_events).

26-oct-26, el dueño: "¿por qué solo los de hoy tienen tarjetas y córners y
los de ayer no? ni los del pasado". `matches` no los trae en MLS, Liga MX,
Argentina, Brasil, Noruega, Suecia, Japón, China ni la Champions (football-data
no los publica) y en las ligas grandes llegan con días de atraso. ESPN sí, en
todas sus ligas: backfill_finals guarda el final de los partidos ya jugados
(evening, últimos 3 días; la primera carga desde septiembre) con los nombres de
`matches`, y el dashboard los usa donde `matches` no los tiene
(board.fill_from_espn).
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

TABLE = "live_scores"
DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        sport_key TEXT NOT NULL,
        kickoff TIMESTAMPTZ NOT NULL,
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        home_goals INT, away_goals INT,
        home_corners INT, away_corners INT,
        home_cards INT, away_cards INT,
        status TEXT,
        clock TEXT,
        espn_id TEXT,
        fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (sport_key, kickoff, home)
    )
"""
LOOKBACK_H = 6              # partidos que empezaron en las últimas horas


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_live(data: dict | None) -> list:
    """Función pura: los partidos de un scoreboard que ya empezaron, con su
    marcador, córners y tarjetas (amarilla o roja = 1, como PlayDoit) y si
    terminó ('final') o sigue ('live')."""
    out = []
    for ev in (data or {}).get("events", []):
        c = (ev.get("competitions") or [{}])[0]
        st = (c.get("status") or ev.get("status") or {}).get("type", {})
        if st.get("state") not in ("in", "post"):
            continue
        side = {t.get("homeAway"): t for t in c.get("competitors", [])}
        if "home" not in side or "away" not in side or not ev.get("date"):
            continue
        details = c.get("details") or []
        cards = {}
        for d in details:
            kind = str(d.get("type", {}).get("text", ""))
            if "Yellow" in kind or "Red" in kind:
                tid = str(d.get("team", {}).get("id", ""))
                cards[tid] = cards.get(tid, 0) + 1
        row = {"event": str(ev.get("id")), "kick": pd.Timestamp(ev["date"]).to_pydatetime(),
               "status": "final" if st.get("completed") else "live",
               "clock": st.get("shortDetail") or st.get("detail") or ""}
        for s in ("home", "away"):
            t = side[s]
            score = t.get("score")
            stats = {x.get("name"): x.get("displayValue") for x in t.get("statistics", []) or []}
            tid = str(t.get("team", {}).get("id", ""))
            row.update({s: t.get("team", {}).get("displayName", ""),
                        f"{s}_goals": _int(score.get("value") if isinstance(score, dict) else score),
                        f"{s}_corners": _int(stats.get("wonCorners")),
                        f"{s}_cards": cards.get(tid, 0) if details else None})
        if row["home_goals"] is None or row["away_goals"] is None:
            continue
        out.append(row)
    return out


def capture_live_scores(engine=None, get=None, now=None) -> int:
    """Closing: guarda el marcador de ESPN de los partidos del sistema que
    empezaron en las últimas LOOKBACK_H horas. Devuelve cuántos guardó."""
    from src.features.espn_lineups import _get, assign_events, espn_leagues
    if engine is None:
        from config.database import engine
    now = now or datetime.now(timezone.utc)
    leagues = espn_leagues()
    with engine.connect() as conn:
        fixtures = [(r[0], r[1], r[2], r[3]) for r in conn.execute(text("""
            SELECT DISTINCT sport_key, home_team_norm, away_team_norm, match_date FROM upcoming_matches
            WHERE match_date BETWEEN :a AND :b
        """), {"a": now - timedelta(hours=LOOKBACK_H), "b": now}).fetchall() if r[0] in leagues]
    if not fixtures:
        return 0
    rows, read, asked = [], 0, 0
    for sport_key in sorted({f[0] for f in fixtures}):
        events = []
        days = {pd.Timestamp(f[3]).tz_convert("UTC").date() for f in fixtures if f[0] == sport_key}
        for day in days | {d - timedelta(days=1) for d in days}:       # ESPN usa su propia fecha
            asked += 1
            data = _get(leagues[sport_key], "scoreboard", f"dates={day:%Y%m%d}", get)
            if data is not None:
                read += 1
            events += parse_live(data)
        uniq = {e["event"]: e for e in events}
        for eid, (h, a, k) in assign_events(sport_key, list(uniq.values()), fixtures).items():
            e = uniq[eid]
            rows.append({"sport_key": sport_key, "kickoff": k, "home": h, "away": a, "espn_id": eid,
                         **{c: e[c] for c in ("home_goals", "away_goals", "home_corners", "away_corners",
                                              "home_cards", "away_cards", "status", "clock")}})
    if asked and not read:
        log.error("❌ Marcadores del día (ESPN): no respondió ninguna liga — el dashboard se queda con ⏳")
        return 0
    _save(engine, rows)
    final = sum(1 for r in rows if r["status"] == "final")
    print(f"⚽ Marcadores del día (ESPN): {len(rows)} partidos ({final} terminados, {len(rows) - final} en juego)")
    return len(rows)


def _save(engine, rows: list) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text(f"""
            INSERT INTO {TABLE} (sport_key, kickoff, home, away, home_goals, away_goals, home_corners,
                                 away_corners, home_cards, away_cards, status, clock, espn_id)
            VALUES (:sport_key, :kickoff, :home, :away, :home_goals, :away_goals, :home_corners,
                    :away_corners, :home_cards, :away_cards, :status, :clock, :espn_id)
            ON CONFLICT (sport_key, kickoff, home) DO UPDATE SET
                away = EXCLUDED.away, home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
                home_corners = EXCLUDED.home_corners, away_corners = EXCLUDED.away_corners,
                home_cards = EXCLUDED.home_cards, away_cards = EXCLUDED.away_cards,
                status = EXCLUDED.status, clock = EXCLUDED.clock, espn_id = EXCLUDED.espn_id,
                fetched_at = NOW()
        """), rows)


def final_rows(sport_key: str, events: list, candidates: list, have: set, start) -> list:
    """Función pura: las filas de live_scores de los partidos TERMINADOS de un
    scoreboard (parse_live) desde `start`, con los nombres de `matches`
    (candidates: [(fecha, local, visitante)], espn_lineups.system_names: misma
    fecha ±1 día y los dos nombres parecidos). Sin su partido en matches no se
    guarda (no se inventan nombres); have: los de ESPN ya guardados."""
    from src.features.espn_lineups import system_names
    out = []
    for e in events:
        if e["status"] != "final" or e["event"] in have or pd.Timestamp(e["kick"]).date() < start:
            continue
        names = system_names(e, candidates)
        if names is None:
            continue
        out.append({"sport_key": sport_key, "kickoff": e["kick"], "home": names[0], "away": names[1],
                    "espn_id": e["event"], **{c: e[c] for c in ("home_goals", "away_goals", "home_corners",
                                                                 "away_corners", "home_cards", "away_cards",
                                                                 "status", "clock")}})
    return out


def backfill_finals(days: int = 3, engine=None, get=None, since=None) -> int:
    """Evening: el final de ESPN (marcador, córners y tarjetas) de los partidos
    ya jugados de los últimos `days` días (o desde `since`) que todavía no
    están en live_scores. Una llamada por liga y mes. Devuelve cuántos guardó."""
    from src.features.espn_lineups import _get, espn_leagues
    if engine is None:
        from config.database import engine
    today = datetime.now(timezone.utc).date()
    start = pd.Timestamp(since).date() if since else today - timedelta(days=days)
    with engine.begin() as conn:
        conn.execute(text(DDL))
        have = {r[0] for r in conn.execute(text(f"""
            SELECT espn_id FROM {TABLE} WHERE status = 'final' AND espn_id IS NOT NULL""")).fetchall()}
    months = sorted({(start + timedelta(days=i)).strftime("%Y%m") for i in range((today - start).days + 1)})
    rows, asked, read = [], 0, 0
    for sport_key, slug in espn_leagues().items():
        with engine.connect() as conn:
            cands = [(r[0], r[1], r[2]) for r in conn.execute(text("""
                SELECT date, LOWER(home_team), LOWER(away_team) FROM matches
                WHERE league = :lg AND date >= CAST(:s AS date) - 2 AND home_goals IS NOT NULL
            """), {"lg": sport_key, "s": start}).fetchall()]
        if not cands:
            continue
        for ym in months:
            asked += 1
            data = _get(slug, "scoreboard", f"dates={ym}", get)
            if data is not None:
                read += 1
            rows += final_rows(sport_key, parse_live(data), cands, have, start)
    if asked and not read:
        log.error("❌ Córners y tarjetas (ESPN): no respondió ninguna liga")
        return 0
    _save(engine, rows)
    print(f"⚽ Córners y tarjetas (ESPN): {len(rows)} partidos terminados nuevos desde {start}")
    return len(rows)


def provisional_result(market: str, score: dict, odds=None) -> tuple[str | None, float | None]:
    """Función pura: (resultado, ganancia por unidad) del pick con el marcador
    final de ESPN, con la misma regla de liquidación (save_bets.resolve_market).
    Sin momio de PlayDoit la ganancia es None (como en program_picks)."""
    from src.models.save_bets import resolve_market
    if not market or score.get("home_goals") is None or score.get("away_goals") is None:
        return None, None
    row = {"home_goals": score["home_goals"], "away_goals": score["away_goals"],
           "home_corners": score.get("home_corners"), "away_corners": score.get("away_corners"),
           "home_yellow": score.get("home_cards"), "away_yellow": score.get("away_cards"),
           "home_red": 0 if score.get("home_cards") is not None else None,
           "away_red": 0 if score.get("away_cards") is not None else None}
    try:
        price = float(odds) if odds is not None and not pd.isna(odds) else None
    except (TypeError, ValueError):
        price = None
    try:
        outcome, profit = resolve_market(market, row, price or 2.0, 1.0)
    except (TypeError, ValueError, KeyError):
        return None, None
    if outcome == "unresolved":
        return None, None
    return outcome, (round(float(profit), 2) if price else None)
