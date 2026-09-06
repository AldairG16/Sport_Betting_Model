"""
scripts/load_mlb_data.py
========================
Descarga y carga datos historicos de MLB desde Retrosheet game logs.
Fuente: https://www.retrosheet.org/gamelogs/

Formato del archivo: CSV sin cabecera, 161 columnas.
Columnas usadas:
  0  → fecha (YYYYMMDD)
  3  → equipo visitante (codigo 3 letras, ej. "NYA")
  6  → equipo local (codigo 3 letras, ej. "BOS")
  9  → carreras visitante
  10 → carreras local

Los datos se insertan en la tabla `matches` con league = "baseball_mlb".
home_goals = carreras_local, away_goals = carreras_visitante
(compatible con el esquema existente de futbol).
"""

import sys
import io
import zipfile
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine

# ============================================================
# CONSTANTES
# ============================================================
RETROSHEET_URL = "https://www.retrosheet.org/gamelogs/gl{year}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sports-betting-model/1.0)"}
SEASONS = [2021, 2022, 2023, 2024, 2025]  # temporadas a cargar
LEAGUE = "baseball_mlb"

# Mapeo de codigos Retrosheet (3 letras) a nombres completos
TEAM_CODES = {
    "ANA": "Los Angeles Angels",
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHA": "Chicago White Sox",
    "CHN": "Chicago Cubs",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KCA": "Kansas City Royals",
    "LAN": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYA": "New York Yankees",
    "NYN": "New York Mets",
    "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SDN": "San Diego Padres",
    "SEA": "Seattle Mariners",
    "SFN": "San Francisco Giants",
    "SLN": "St. Louis Cardinals",
    "TBA": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WAS": "Washington Nationals",
    # Alias historicos
    "CIN": "Cincinnati Reds",
    "FLO": "Miami Marlins",
    "MON": "Washington Nationals",
}


def _download_season(year: int) -> pd.DataFrame | None:
    """Descarga y parsea el game log de una temporada MLB."""
    url = RETROSHEET_URL.format(year=year)
    try:
        r = requests.get(url, timeout=30, headers=HEADERS)
        if r.status_code != 200:
            print(f"  ⚠️  {year}: HTTP {r.status_code} — omitiendo")
            return None

        z = zipfile.ZipFile(io.BytesIO(r.content))
        filename = z.namelist()[0]
        with z.open(filename) as f:
            content = f.read().decode("latin-1")

        rows = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip('"') for p in line.split(",")]
            if len(parts) < 11:
                continue
            try:
                date_raw  = parts[0]   # YYYYMMDD
                away_code = parts[3]   # codigo visitante
                home_code = parts[6]   # codigo local
                away_runs = int(parts[9])
                home_runs = int(parts[10])

                date = datetime.strptime(date_raw, "%Y%m%d").strftime("%Y-%m-%d")
                home = TEAM_CODES.get(home_code, home_code)
                away = TEAM_CODES.get(away_code, away_code)

                rows.append({
                    "date":       date,
                    "home_team":  home,
                    "away_team":  away,
                    "home_goals": home_runs,
                    "away_goals": away_runs,
                    "league":     LEAGUE,
                    "neutral":    False,
                })
            except (ValueError, IndexError):
                continue

        df = pd.DataFrame(rows)
        print(f"  ✅ {year}: {len(df)} juegos parseados")
        return df

    except Exception as e:
        print(f"  ❌ {year}: {e}")
        return None


def _ensure_schema():
    """Agrega columna sport_key a matches si no existe."""
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE matches
            ADD COLUMN IF NOT EXISTS sport_key TEXT
        """))


def _insert_games(df: pd.DataFrame) -> tuple[int, int]:
    """Inserta juegos en la tabla matches. Retorna (insertados, omitidos)."""
    inserted = skipped = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            try:
                result = conn.execute(text("""
                    INSERT INTO matches
                        (date, home_team, away_team, home_goals, away_goals, league, neutral)
                    VALUES
                        (:date, :home_team, :away_team, :home_goals, :away_goals, :league, :neutral)
                    ON CONFLICT (date, home_team, away_team) DO NOTHING
                """), {
                    "date":       row["date"],
                    "home_team":  row["home_team"],
                    "away_team":  row["away_team"],
                    "home_goals": int(row["home_goals"]),
                    "away_goals": int(row["away_goals"]),
                    "league":     row["league"],
                    "neutral":    False,
                })
                if result.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
    return inserted, skipped


def load_mlb_data(verbose: bool = True) -> int:
    """
    Descarga y carga datos historicos de MLB en la DB.
    Retorna el total de juegos insertados.
    """
    if verbose:
        print("\n⚾ CARGANDO DATOS HISTORICOS MLB (Retrosheet)")
        print(f"   Temporadas: {SEASONS}\n")

    _ensure_schema()

    total_inserted = 0
    total_skipped  = 0

    for year in SEASONS:
        if verbose:
            print(f"📥 Temporada {year}...")
        df = _download_season(year)
        if df is None or df.empty:
            continue
        ins, skp = _insert_games(df)
        total_inserted += ins
        total_skipped  += skp
        if verbose:
            print(f"   DB: {ins} nuevos | {skp} ya existian")

    if verbose:
        print(f"\n✅ MLB cargado — {total_inserted} juegos insertados | {total_skipped} omitidos")

    return total_inserted


if __name__ == "__main__":
    load_mlb_data()
