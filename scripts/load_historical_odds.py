"""
scripts/load_historical_odds.py
===============================
Momios históricos de football-data.co.uk → tabla historical_odds (19-oct-26).

Hasta aquí la base tenía 94k partidos con resultado y NI UN momio: el modelo
nunca se comparó con el mercado en años de datos, solo en las semanas de
sombra. Los mismos CSV que ya descarga el weekly traen el cierre de Pinnacle
(desde 2012-13), el promedio y la mejor cuota entre casas, más/menos 2.5 y
hándicap asiático; se tiraban.

Tabla aparte, no `matches`: es la base de investigación del buscador de
ventajas (src/models/edge_finder.py) y no toca nada de lo que ya funciona.
Trae sus propios goles y tiros a puerta para que cada temporada esté completa.

Prioridad de columnas (la primera que exista):
  Pinnacle:   cierre PSC* → PS* (apertura)
  promedio:   cierre AvgC* → Avg* → BbAv* (Betbrain, temporadas viejas)
  mejor:      cierre MaxC* → Max* → BbMx*
`pin_closing` / `avg_closing` dicen si el precio es de cierre.

Uso:
  python scripts/load_historical_odds.py              # temporada en curso + las que falten
  python scripts/load_historical_odds.py --backfill   # todas desde 2012-13
"""

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.utils.football_data import (OTHER_SEASON_LEAGUES, SEASON_LEAGUES, SEASON_URL,
                                     check_division, fetch_csv, is_missing, parse_dates)
from src.utils.log import get_logger

log = get_logger(__name__)

FIRST_SEASON = 2012          # Pinnacle aparece en football-data en 2012-13
TABLE = "historical_odds"

# Archivos de temporada: los que carga el sistema + las segundas divisiones que
# ofrece PlayDoit (League One/Two, 2. Bundesliga, Serie B, Segunda, Ligue 2).
SEASON_DIVISIONS = {**SEASON_LEAGUES,
                    **{c: OTHER_SEASON_LEAGUES[c] for c in ("E2", "E3", "D2", "I2", "SP2", "F2")}}
# Archivos /new/ (todas las temporadas en uno) → (sport_key, Country esperado)
NEW_URL = "https://www.football-data.co.uk/new/{code}.csv"
NEW_DIVISIONS = {
    "MEX": ("soccer_mexico_ligamx", "Mexico"),
    "USA": ("soccer_usa_mls", "USA"),
    "BRA": ("soccer_brazil_campeonato", "Brazil"),
    "ARG": ("soccer_argentina_primera_division", "Argentina"),
    "JPN": ("soccer_japan_j_league", "Japan"),
    "NOR": ("soccer_norway_eliteserien", "Norway"),
    "SWE": ("soccer_sweden_allsvenskan", "Sweden"),
    "CHN": ("soccer_china_superleague", "China"),
}

ODDS_COLS = ["pin_home", "pin_draw", "pin_away", "avg_home", "avg_draw", "avg_away",
             "max_home", "max_draw", "max_away", "pin_over25", "pin_under25",
             "avg_over25", "avg_under25", "max_over25", "max_under25",
             "ah_line", "pin_ah_home", "pin_ah_away", "avg_ah_home", "avg_ah_away"]
STAT_COLS = ["home_goals_ht", "away_goals_ht", "home_shots_target", "away_shots_target",
             "home_corners", "away_corners"]
COLUMNS = ["sport_key", "season", "match_date", "home", "away", "home_goals", "away_goals",
           *STAT_COLS, *ODDS_COLS, "pin_closing", "avg_closing", "source"]

DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id BIGSERIAL PRIMARY KEY,
        sport_key TEXT NOT NULL,
        season INT NOT NULL,
        match_date DATE NOT NULL,
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        home_goals INT, away_goals INT,
        {", ".join(f"{c} INT" for c in STAT_COLS)},
        {", ".join(f"{c} DOUBLE PRECISION" for c in ODDS_COLS)},
        pin_closing BOOLEAN, avg_closing BOOLEAN,
        source TEXT,
        loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (sport_key, match_date, home, away)
    )
