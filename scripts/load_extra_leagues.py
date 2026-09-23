"""
scripts/load_extra_leagues.py
=============================
Carga datos historicos de "extra leagues" desde football-data.co.uk.

Estas ligas usan un formato diferente a las principales:
  - Un solo CSV por liga (todas las temporadas juntas)
  - Columnas: Country, League, Season, Date, Time, Home, Away, HG, AG, Res, odds...
  - NO incluyen: shots, corners, cards (solo goles y odds)

URL: https://www.football-data.co.uk/new/{CODE}.csv

Ligas disponibles:
  JPN = Japan J-League
  KOR = South Korea K-League
  NOR = Norway Eliteserien
  SWE = Sweden Allsvenskan
  CHN = China Super League
  DNK = Denmark Superliga
  FIN = Finland Veikkausliiga
  POL = Poland Ekstraklasa
  AUT = Austria Bundesliga
  ROU = Romania Liga 1

Uso:
  python scripts/load_extra_leagues.py
"""

import sys
import os
import requests
import pandas as pd
from io import StringIO

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.utils.db_batch import insert_ignore_conflicts, describe
from src.utils.log import get_logger

log = get_logger(__name__)


# ── Mapeo de ligas extra ─────────────────────────────────────────────────────
EXTRA_LEAGUES = {
    "JPN": "soccer_japan_j_league",
    "KOR": "soccer_korea_kleague1",
    "NOR": "soccer_norway_eliteserien",
    "SWE": "soccer_sweden_allsvenskan",
    "CHN": "soccer_china_superleague",
    # Potenciales futuras adiciones:
    # "DNK": "soccer_denmark_superliga",
    # "FIN": "soccer_finland_veikkausliiga",
    # "POL": "soccer_poland_ekstraklasa",
    # "AUT": "soccer_austria_bundesliga",
}

BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"


def _download_csv(code: str) -> pd.DataFrame | None:
    """Descarga CSV de una extra league desde football-data.co.uk."""
    url = BASE_URL.format(code=code)
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        # Handle BOM encoding
        content = r.content.decode("utf-8-sig")
        df = pd.read_csv(StringIO(content))
        return df
    except Exception as e:
        print(f"  ERROR descargando {code}: {e}")
        return None


def _normalize_date(date_str: str) -> str | None:
    """Convierte fecha de DD/MM/YYYY a YYYY-MM-DD."""
    try:
        return pd.to_datetime(date_str, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def load_extra_leagues(leagues: dict = None):
    """
    Descarga y carga datos historicos de extra leagues.

    Args:
        leagues: dict {code: sport_key} o None para todas
    """
    if leagues is None:
        leagues = EXTRA_LEAGUES

    print("\n" + "=" * 55)
    print("  CARGANDO EXTRA LEAGUES (football-data.co.uk)")
    print("=" * 55)

    total_new = 0

    for code, sport_key in leagues.items():
        print(f"\n  Descargando {code} ({sport_key})...")

        df = _download_csv(code)
        if df is None or df.empty:
            print(f"  Sin datos para {code}")
            continue

        # Normalizar columnas
        df = df.rename(columns=lambda c: c.strip())

        # Verificar columnas requeridas
        required = ["Date", "Home", "Away", "HG", "AG"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"  Columnas faltantes: {missing}")
            continue

        # Limpiar datos
        df["date"] = df["Date"].apply(_normalize_date)
        df = df.dropna(subset=["date"])

        # Normalizar equipos
        df["home_team"] = df["Home"].apply(lambda x: normalize_team(str(x).strip().lower()))
        df["away_team"] = df["Away"].apply(lambda x: normalize_team(str(x).strip().lower()))

        # Goles
        df["home_goals"] = pd.to_numeric(df["HG"], errors="coerce")
        df["away_goals"] = pd.to_numeric(df["AG"], errors="coerce")
        df = df.dropna(subset=["home_goals", "away_goals"])
        df["home_goals"] = df["home_goals"].astype(int)
        df["away_goals"] = df["away_goals"].astype(int)

        # Season
        df["season"] = df["Season"].astype(str) if "Season" in df.columns else "unknown"
        df["league"] = sport_key

        # Por lotes con ON CONFLICT DO NOTHING (src/utils/db_batch). Antes:
        # una sentencia por fila (24 min por semana para ~18k filas), un
        # `except: pass` que dentro de la transacción revertía el lote
        # entero en silencio, y "nuevas insertadas" contaba las procesadas.
        cols = ["date", "league", "season", "home_team", "away_team",
                "home_goals", "away_goals"]
        with engine.begin() as conn:
            res = insert_ignore_conflicts(conn, "matches", cols,
                                          df[cols].to_dict("records"),
                                          ["date", "home_team", "away_team"])
        total_new += res["inserted"]
        print(f"  {code}: {len(df)} filas — {describe(res)}")
        if res["errors"]:
            log.error(f"❌ Liga extra {code}: {res['errors']} filas no se pudieron "
                      f"insertar — {res['first_error']}")

    print(f"\n  TOTAL: {total_new} registros nuevos")
    print("=" * 55)

    return total_new


if __name__ == "__main__":
    load_extra_leagues()
