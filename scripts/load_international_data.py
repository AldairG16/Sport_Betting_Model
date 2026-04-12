"""
Load International Results
==========================
Descarga y carga datos históricos de selecciones nacionales desde:
  github.com/martj42/international_results

Fuente: CSV con todos los partidos internacionales desde 1872.
Actualizado semanalmente por la comunidad.
Gratuito, sin API key necesaria.

Qué carga:
  - Clasificatorias Mundial (UEFA, CONMEBOL, CONCACAF, etc.)
  - UEFA Nations League
  - Eurocopas y Copas América (fases de grupos y eliminatorias)
  - NO carga amistosos (demasiado ruido para el modelo)

Dónde guarda:
  - Tabla `matches` existente, con sport_key mapeado por torneo
  - Solo últimas 4 temporadas (datos más recientes = más relevantes)
  - ON CONFLICT DO NOTHING (no duplica si ya existe)
"""

import sys
import os
import io
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine

# ─── Fuente de datos ──────────────────────────────────────────────────────
CSV_URL   = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
GOALS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/goalscorers.csv"

# ─── Cuántos años de historia cargar ─────────────────────────────────────
YEARS_BACK = 4   # últimos 4 años (suficiente para forma y H2H)

# ─── Torneos que SÍ se cargan (excluye amistosos) ─────────────────────────
TOURNAMENT_MAP = {
    # Clasificatorias Mundial
    "FIFA World Cup qualification":             "soccer_fifa_world_cup_qualifiers_europe",
    "FIFA World Cup":                           "soccer_fifa_world_cup",

    # UEFA
    "UEFA Nations League":                      "soccer_uefa_nations_league",
    "UEFA Euro":                                "soccer_uefa_euro",
    "UEFA Euro qualification":                  "soccer_uefa_euro_qualification",

    # CONMEBOL
    "Copa América":                             "soccer_conmebol_copa_america",
    "CONMEBOL World Cup qualification":         "soccer_conmebol_wc_qualifiers",

    # CONCACAF
    "CONCACAF Gold Cup":                        "soccer_concacaf_gold_cup",
    "CONCACAF Nations League":                  "soccer_concacaf_nations_league",

    # Copa Africa / Asia
    "Africa Cup of Nations":                    "soccer_afcon",
    "Africa Cup of Nations qualification":      "soccer_afcon_qualification",
    "AFC Asian Cup":                            "soccer_afc_asian_cup",
}

# Torneos que EXPLÍCITAMENTE se excluyen (amistosos, torneos menores)
EXCLUDED_TOURNAMENTS = {
    "Friendly",
    "King's Cup",
    "Kirin Cup",
    "China Cup",
    "AFF Championship",
    "CECAFA Cup",
    "COSAFA Cup",
    "WAFU Cup of Nations",
}

# ─── Normalización de nombres de selecciones ─────────────────────────────
# Mapea nombres del CSV → nombres de the-odds-api
TEAM_NAME_MAP = {
    "Czech Republic":            "Czechia",
    "Bosnia and Herzegovina":    "Bosnia & Herzegovina",
    "Bosnia-Herzegovina":        "Bosnia & Herzegovina",
    "Korea Republic":            "South Korea",
    "Korea DPR":                 "North Korea",
    "Ivory Coast":               "Cote d'Ivoire",
    "USA":                       "United States",
    "Trinidad & Tobago":         "Trinidad and Tobago",
    "Cape Verde":                "Cape Verde Islands",
    "St. Kitts and Nevis":       "Saint Kitts and Nevis",
    "St. Lucia":                 "Saint Lucia",
    "St. Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    "Antigua & Barbuda":         "Antigua and Barbuda",
    "São Tomé and Príncipe":     "Sao Tome and Principe",
    "Türkiye":                   "Turkey",
    "FYR Macedonia":             "North Macedonia",
    "Macedonia":                 "North Macedonia",
}


def normalize_team(name: str) -> str:
    return TEAM_NAME_MAP.get(name, name)