"""


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def _first(df: pd.DataFrame, *names):
    """(serie numérica, nombre) de la primera columna que exista, o (None, None)."""
    for n in names:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce"), n
    return None, None



def _int(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if np.isfinite(f) else None


def parse_odds(df: pd.DataFrame, sport_key: str, season: int, source: str,
               names: tuple = ("HomeTeam", "AwayTeam", "FTHG", "FTAG")) -> list:
    """CSV de football-data → filas de historical_odds (función pura salvo
    normalize_team). Sin fecha, equipos o resultado la fila no entra."""
    from src.utils.team_normalizer import normalize_team
    home_c, away_c, hg_c, ag_c = names
    if df is None or df.empty or not {home_c, away_c, hg_c, ag_c, "Date"} <= set(df.columns):
        return []
    out = pd.DataFrame(index=df.index)
    out["match_date"] = parse_dates(df["Date"].astype(str)).dt.date
    out["home_goals"] = pd.to_numeric(df[hg_c], errors="coerce")
    out["away_goals"] = pd.to_numeric(df[ag_c], errors="coerce")
    # la primera opción de cada grupo es la de cierre
    closing = {}
    for key, options in {
        "pin": [("PSCH", "PSCD", "PSCA"), ("PSH", "PSD", "PSA")],
        "avg": [("AvgCH", "AvgCD", "AvgCA"), ("AvgH", "AvgD", "AvgA"), ("BbAvH", "BbAvD", "BbAvA")],
        "max": [("MaxCH", "MaxCD", "MaxCA"), ("MaxH", "MaxD", "MaxA"), ("BbMxH", "BbMxD", "BbMxA")],
    }.items():
        for k, trio in enumerate(options):
            if all(c in df.columns for c in trio):
                for side, col in zip(("home", "draw", "away"), trio):
                    out[f"{key}_{side}"] = pd.to_numeric(df[col], errors="coerce")
                closing[key] = k == 0
                break
    for key, options in {
        "pin": [("PC>2.5", "PC<2.5"), ("P>2.5", "P<2.5")],
        "avg": [("AvgC>2.5", "AvgC<2.5"), ("Avg>2.5", "Avg<2.5"), ("BbAv>2.5", "BbAv<2.5")],
        "max": [("MaxC>2.5", "MaxC<2.5"), ("Max>2.5", "Max<2.5"), ("BbMx>2.5", "BbMx<2.5")],
    }.items():
        for o, u in options:
            if o in df.columns and u in df.columns:
                out[f"{key}_over25"] = pd.to_numeric(df[o], errors="coerce")
                out[f"{key}_under25"] = pd.to_numeric(df[u], errors="coerce")
                break
    for col, names_ in (("ah_line", ("AHCh", "AHh", "BbAHh")), ("pin_ah_home", ("PCAHH", "PAHH")),
                        ("pin_ah_away", ("PCAHA", "PAHA")), ("avg_ah_home", ("AvgCAHH", "AvgAHH", "BbAvAHH")),
                        ("avg_ah_away", ("AvgCAHA", "AvgAHA", "BbAvAHA"))):
        s, _ = _first(df, *names_)
        if s is not None:
            out[col] = s
    for col, src in (("home_goals_ht", "HTHG"), ("away_goals_ht", "HTAG"), ("home_shots_target", "HST"),
                     ("away_shots_target", "AST"), ("home_corners", "HC"), ("away_corners", "AC")):
        out[col] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else np.nan
    for c in ODDS_COLS:
        if c not in out.columns:
            out[c] = np.nan
        elif c != "ah_line":
            out[c] = out[c].where((out[c] > 1.0) & (out[c] < 1000))     # cuotas absurdas fuera
    ok = (out["match_date"].notna() & out["home_goals"].notna() & out["away_goals"].notna()
          & df[home_c].notna() & df[away_c].notna())
    out, teams = out[ok], df.loc[ok, [home_c, away_c]]
    norm = {t: normalize_team(str(t).strip()) for t in pd.unique(teams.values.ravel())}
    out["home"] = teams[home_c].map(norm)
    out["away"] = teams[away_c].map(norm)
    out["sport_key"], out["season"], out["source"] = sport_key, int(season), source
    out["pin_closing"], out["avg_closing"] = closing.get("pin"), closing.get("avg")
    rows = out.astype(object).where(out.notna(), None).to_dict("records")
    for r in rows:
        for c in ("home_goals", "away_goals", *STAT_COLS):
            r[c] = _int(r[c]) if r[c] is not None else None
    return rows


def code_start(code: str) -> int:
    """Código de temporada de los archivos mmz4281 → año de inicio: '1213' →
    2012, '1920' → 2019, '2021' → 2020. Nunca se interpreta como año: el
    20-oct-26 '1920' y '2021' quedaron guardados como 1920 y 2021."""
    return 2000 + int(str(code)[:2])


def season_start(value) -> int | None:
    """Columna Season de los archivos /new/: '2012/2013' → 2012, '2025' → 2025."""
    import re
    m = re.search(r"(19|20)\d{2}", str(value if value is not None else ""))
    return int(m.group(0)) if m else None


def season_codes(first: int = FIRST_SEASON, today=None) -> list:
    from datetime import date
    today = today or date.today()
    last = today.year if today.month >= 7 else today.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(first, last + 1)]


def parse_new(df: pd.DataFrame, code: str) -> list:
    """Archivo /new/ (todas las temporadas) → filas, temporada por fila."""
    sport_key, country = NEW_DIVISIONS[code]
    if df is None or df.empty:
        return []
    got = {str(c).strip() for c in df.get("Country", pd.Series(dtype=str)).dropna().unique()}
    if got and got != {country}:
        raise ValueError(f"{code}.csv trae Country={sorted(got)} (se esperaba {country})")
    rows = []
    for season, part in df.groupby(df["Season"].map(season_start)):
        if season is None or season < FIRST_SEASON:
            continue
        rows += parse_odds(part, sport_key, season, f"new/{code}", names=("Home", "Away", "HG", "AG"))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Descarga y carga
# ─────────────────────────────────────────────────────────────────────────────

def _fetch(url: str, tries: int = 2):
    """fetch_csv con un reintento: football-data a veces tarda más de 30 s."""
    df, why = None, ""
    for attempt in range(tries):
        df, why = fetch_csv(url)
        if df is not None or is_missing(why):
            return df, why
        time.sleep(3 * (attempt + 1))
    return df, why


def _existing(engine) -> set:
    with engine.connect() as conn:
        if conn.execute(text(f"SELECT to_regclass('{TABLE}')")).scalar() is None:
            return set()
        return {(r[0], int(r[1])) for r in conn.execute(text(
            f"SELECT DISTINCT sport_key, season FROM {TABLE}"))}


def _save(engine, rows: list) -> dict:
    from src.utils.db_batch import insert_ignore_conflicts
    with engine.begin() as conn:
        conn.execute(text(DDL))
        return insert_ignore_conflicts(conn, TABLE, COLUMNS, rows,
                                       ["sport_key", "match_date", "home", "away"],
                                       fill_null=tuple(ODDS_COLS + STAT_COLS))


def load_historical_odds(backfill: bool = False, verbose: bool = True, pause: float = 0.5) -> dict:
    """
    Temporada en curso de cada división, más las temporadas que todavía no
    estén en la tabla (así una descarga fallida se reintenta la semana
    siguiente). Con backfill=True, todas desde 2012-13. Los archivos /new/
    traen todas las temporadas: se cargan siempre completos.
    Descarga primero y guarda después (la base corta conexiones quietas).
    """
    from config.database import engine
    seasons = season_codes()
    have = set() if backfill else _existing(engine)
    rows, failed, files = [], [], 0
    for code, sport_key in SEASON_DIVISIONS.items():
        for i, sc in enumerate(seasons):
            start = code_start(sc)
            if not backfill and i < len(seasons) - 1 and (sport_key, start) in have:
                continue
            df, why = _fetch(SEASON_URL.format(season=sc, code=code))
            time.sleep(pause)
            if df is None:
                if not is_missing(why):
                    failed.append(f"{code} {sc}: {why}")
                continue
            problem = check_division(df, code)
            if problem:
                failed.append(f"{code} {sc}: {problem}")
                continue
            files += 1
            rows += parse_odds(df, sport_key, start, f"{code}/{sc}")
    for code in NEW_DIVISIONS:
        df, why = _fetch(NEW_URL.format(code=code))
        time.sleep(pause)
        if df is None:
            failed.append(f"{code}: {why}")
            continue
        try:
            rows += parse_new(df, code)
            files += 1
        except ValueError as e:
            failed.append(str(e))
    engine.dispose()
    result = _save(engine, rows) if rows else {"inserted": 0, "filled": 0, "existing": 0,
                                               "errors": 0, "first_error": None}
    result.update(files=files, rows=len(rows), failed=failed)
    if verbose:
        from src.utils.db_batch import describe
        print(f"📈 Momios históricos: {files} archivos, {len(rows):,} partidos → {describe(result)}")
    if failed:
        # ERROR: sin estos momios el buscador de ventajas mide con huecos
        log.error(f"❌ Momios históricos: {len(failed)} descargas fallaron — " + "; ".join(failed[:4]))
    return result


if __name__ == "__main__":
    load_historical_odds(backfill="--backfill" in sys.argv)
