"""
scripts/fetch_results.py
========================
Descarga resultados de partidos recientes desde The Odds API (scores endpoint)
e inserta los marcadores a 90 minutos en la tabla `matches`.

Después de ejecutar esto, update_bet_results() puede resolver las apuestas
pendientes automáticamente.

Uso:
  python scripts/fetch_results.py          → últimas 24h
  python scripts/fetch_results.py --days 2 → últimas 48h
"""

import sys
import os
import argparse
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from config.database import engine
from config.settings import ODDS_API_KEY, SPORT_KEYS
from src.utils.team_normalizer import normalize_team

SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport}/scores/"


def fetch_scores_for_league(sport_key: str, days_from: int = 2) -> list:
    """
    Descarga resultados completados para una liga.
    Returns lista de dicts con home_team, away_team, home_goals, away_goals, date.
    """
    url = SCORES_URL.format(sport=sport_key)
    try:
        r = requests.get(url, params={
            "apiKey":    ODDS_API_KEY,
            "daysFrom":  days_from,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"   ⚠️  Error fetching {sport_key}: {e}")
        return []

    results = []
    for match in data:
        if not match.get("completed"):
            continue

        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        scores    = match.get("scores") or []
        date_str  = match.get("commence_time", "")[:10]

        # Extraer goles de cada equipo
        home_goals = None
        away_goals = None
        for s in scores:
            try:
                if s["name"] == home_team:
                    home_goals = int(s["score"])
                elif s["name"] == away_team:
                    away_goals = int(s["score"])
            except (KeyError, ValueError):
                continue

        if home_goals is None or away_goals is None:
            continue

        results.append({
            "date":       date_str,
            "home_team":  normalize_team(home_team),
            "away_team":  normalize_team(away_team),
            "home_goals": home_goals,
            "away_goals": away_goals,
            "league":     sport_key,
        })

    return results


def insert_results(results: list) -> tuple[int, int]:
    """
    Inserta resultados en la tabla matches.
    Returns (insertados, ya_existian).
    """
    inserted = 0
    skipped  = 0

    with engine.begin() as conn:
        for r in results:
            try:
                res = conn.execute(text("""
                    INSERT INTO matches
                        (date, home_team, away_team, home_goals, away_goals, league)
                    VALUES
                        (:date, :home_team, :away_team, :home_goals, :away_goals, :league)
                    ON CONFLICT (date, home_team, away_team) DO NOTHING
                """), r)
                if res.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"   ❌ Error insertando {r['home_team']} vs {r['away_team']}: {e}")

    return inserted, skipped


def fetch_all_results(days_from: int = 2, verbose: bool = True) -> int:
    """
    Descarga e inserta resultados de todas las ligas configuradas.
    Returns total de nuevos resultados insertados.
    """
    if verbose:
        print(f"\n📡 DESCARGANDO RESULTADOS (últimas {days_from*24}h)...\n")

    total_inserted = 0
    credits_used   = 0

    for sport_key in SPORT_KEYS:
        results = fetch_scores_for_league(sport_key, days_from)
        if not results:
            continue

        inserted, skipped = insert_results(results)
        total_inserted += inserted
        credits_used   += 1  # 1 crédito por liga

        if verbose and (inserted > 0 or skipped > 0):
            completed = len(results)
            print(f"  {sport_key}")
            print(f"    Completados: {completed}  |  Nuevos: {inserted}  |  Ya existían: {skipped}")
            for r in results:
                if inserted > 0:
                    print(f"    {r['home_team']} {r['home_goals']}-{r['away_goals']} {r['away_team']}  ({r['date']})")

    if verbose:
        print(f"\n✅ Total nuevos resultados: {total_inserted}")
        print(f"   Créditos API usados: {credits_used} (1 por liga)")

    return total_inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2,
                        help="Cuántos días atrás buscar resultados (default: 2)")
    args = parser.parse_args()

    fetch_all_results(days_from=args.days)
