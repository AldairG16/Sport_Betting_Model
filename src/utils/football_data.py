"""
src/utils/football_data.py
==========================
Descarga segura de los CSV de football-data.co.uk (1-oct-26).

El servidor corrige nombres de archivo: si el pedido no existe, redirige a
otro que difiere en una letra. Así entraron, sin ningún error, partidos de una
liga con la etiqueta de otra:
  - KOR.csv → NOR.csv: 3,541 partidos noruegos como K-League (desde 2012).
  - ECL.csv → EC.csv: la National League inglesa (5ª división) como
    Champions League, todas las temporadas desde 2015.
  - Agosto 2026, temporada nueva sin publicar: E0 → EC (National League como
    Premier) y SP1 → SC1 / P1 (Escocia y Portugal como La Liga).

Regla: el archivo final tiene que ser el pedido. Si el servidor manda otro,
el pedido "no existe" (igual que un 404), no son datos. Los CSV de temporada
(mmz4281) traen además la columna Div, que debe ser el código pedido; los de
/new/ traen Country (scripts/load_extra_leagues.py).
"""

from io import StringIO
from pathlib import PurePosixPath
from urllib.parse import urlparse

import pandas as pd

# Archivos de temporada (mmz4281) que se cargan → sport_key de The Odds API.
# Una sola fuente para load_historical_data y fetch_results_backup_fbdata.
SEASON_LEAGUES = {
    "E0":  "soccer_epl",
    "E1":  "soccer_efl_champ",               # Championship
    "D1":  "soccer_germany_bundesliga",
    "I1":  "soccer_italy_serie_a",
    "SP1": "soccer_spain_la_liga",
    "F1":  "soccer_france_ligue_one",
    "N1":  "soccer_netherlands_eredivisie",
    "P1":  "soccer_portugal_primeira_liga",
    "SC0": "soccer_spl",                      # Scottish Premiership
    "T1":  "soccer_turkey_super_league",
    "B1":  "soccer_belgium_first_div",
    "G1":  "soccer_greece_super_league",
}
# Los demás archivos de temporada, que no se cargan: a donde redirige el
# servidor. Clave = la que usa The Odds API para esa liga (si algún día se
# activa, su historia ya está bien etiquetada).
OTHER_SEASON_LEAGUES = {
    "EC":  "soccer_england_national_league",
    "E2":  "soccer_england_league1",
    "E3":  "soccer_england_league2",
    "SC1": "soccer_scotland_championship",
    "SC2": "soccer_scotland_league_one",
    "SC3": "soccer_scotland_league_two",
    "D2":  "soccer_germany_bundesliga2",
    "I2":  "soccer_italy_serie_b",
    "SP2": "soccer_spain_segunda_division",
    "F2":  "soccer_france_ligue_two",
}
SEASON_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"


def file_name(url: str) -> str:
    return PurePosixPath(urlparse(str(url or "")).path).name


def fetch_csv(url: str, timeout: int = 30, get=None) -> tuple[pd.DataFrame | None, str]:
    """
    (datos, "") o (None, motivo). Motivos: la descarga falló, el servidor
    mandó OTRO archivo (el pedido no existe), HTTP distinto de 200, archivo
    vacío o CSV ilegible. `get` es inyectable para los tests.
    """
    if get is None:
        import requests
        get = requests.get
    try:
        r = get(url, timeout=timeout)
    except Exception as e:
        return None, f"descarga falló: {type(e).__name__}: {str(e)[:160]}"
    asked, got = file_name(url), file_name(getattr(r, "url", "") or url)
    if got.lower() != asked.lower():
        return None, f"no existe: el servidor redirige {asked} a {got}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    raw = r.content or b""
    if len(raw) < 200:
        return None, "archivo vacío"
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    try:
        return pd.read_csv(StringIO(content)), ""
    except Exception as e:
        return None, f"CSV ilegible: {type(e).__name__}: {str(e)[:160]}"


def is_missing(reason: str) -> bool:
    """El archivo no existe (todavía): no es una falla. Una temporada nueva
    sin publicar da 404 o una redirección a otro archivo."""
    return reason.startswith(("no existe", "HTTP 404", "archivo vacío"))


def parse_dates(s: pd.Series) -> pd.Series:
    """Fechas de football-data: dd/mm/yyyy, o dd/mm/yy en temporadas viejas."""
    d = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")
    short = d.isna() & s.notna()
    if short.any():
        d[short] = pd.to_datetime(s[short], format="%d/%m/%y", errors="coerce")
    return d


def check_division(df: pd.DataFrame, code: str) -> str | None:
    """None si todas las filas son de la división pedida; si no, el motivo."""
    if "Div" not in df.columns:
        return f"{code}.csv no trae la columna Div"
    divs = {str(d).strip() for d in df["Div"].dropna().unique()}
    if divs != {code}:
        return f"{code}.csv trae Div={sorted(divs)}"
    return None
