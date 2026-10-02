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
  NOR = Norway Eliteserien
  SWE = Sweden Allsvenskan
  CHN = China Super League
  DNK = Denmark Superliga
  FIN = Finland Veikkausliiga
  POL = Poland Ekstraklasa
  AUT = Austria Bundesliga
  ROU = Romania Liga 1

Corea del Sur NO existe en football-data: hasta el 1-oct-26 se pedía KOR.csv y
el servidor REDIRIGE en silencio a NOR.csv. Cada weekly guardaba toda la
historia noruega como K-League (3,541 partidos) y la de Noruega no entraba
("ya existían"). Por eso cada liga trae su país esperado y se verifica.

Uso:
  python scripts/load_extra_leagues.py
"""

import sys
import os
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.utils.db_batch import insert_ignore_conflicts, describe
from src.utils.football_data import fetch_csv
from src.utils.log import get_logger

log = get_logger(__name__)


# ── Mapeo de ligas extra ─────────────────────────────────────────────────────
EXTRA_LEAGUES = {
    "JPN": "soccer_japan_j_league",
    "NOR": "soccer_norway_eliteserien",
    "SWE": "soccer_sweden_allsvenskan",
    "CHN": "soccer_china_superleague",
    # Potenciales futuras adiciones:
    # "DNK": "soccer_denmark_superliga",
    # "FIN": "soccer_finland_veikkausliiga",
    # "POL": "soccer_poland_ekstraklasa",
    # "AUT": "soccer_austria_bundesliga",
}

# País que debe traer la columna Country de cada CSV
EXPECTED_COUNTRY = {
    "JPN": "Japan", "NOR": "Norway", "SWE": "Sweden", "CHN": "China",
    "DNK": "Denmark", "FIN": "Finland", "POL": "Poland", "AUT": "Austria",
}

BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"


def check_country(code: str, df: pd.DataFrame) -> str | None:
    """None si el CSV es del país de la liga pedida; si no, el motivo para no
    cargarlo. La redirección a otro archivo ya la descarta fetch_csv."""
    expected = EXPECTED_COUNTRY.get(code)
    if expected and "Country" in df.columns:
        got = {str(c).strip() for c in df["Country"].dropna().unique()}
        if got != {expected}:
            return f"{code}.csv trae Country={sorted(got)} (se esperaba {expected})"
    return None


def _download_csv(code: str) -> pd.DataFrame | None:
    """Descarga CSV de una extra league desde football-data.co.uk. Estos
    archivos traen todas las temporadas: que falten es un error."""
    df, why = fetch_csv(BASE_URL.format(code=code))
    problem = why if df is None else check_country(code, df)
    if problem:
        log.error(f"❌ Liga extra {code}: {problem} — no se carga")
        return None
    return df


def _normalize_date(date_str: str) -> str | None:
    """Convierte fecha de DD/MM/YYYY a YYYY-MM-DD."""
    try:
        return pd.to_datetime(date_str, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def season_start(value) -> int | None:
    """Año de inicio de la temporada, como lo guarda `matches.season` (entero).
    football-data escribe "2025" en las ligas de año calendario y, desde que la
    J-League pasó a jugar de agosto a mayo, "2026/2027": ese texto no entraba
    en la columna entera y el weekly del 28-sep-26 perdió 80 partidos."""
    import re
    m = re.search(r"\d{4}", str(value if value is not None else ""))
    return int(m.group(0)) if m else None


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

        # Season: año de inicio (entero); None si no viene. dtype object para
        # que un faltante quede None (NULL) y no NaN, que la columna entera rechaza
        df["season"] = (pd.Series([season_start(v) for v in df["Season"]], index=df.index, dtype=object)
                        if "Season" in df.columns else None)
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
