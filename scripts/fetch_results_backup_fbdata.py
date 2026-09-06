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
from datetime import datetime, timedelta

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team


# Mapeo code football-data.co.uk → sport_key de The Odds API.
# Debe coincidir con `scripts/load_historical_data.py` (misma forma de nombrar).
LEAGUES = {
    "E0":  "soccer_epl",
    "E1":  "soccer_efl_champ",
    "D1":  "soccer_germany_bundesliga",
    "I1":  "soccer_italy_serie_a",
    "SP1": "soccer_spain_la_liga",
    "F1":  "soccer_france_ligue_one",
    "N1":  "soccer_netherlands_eredivisie",
    "P1":  "soccer_portugal_primeira_liga",
    "SC0": "soccer_spl",
    "T1":  "soccer_turkey_super_league",
    "B1":  "soccer_belgium_first_div",
    "G1":  "soccer_greece_super_league",
}


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
    """Descarga el CSV de football-data. Retorna None si falla."""
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
    try:
        df = pd.read_csv(url)
    except Exception as e:
        print(f"   ⚠️  {code} ({season}): descarga falló — {e}")
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
    })

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
                "home_yellow","away_yellow","home_red","away_red"]:
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
                # INSERT si no existe
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
                    inserted += 1
                    continue

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
                        away_red           = COALESCE(away_red, :away_red)
                    WHERE date = :date
                      AND home_team = :home_team
                      AND away_team = :away_team
                """), row.to_dict())
                if r.rowcount > 0:
                    updated += 1
            except Exception as e:
                print(f"      ⚠️ row error ({row.get('home_team')} vs {row.get('away_team')}): {e}")
    return inserted, updated


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
                print(f"— sin partidos en ventana (ventana=0)")
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
