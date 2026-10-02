"""
scripts/fetch_results_backup_fbdata.py
=======================================
Fuente de respaldo DIARIA de resultados desde football-data.co.uk.

The Odds API /scores sólo entrega goles finales (FT). football-data.co.uk
añade córners, tarjetas (amarillas/rojas), tiros y tiros al arco — los
mercados specialty de nuestro modelo dependen de estos campos.

El historial se carga vía `scripts/load_historical_data.py` (cargas masivas).
Este script es la versión DIARIA: sólo descarga el CSV de la temporada vigente
(`2526` por ejemplo) e inserta las filas recientes con `ON CONFLICT DO NOTHING`.

Ligas cubiertas: las 13 top-5 europeas + Championship + Scottish + Turkey +
Belgium + Greece + Eredivisie + Portugal. Ligas fuera de Europa (Liga MX,
Brasileirao, J-League, etc.) NO están en football-data.co.uk — para ésas
seguimos dependiendo 100% de The Odds API /scores (que no trae córners).

Uso:
  python scripts/fetch_results_backup_fbdata.py            # temporada actual, últimos 10 días
  python scripts/fetch_results_backup_fbdata.py --days 30  # extender ventana
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.football_data import SEASON_LEAGUES, SEASON_URL, check_division, fetch_csv
from src.utils.team_normalizer import normalize_team
from src.utils.log import get_logger

log = get_logger(__name__)


# Mapeo code football-data.co.uk → sport_key de The Odds API (el mismo que
# usa scripts/load_historical_data.py).
LEAGUES = SEASON_LEAGUES


def _current_season_code() -> str:
    """
    Devuelve el código de temporada estilo football-data (ej: '2526' para 2025-26).
    Las temporadas europeas empiezan en julio/agosto — si es ≥ agosto, usar año actual;
    si es antes de agosto, usar año anterior como inicio.
    """
    now = datetime.utcnow()
    if now.month >= 8:
        start = now.year
    else:
        start = now.year - 1
    end = start + 1
    return f"{str(start)[2:]}{str(end)[2:]}"  # ej: '2526'


def _download_csv(season: str, code: str) -> pd.DataFrame | None:
    """Descarga el CSV de football-data. Retorna None si falla o si todavía
    no existe. En agosto-26 la temporada nueva no estaba publicada y el
    servidor mandaba otro archivo (E0 → National League, SP1 → Escocia y
    Portugal): se guardaron como Premier y La Liga (src/utils/football_data.py)."""
    df, why = fetch_csv(SEASON_URL.format(season=season, code=code))
    if df is None:
        print(f"   ⚠️  {code} ({season}): {why}")
        return None
    problem = check_division(df, code)
    if problem:
        log.error(f"❌ fbdata {code} ({season}): {problem} — no se carga")
        return None
    if df.empty:
        return None
    return df


def _normalize_and_clean(df: pd.DataFrame, league: str, season_start: int) -> pd.DataFrame:
    """Aplica el mismo renombrado + normalización que load_historical_data."""
    df = df.rename(columns={
        "Date":"date",
        "HomeTeam":"home_team",
        "AwayTeam":"away_team",
        "FTHG":"home_goals",
        "FTAG":"away_goals",
        "HS":"home_shots",
        "AS":"away_shots",
        "HST":"home_shots_target",
        "AST":"away_shots_target",
        "HC":"home_corners",
        "AC":"away_corners",
        "HY":"home_yellow",
        "AY":"away_yellow",
        "HR":"home_red",
        "AR":"away_red",
        "HTHG":"home_goals_ht",
        "HTAG":"away_goals_ht",
    })
    # Solo las columnas que se usan (el CSV trae ~100 de cuotas): agregar
    # columnas a un frame tan ancho dejaba 72 PerformanceWarning por corrida
    # en el log, que tapaban lo importante.
    keep = ["date", "home_team", "away_team", "home_goals", "away_goals",
            "home_shots", "away_shots", "home_shots_target", "away_shots_target",
            "home_corners", "away_corners", "home_yellow", "away_yellow",
            "home_red", "away_red", "home_goals_ht", "away_goals_ht"]
    df = df[[c for c in keep if c in df.columns]].copy()

    # Parse date (football-data usa DD/MM/YYYY o DD/MM/YY)
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["date"])

    # Filtrar filas sin resultado final (fixtures futuros)
    df = df.dropna(subset=["home_goals", "away_goals"])

    # Normalizar nombres de equipos para que coincidan con la DB
    df["home_team"] = df["home_team"].apply(normalize_team)
    df["away_team"] = df["away_team"].apply(normalize_team)

    df["league"]  = league
    df["season"]  = season_start

    # Asegurar columnas opcionales
    for col in ["home_shots","away_shots","home_shots_target","away_shots_target",
                "home_corners","away_corners",
                "home_yellow","away_yellow","home_red","away_red",
                "home_goals_ht","away_goals_ht"]:
        if col not in df.columns:
            df[col] = None

    return df[[
        "date","league","season",
        "home_team","away_team",
        "home_goals","away_goals",
        "home_shots","away_shots",
        "home_shots_target","away_shots_target",
        "home_corners","away_corners",
        "home_yellow","away_yellow","home_red","away_red",
        "home_goals_ht","away_goals_ht",
    ]]


def _upsert_matches(df: pd.DataFrame) -> tuple[int, int]:
    """
    Inserta nuevas filas (no existen en matches) + UPDATE a filas existentes
    sólo si los campos de córners/tarjetas están NULL (evita sobrescribir data
    ya cargada). Retorna (inserted, updated).
    """
    inserted = 0
    updated = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            try:
                # Savepoint por fila: sin él, una fila con error aborta la
                # transacción y todas las siguientes fallan — y el lote
                # entero se revierte al final (córners/tarjetas que no llegan).
                with conn.begin_nested():
                    inserted_now, updated_now = _upsert_one(conn, row)
                inserted += inserted_now
                updated += updated_now
            except Exception as e:
                log.warning(f"      ⚠️ fila con error ({row.get('home_team')} vs "
                            f"{row.get('away_team')}): {type(e).__name__}: {str(e)[:160]}")
    return inserted, updated


def _upsert_one(conn, row) -> tuple[int, int]:
    """INSERT si no existe; si existe, UPDATE solo de los campos NULL.
    Devuelve (insertadas, actualizadas) de esta fila."""
    r = conn.execute(text("""
        INSERT INTO matches (
            date, league, season, home_team, away_team,
            home_goals, away_goals, home_shots, away_shots,
            home_shots_target, away_shots_target,
            home_corners, away_corners,
            home_yellow, away_yellow, home_red, away_red
        )
        VALUES (
            :date, :league, :season, :home_team, :away_team,
            :home_goals, :away_goals, :home_shots, :away_shots,
            :home_shots_target, :away_shots_target,
            :home_corners, :away_corners,
            :home_yellow, :away_yellow, :home_red, :away_red
        )
        ON CONFLICT (date, home_team, away_team) DO NOTHING
    """), row.to_dict())
    if r.rowcount > 0:
        return 1, 0

    # UPDATE parcial: sólo setea columnas que están NULL en DB
    # (rellena córners/tarjetas si The Odds API ya había insertado goles).
    r = conn.execute(text("""
        UPDATE matches
        SET home_corners       = COALESCE(home_corners, :home_corners),
            away_corners       = COALESCE(away_corners, :away_corners),
            home_shots         = COALESCE(home_shots, :home_shots),
            away_shots         = COALESCE(away_shots, :away_shots),
            home_shots_target  = COALESCE(home_shots_target, :home_shots_target),
            away_shots_target  = COALESCE(away_shots_target, :away_shots_target),
            home_yellow        = COALESCE(home_yellow, :home_yellow),
            away_yellow        = COALESCE(away_yellow, :away_yellow),
            home_red           = COALESCE(home_red, :home_red),
            away_red           = COALESCE(away_red, :away_red),
            home_goals_ht      = COALESCE(home_goals_ht, :home_goals_ht),
            away_goals_ht      = COALESCE(away_goals_ht, :away_goals_ht)
        WHERE date = :date
          AND home_team = :home_team
          AND away_team = :away_team
    """), row.to_dict())
    return 0, (1 if r.rowcount > 0 else 0)


def fetch_fbdata_backup(days: int = 10, verbose: bool = True) -> dict:
    """
    Descarga la temporada en curso de football-data.co.uk para todas las ligas
    mapeadas y rellena matches con los resultados de los últimos `days` días.

    Returns: dict con stats por liga + totales.
    """
    season = _current_season_code()
    season_start = int(season[:2]) + 2000
    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)

    if verbose:
        print(f"\n📡 FOOTBALL-DATA.CO.UK BACKUP — temporada {season}, últimos {days}d\n")

    totals = {"inserted": 0, "updated": 0, "leagues_ok": 0, "leagues_fail": 0}
    by_league = {}

    for code, league in LEAGUES.items():
        if verbose:
            print(f"   {code:4} {league:40} ", end="", flush=True)
        raw = _download_csv(season, code)
        if raw is None or raw.empty:
            totals["leagues_fail"] += 1
            if verbose:
                print("— sin data")
            continue

        try:
            df = _normalize_and_clean(raw, league, season_start)
        except Exception as e:
            totals["leagues_fail"] += 1
            if verbose:
                print(f"— error normalizando: {e}")
            continue

        # Filtrar a ventana reciente
        df_recent = df[df["date"] >= cutoff].copy()
        if df_recent.empty:
            totals["leagues_ok"] += 1
            if verbose:
                print("— sin partidos en ventana (ventana=0)")
            continue

        ins, upd = _upsert_matches(df_recent)
        by_league[league] = {"inserted": ins, "updated": upd, "rows": len(df_recent)}
        totals["inserted"]   += ins
        totals["updated"]    += upd
        totals["leagues_ok"] += 1
        if verbose:
            print(f"— {len(df_recent):3d} filas  (insert={ins}, update={upd})")

    totals["by_league"] = by_league

    if verbose:
        print(f"\n✅ fbdata backup: {totals['inserted']} insertados, "
              f"{totals['updated']} actualizados en {totals['leagues_ok']} ligas "
              f"({totals['leagues_fail']} ligas fallaron)")

    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10,
                        help="Ventana hacia atrás (default: 10 días)")
    parser.add_argument("--quiet", action="store_true", help="Menos output")
    args = parser.parse_args()
    fetch_fbdata_backup(days=args.days, verbose=not args.quiet)