def map_tournament(tournament: str) -> str | None:
    """
    Retorna el sport_key correspondiente al torneo, o None si se debe excluir.
    Hace búsqueda parcial para capturar variantes como
    'FIFA World Cup qualification - UEFA' → 'soccer_fifa_world_cup_qualifiers_europe'
    """
    if not tournament:
        return None

    # Exclusión explícita
    if tournament in EXCLUDED_TOURNAMENTS:
        return None

    # Match exacto
    if tournament in TOURNAMENT_MAP:
        return TOURNAMENT_MAP[tournament]

    # Match parcial (orden importa: más específico primero)
    t_lower = tournament.lower()

    if "friendly" in t_lower:
        return None
    if "world cup qualif" in t_lower:
        if "conmebol" in t_lower or "south america" in t_lower:
            return "soccer_conmebol_wc_qualifiers"
        if "concacaf" in t_lower or "north america" in t_lower:
            return "soccer_concacaf_wc_qualifiers"
        return "soccer_fifa_world_cup_qualifiers_europe"
    if "nations league" in t_lower:
        if "uefa" in t_lower:
            return "soccer_uefa_nations_league"
        if "concacaf" in t_lower:
            return "soccer_concacaf_nations_league"
    if "euro qualif" in t_lower:
        return "soccer_uefa_euro_qualification"
    if "euro" in t_lower and "qualif" not in t_lower:
        return "soccer_uefa_euro"
    if "copa am" in t_lower:
        return "soccer_conmebol_copa_america"
    if "gold cup" in t_lower:
        return "soccer_concacaf_gold_cup"
    if "african cup" in t_lower or "afcon" in t_lower:
        return "soccer_afcon"
    if "asian cup" in t_lower:
        return "soccer_afc_asian_cup"
    if "world cup" in t_lower and "qualif" not in t_lower:
        return "soccer_fifa_world_cup"

    return None


