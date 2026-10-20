"""
scripts/goalscorer_picks.py
===========================
Mercado PAPEL de anytime goalscorer ("X anota en cualquier momento").

Generación (morning): para los partidos de los próximos 48h, toma los
mejores goleadores de cada equipo (por tasa histórica) y calcula P(anota)
con el contexto ofensivo del partido. Guarda en la tabla goalscorer_picks
con fair odds. Cuesta 0 créditos de API.

Resolución (evening/late): tras el partido, busca al jugador en
match_events.{home,away}_scorers (selecciones) o, en clubes, en los goles por
jugador de ESPN (tabla lineups, 20-oct-26) → win/loss; si el jugador no
estuvo en el partido, 'void' (como en las casas). Hasta el 20-oct solo se
buscaba en match_events, que no tiene clubes: ningún pick se liquidaba.

Odds reales: el plan actual de The Odds API no incluye player props, así
que el pick es papel. Si ves una cuota real MEJOR que la fair odds del
pick, ahí hay valor — regístrala con record_placed_odds() para medir
slippage (igual que bets_history).
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.models.scorer_model import (
    load_scorer_rates, anytime_scorer_prob, _parse_scorers,
)

TOP_PER_TEAM = 2          # mejores N jugadores por equipo
MAX_GOALS_TEAM = 2.6      # cap de goles esperados (igual que el pipeline)
MIN_PROB = 0.30           # solo picks con P(anota) >= 30%
HOURS_AHEAD = 48


def _ensure_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS goalscorer_picks (
            id SERIAL PRIMARY KEY,
            match TEXT NOT NULL,
            match_date TIMESTAMPTZ NOT NULL,
            league TEXT,
            player TEXT NOT NULL,
            team TEXT NOT NULL,
            probability NUMERIC,
            lambda NUMERIC,
            fair_odds NUMERIC,
            odds_placed NUMERIC,
            result TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (match, player, match_date)
        )
    """))


def generate_goalscorer_picks(verbose: bool = True) -> int:
    """
    Genera picks para partidos de las próximas HOURS_AHEAD horas.
    Usa los goles esperados (lambda) guardados en upcoming_matches si
    el pipeline los dejó; si no, aproxima por odds de over/under.
    """
    upcoming = pd.read_sql(text(f"""
        SELECT home_team, away_team, match_date, league, sport_key,
               home_odds, over25_odds
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() AND NOW() + INTERVAL '{HOURS_AHEAD} hours'
    """), engine)
    if upcoming.empty:
        if verbose:
            print("goalscorer: sin partidos próximos")
        return 0

    rates = load_scorer_rates()
    if not rates:
        if verbose:
            print("goalscorer: sin datos de goleadores en match_events")
        return 0

    # Ranking de jugadores por equipo (tasa raw)
    by_team: dict = {}
    for player, info in rates.items():
        if info["goals"] >= 4 and info["rate_raw"] >= 0.22:
            by_team.setdefault(info["team"], []).append((info["rate_raw"], player))
    for t in by_team:
        by_team[t].sort(reverse=True)

    picks = []
    for _, row in upcoming.iterrows():
        home = normalize_team(row["home_team"])
        away = normalize_team(row["away_team"])

        # Goles esperados del equipo: 60/40 localía (aprox del pipeline)
        # desde la odd over2.5 si existe, si no 1.3 baseline
        exp_total = 1.3
        if row["over25_odds"] and float(row["over25_odds"]) > 1:
            p_over = min(max(1.0 / float(row["over25_odds"]), 0.2), 0.9)
            exp_total = 1.6 + p_over * 1.6   # ~1.9 a 3.0 goles
        exp_total = min(exp_total, MAX_GOALS_TEAM + MAX_GOALS_TEAM)
        exp_home = min(exp_total * 0.58, MAX_GOALS_TEAM)
        exp_away = min(exp_total * 0.42, MAX_GOALS_TEAM)

        for team, exp_g in ((home, exp_home), (away, exp_away)):
            for rate, player in by_team.get(team, [])[:TOP_PER_TEAM]:
                pr = anytime_scorer_prob(player, exp_g, rates=rates)
                if pr and pr["prob"] >= MIN_PROB:
                    picks.append({
                        "match": f"{row['home_team']} vs {row['away_team']}",
                        "match_date": row["match_date"],
                        "league": row.get("sport_key") or "",
                        "player": pr["player"].title(),
                        "team": team,
                        "probability": pr["prob"],
                        "lambda": pr["lambda"],
                        "fair_odds": pr["fair_odds"],
                    })

    if not picks:
        if verbose:
            print("goalscorer: ningún pick califica (P<30% o sin élite)")
        return 0

    inserted = 0
    with engine.begin() as conn:
        _ensure_table(conn)
        for p in picks:
            r = conn.execute(text("""
                INSERT INTO goalscorer_picks
                    (match, match_date, league, player, team, probability, lambda, fair_odds)
                VALUES (:match, :match_date, :league, :player, :team,
                        :probability, :lambda, :fair_odds)
                ON CONFLICT (match, player, match_date) DO NOTHING
            """), p)
            inserted += r.rowcount

    if verbose:
        print(f"⚽ GOLEADORES: {inserted} picks nuevos ({len(upcoming)} partidos)")
        for p in picks[:10]:
            print(f"   {p['player']:<22} {p['team'][:18]:<18} "
                  f"P={p['probability']:.0%}  fair @{p['fair_odds']:.2f}  | {p['match']}")
    return inserted


def _name_tokens(name: str) -> set:
    import re
    import unicodedata
    clean = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z]+", clean) if len(t) >= 2}


