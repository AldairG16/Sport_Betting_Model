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
import argparse
import requests
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from config.database import engine
from config.settings import ODDS_API_KEY, SPORT_KEYS
from src.utils.team_normalizer import normalize_team
from src.utils.log import get_logger

log = get_logger(__name__)

SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport}/scores/"


COST_WITH_DAYS_FROM = 2          # lo que cobra /scores con daysFrom (sin el header x-requests-last)
_last_cost = 0


def fetch_scores_for_league(sport_key: str, days_from: int = 2) -> tuple[list, bool]:
    """
    Descarga resultados completados para una liga.
    Returns (lista de dicts, ok) — ok=False si la API rechazó la petición.
    """
    global _last_cost
    url = SCORES_URL.format(sport=sport_key)
    _last_cost = 0
    try:
        r = requests.get(url, params={
            "apiKey":    ODDS_API_KEY,
            "daysFrom":  days_from,
        }, timeout=15)
        last = r.headers.get("x-requests-last", "")
        _last_cost = int(last) if last.isdigit() else COST_WITH_DAYS_FROM
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"   ⚠️  Error fetching {sport_key}: {e}")
        return [], False

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

    return results, True


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
                # Savepoint por fila: sin él, un INSERT fallido aborta la
                # transacción, los siguientes fallan también y al final se
                # revierte el lote entero (resultados que no llegan → bets
                # que no se liquidan).
                with conn.begin_nested():
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
                log.error(f"   ❌ Error insertando {r['home_team']} vs {r['away_team']}: {e}")

    return inserted, skipped


def leagues_needing_results(days_from: int = 2) -> set | None:
    """Las ligas con algún partido ya jugado (empezó hace 2 h o más, en los
    últimos days_from días) que se analizó o tiene apuesta pendiente y todavía
    no tiene marcador en `matches` (29-oct-26: se pedían todas las ligas en
    cada corrida, 2 créditos cada una, ~114 al día, y en las de las 6:00 y las
    12:30 casi todas ya estaban). None si no se pudo leer: se piden todas."""
    import pandas as pd
    from src.utils.output_checks import missing_results
    window = {"d": days_from}
    try:
        played = pd.read_sql(text("""
            SELECT match, match_date, league FROM model_vs_sharp
            WHERE match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '2 hours'
            UNION ALL
            SELECT match, match_date, league FROM bets_history
            WHERE result = 'pending'
              AND match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '2 hours'
        """), engine, params=window).to_dict("records")
        have = pd.read_sql(text("""
            SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, home_goals, away_goals
            FROM matches WHERE home_goals IS NOT NULL AND date >= CURRENT_DATE - make_interval(days => :d)
        """), engine, params={"d": days_from + 2}).to_dict("records")
    except Exception as e:
        log.warning(f"   ⚠️  No se pudo ver qué ligas faltan ({type(e).__name__}: {e}) — se piden todas")
        return None
    return {p["league"] for p in missing_results(played, have, normalize_team)}


def fetch_all_results(days_from: int = 2, verbose: bool = True) -> int:
    """
    Descarga e inserta resultados de las ligas a las que les falta alguno
    (leagues_needing_results). Returns total de nuevos resultados insertados.
    """
    if verbose:
        print(f"\n📡 DESCARGANDO RESULTADOS (últimas {days_from*24}h)...\n")

    total_inserted = 0
    credits_used   = 0
    leagues_ok     = 0
    leagues_err    = 0
    needed = leagues_needing_results(days_from)
    asked = [k for k in SPORT_KEYS if needed is None or k in needed]
    # 30-oct-26: una liga quitada de SPORT_KEYS (MLS, Championship) con
    # partidos ya analizados sin marcador también se pide: sus picks se liquidan
    asked += sorted((needed or set()) - set(SPORT_KEYS))
    if verbose and needed is not None:
        print(f"   Ligas con marcadores pendientes: {len(asked)} de {len(SPORT_KEYS)}")

    for sport_key in asked:
        results, ok = fetch_scores_for_league(sport_key, days_from)
        credits_used += _last_cost
        if not ok:
            leagues_err += 1
            continue
        leagues_ok += 1
        if not results:
            continue

        inserted, skipped = insert_results(results)
        total_inserted += inserted

        if verbose and (inserted > 0 or skipped > 0):
            completed = len(results)
            print(f"  {sport_key}")
            print(f"    Completados: {completed}  |  Nuevos: {inserted}  |  Ya existían: {skipped}")
            for r in results:
                if inserted > 0:
                    print(f"    {r['home_team']} {r['home_goals']}-{r['away_goals']} {r['away_team']}  ({r['date']})")

    if verbose:
        print(f"\n✅ Total nuevos resultados: {total_inserted}")
        print(f"   Créditos API usados: {credits_used}")
        print(f"   Ligas OK: {leagues_ok}  |  Ligas con error API: {leagues_err}")

    # Si TODAS las ligas fallaron, no es un problema de datos — es un bug de
    # configuración (param inválido, API key, rate limit). Levantar excepción
    # para que el orchestrator marque el step como fallido en vez de seguir
    # con `matches` vacío y dejar todas las bets `pending` silenciosamente.
    if leagues_err > 0 and leagues_ok == 0 and len(asked) > 0:
        raise RuntimeError(
            f"fetch_all_results: TODAS las {leagues_err} ligas fallaron contra "
            f"la API. Probable bug de configuración (daysFrom inválido, API key, "
            f"rate limit). Sin marcadores en `matches`, todas las bets se "
            f"quedarían pending."
        )

    return total_inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2,
                        help="Cuántos días atrás buscar resultados (default: 2)")
    args = parser.parse_args()

    fetch_all_results(days_from=args.days)