def ensure_schema():
    """Asegura que las columnas necesarias existen en matches."""
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE matches
            ADD COLUMN IF NOT EXISTS neutral BOOLEAN DEFAULT FALSE
        """))
        # aet = after extra time. Si True, home_goals/away_goals son a 90 min
        # y el marcador final (con tiempo extra) no se usa para resolver apuestas.
        conn.execute(text("""
            ALTER TABLE matches
            ADD COLUMN IF NOT EXISTS aet BOOLEAN DEFAULT FALSE
        """))


def _load_90min_scores(cutoff: datetime) -> pd.DataFrame:
    """
    Descarga el CSV de goleadores y calcula el marcador a 90 minutos
    para cada partido. Descarta goles marcados en minuto > 90 (tiempo extra).

    Las casas de apuestas siempre liquidan a 90 minutos — los goles en
    tiempo extra no cuentan para over/under ni para resultado 1X2.

    Returns:
        DataFrame con columnas: date, home_team, away_team,
                                home_goals_90, away_goals_90, went_to_aet
    """
    try:
        resp = requests.get(GOALS_URL, timeout=30)
        resp.raise_for_status()
        goals = pd.read_csv(io.StringIO(resp.text))
    except Exception as e:
        print(f"   Advertencia: no se pudo descargar goleadores ({e})")
        return pd.DataFrame()

    goals["date"] = pd.to_datetime(goals["date"], errors="coerce")
    goals = goals[goals["date"] >= cutoff].copy()

    # Convertir minuto a número. El formato puede ser "90+2" (stoppage time)
    # o "105", "120" (tiempo extra). Stoppage time (90+N) cuenta como 90 min.
    def parse_minute(m):
        m = str(m).strip()
        if "+" in m:
            # Ej: "90+2" -> base 90, sigue contando en tiempo regular
            base = int(m.split("+")[0])
            return base  # 90 = tiempo regular (cuenta)
        try:
            return float(m)
        except Exception:
            return 0.0

    goals["min_num"] = goals["minute"].apply(parse_minute)

    # Goles a 90 minutos = minuto <= 90 (incluyendo stoppage time del 90+N)
    goals_90 = goals[goals["min_num"] <= 90].copy()

    # Goles en tiempo extra = minuto > 90 (91, 92, ..., 105, 120)
    goals_aet = goals[goals["min_num"] > 90].copy()

    # Partidos que tuvieron goles en tiempo extra (fueron a AET)
    aet_matches = set(
        zip(goals_aet["date"].dt.date.astype(str),
            goals_aet["home_team"],
            goals_aet["away_team"])
    )

    # Calcular marcador a 90 min por partido
    def count_goals(sub, team_col, home_col, away_col):
        rows = []
        for (date, ht, at), grp in sub.groupby(["date", "home_team", "away_team"]):
            hg = int(((grp[team_col] == ht) & (~grp["own_goal"])).sum()) + \
                 int(((grp[team_col] == at) &  grp["own_goal"]).sum())
            ag = int(((grp[team_col] == at) & (~grp["own_goal"])).sum()) + \
                 int(((grp[team_col] == ht) &  grp["own_goal"]).sum())
            went_aet = (str(date), ht, at) in aet_matches
            rows.append({
                "date": str(date), "home_team": ht, "away_team": at,
                "home_goals_90": hg, "away_goals_90": ag,
                "went_to_aet": went_aet
            })
        return pd.DataFrame(rows)

    result = count_goals(goals_90, "team", "home_team", "away_team")
    return result


def load_international_data(verbose: bool = True) -> int:
    """
    Descarga y carga datos históricos de selecciones.

    Returns:
        int — número de nuevos registros insertados
    """
    ensure_schema()

    cutoff = datetime.now() - timedelta(days=YEARS_BACK * 365)
    if verbose:
        print(f"\n🌍 CARGANDO DATOS INTERNACIONALES...")
        print(f"   Fuente: {CSV_URL}")
        print(f"   Desde: {cutoff.strftime('%Y-%m-%d')}  ({YEARS_BACK} anos)")

    # ── Descargar CSV ──────────────────────────────────────────────────────
    try:
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if verbose:
            print(f"   Descargados: {len(df):,} partidos totales")
    except Exception as e:
        print(f"❌ Error descargando CSV: {e}")
        return 0

    # ── Filtrar por fecha ──────────────────────────────────────────────────
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"] >= cutoff].copy()
    if verbose:
        print(f"   Ultimos {YEARS_BACK} anos: {len(df):,} partidos")

    # ── Filtrar torneos relevantes ─────────────────────────────────────────
    df["sport_key"] = df["tournament"].apply(map_tournament)
    df = df[df["sport_key"].notna()].copy()
    if verbose:
        print(f"   Torneos competitivos (sin amistosos): {len(df):,} partidos")
        print()
        print("   Por torneo:")
        for sk, count in df["sport_key"].value_counts().items():
            print(f"     {sk:<50} {count:>5}")

    if df.empty:
        print("⚠️  Sin partidos para cargar")
        return 0

    # ── Normalizar nombres ─────────────────────────────────────────────────
    df["home_team"] = df["home_team"].apply(normalize_team)
    df["away_team"] = df["away_team"].apply(normalize_team)

    # ── Preparar columnas ──────────────────────────────────────────────────
    df = df.rename(columns={
        "home_score": "home_goals",
        "away_score": "away_goals",
    })

    df["neutral"] = df["neutral"].map({"TRUE": True, "FALSE": False, True: True, False: False})
    # La tabla matches usa 'league' (no sport_key)
    df["league"] = df["sport_key"]

    # Columnas requeridas por la tabla matches
    cols = ["date", "home_team", "away_team", "home_goals", "away_goals",
            "league", "neutral"]
    df = df[cols].dropna(subset=["home_goals", "away_goals"])

    df["home_goals"] = df["home_goals"].astype(int)
    df["away_goals"] = df["away_goals"].astype(int)

    # ── Insertar en DB ─────────────────────────────────────────────────────
    inserted = 0
    skipped  = 0

    with engine.begin() as conn:
        for _, row in df.iterrows():
            try:
                result = conn.execute(text("""
                    INSERT INTO matches
                        (date, home_team, away_team, home_goals, away_goals,
                         league, neutral)
                    VALUES
                        (:date, :home_team, :away_team, :home_goals, :away_goals,
                         :league, :neutral)
                    ON CONFLICT (date, home_team, away_team) DO NOTHING
                """), {
                    "date":       row["date"].strftime("%Y-%m-%d"),
                    "home_team":  row["home_team"],
                    "away_team":  row["away_team"],
                    "home_goals": int(row["home_goals"]),
                    "away_goals": int(row["away_goals"]),
                    "league":     row["league"],
                    "neutral":    bool(row["neutral"]) if pd.notna(row["neutral"]) else False,
                })
                if result.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1

    if verbose:
        print(f"\n   Insertados: {inserted:,} nuevos registros")
        print(f"   Ya existian: {skipped:,}")
        print(f"\n✅ Datos internacionales cargados correctamente")

    return inserted


if __name__ == "__main__":
    total = load_international_data(verbose=True)
    print(f"\nTotal insertados: {total}")