def same_player(a: str, b: str) -> bool:
    """¿Es el mismo jugador con otra grafía? ("Kylian Mbappe-Lottin" en
    Understat, "Kylian Mbappé" en ESPN). Sin acentos; con 2+ palabras en ambos
    nombres hacen falta 2 en común, si no 1 de 4+ letras."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    common = ta & tb
    if len(ta) >= 2 and len(tb) >= 2:
        return len(common) >= 2
    return any(len(t) >= 4 for t in common)


def _surname(name: str) -> str:
    import re
    import unicodedata
    clean = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    parts = [t for t in re.split(r"[^a-z]+", clean) if t]
    return parts[-1] if parts else ""


def espn_result(goals: dict, names: dict, player: str) -> str:
    """win/loss si el jugador estuvo en el partido (ESPN), void si no.
    Primero el nombre (same_player); si no aparece, el apellido cuando un
    solo jugador del plantel lo lleva ("Fernando Niño" = "Fer Niño",
    "Mathis Cherki" = "Rayan Cherki")."""
    names = names or {}
    ids = [pid for pid, nm in names.items() if same_player(player, nm)]
    if not ids:
        sur = _surname(player.split("-")[0]) if "-" in player else _surname(player)
        same = [pid for pid, nm in names.items() if len(sur) >= 4 and _surname(nm) == sur]
        ids = same if len(same) == 1 else []
    if not ids:
        return "void"
    return "win" if any(int((goals or {}).get(pid, 0) or 0) > 0 for pid in ids) else "loss"


def _result_from_espn(conn, pk) -> str | None:
    """Resultado con los goles por jugador de ESPN (tabla lineups, partidos
    terminados). None si todavía no hay datos de ese partido."""
    import json
    if conn.execute(text("SELECT to_regclass('lineups')")).scalar() is None:
        return None
    # equipo Y rival: con solo el equipo, un partido de Champions el martes
    # tomaba el de liga del domingo (20-oct-26, Raphinha "no jugó")
    home, _, away = str(pk["match"]).partition(" vs ")
    team = str(pk.get("team") or "")
    rival = normalize_team(away) if normalize_team(home) == team else normalize_team(home)
    row = conn.execute(text("""
        SELECT goals, names FROM lineups
        WHERE completed AND team = :t AND opponent = :o
          AND kickoff BETWEEN CAST(:d AS timestamptz) - INTERVAL '1 day'
                          AND CAST(:d AS timestamptz) + INTERVAL '1 day'
        ORDER BY ABS(EXTRACT(EPOCH FROM (kickoff - CAST(:d AS timestamptz)))) LIMIT 1
    """), {"t": team, "o": rival, "d": str(pk["match_date"])}).first()
    if row is None:
        return None
    load = lambda v: v if isinstance(v, dict) else json.loads(v or "{}")
    return espn_result(load(row[0]), load(row[1]), str(pk["player"]))


def resolve_goalscorer_picks(verbose: bool = True) -> int:
    """Resuelve picks de partidos ya jugados usando match_events."""
    # La tabla nace con el primer pick: si el morning aún no generó ninguno,
    # la consulta fallaba cada noche (UndefinedTable) y ensuciaba el reporte
    # de errores tolerados del evening.
    with engine.begin() as conn:
        _ensure_table(conn)
    pending = pd.read_sql(text("""
        SELECT id, match, player, team, match_date
        FROM goalscorer_picks
        WHERE result = 'pending'
          AND match_date < NOW() - INTERVAL '2 hours'
    """), engine)
    if pending.empty:
        return 0

    resolved = 0
    with engine.begin() as conn:
        for _, pk in pending.iterrows():
            try:
                home_raw, away_raw = str(pk["match"]).split(" vs ")
            except ValueError:
                continue
            day = str(pk["match_date"])[:10]
            ev = conn.execute(text("""
                SELECT home_scorers, away_scorers
                FROM match_events
                WHERE date = :d
                  AND LOWER(home_team) = LOWER(:h)
                  AND LOWER(away_team) = LOWER(:a)
                LIMIT 1
            """), {"d": day, "h": home_raw, "a": away_raw}).fetchone()

            if ev is not None:
                names = {str(s.get("player", "")).strip().lower()
                         for s in _parse_scorers(ev[0]) + _parse_scorers(ev[1])}
                result = "win" if str(pk["player"]).strip().lower() in names else "loss"
            else:
                result = _result_from_espn(conn, pk)
                if result is None:
                    continue    # sin datos aún — late_results reintentará
            conn.execute(text("""
                UPDATE goalscorer_picks SET result = :r WHERE id = :id
            """), {"r": result, "id": int(pk["id"])})
            resolved += 1
        # sin datos a los 10 días: 'stale' (no se vuelven a buscar)
        stale = conn.execute(text("""
            UPDATE goalscorer_picks SET result = 'stale'
            WHERE result = 'pending' AND match_date < NOW() - INTERVAL '10 days'
        """)).rowcount

    if verbose and (resolved or stale):
        print(f"⚽ GOLEADORES resueltos: {resolved}" + (f", {stale} sin datos → stale" if stale else ""))
    return resolved


def record_placed_odds(match: str, player: str, odds: float) -> int:
    """Registrar cuota real encontrada manualmente (slippage tracking)."""
    with engine.begin() as conn:
        r = conn.execute(text("""
            UPDATE goalscorer_picks SET odds_placed = :o
            WHERE match = :m AND LOWER(player) = LOWER(:p) AND result = 'pending'
        """), {"o": odds, "m": match, "p": player})
        return r.rowcount


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve", action="store_true")
    args = parser.parse_args()
    if args.resolve:
        resolve_goalscorer_picks()
    else:
        generate_goalscorer_picks()
