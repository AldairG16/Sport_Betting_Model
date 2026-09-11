"""
scripts/goalscorer_picks.py
===========================
Mercado PAPEL de anytime goalscorer ("X anota en cualquier momento").

Generación (morning): para los partidos de los próximos 48h, toma los
mejores goleadores de cada equipo (por tasa histórica) y calcula P(anota)
con el contexto ofensivo del partido. Guarda en la tabla goalscorer_picks
con fair odds. Cuesta 0 créditos de API.

Resolución (evening/late): tras el partido, busca al jugador en
match_events.{home,away}_scorers → win/loss.

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


def resolve_goalscorer_picks(verbose: bool = True) -> int:
    """Resuelve picks de partidos ya jugados usando match_events."""
    pending = pd.read_sql(text("""
        SELECT id, match, player, match_date
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

            if ev is None:
                # Sin datos aún — dejar pending (late_results reintentará)
                continue

            names = {str(s.get("player", "")).strip().lower()
                     for s in _parse_scorers(ev[0]) + _parse_scorers(ev[1])}
            result = "win" if str(pk["player"]).strip().lower() in names else "loss"
            conn.execute(text("""
                UPDATE goalscorer_picks SET result = :r WHERE id = :id
            """), {"r": result, "id": int(pk["id"])})
            resolved += 1

    if verbose and resolved:
        print(f"⚽ GOLEADORES resueltos: {resolved}")
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
