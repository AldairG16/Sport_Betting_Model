"""
scripts/system_audit.py
=======================
La auditoría del sistema, siempre la misma (23-oct-26).

El dueño: "quiero el sistema 100% funcional… que al correr auditorías no
estemos encontrando errores nuevos que auditorías pasadas no encontraron".
Cada auditoría a mano encontraba una clase de error nueva porque revisaba
otra cosa. Aquí está cada clase que ya pasó, como un chequeo que corre igual
cada vez; la próxima clase que aparezca se agrega aquí.

  1. Salidas (diario, output_checks.run_output_checks): liquidación contra
     un cálculo independiente, resultados que faltan, pendientes atorados,
     xG fuera de escala, aprendizaje del weekly, datos de entrada (fechas
     espejo, resultados con fecha futura, repetidos, tablas viejas).
  2. Equipos próximos con historia y sin la historia partida en dos nombres.
  3. Nombres de las otras fuentes (Understat, ESPN) que cuadran con matches.
  4. Lo que entró al modelo en la última corrida (cobertura de cada feature).
  5. Elo sin NaN.
  6. Cada liga de ESPN apunta a la liga correcta (chi.1 era Chile).
Corre en el weekly (paso "Auditoría del sistema", sin el 1, que ya corrió en
el evening) y a mano:
    python scripts/system_audit.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))

from src.utils.log import get_logger

log = get_logger(__name__)

COVERAGE_MAX_AGE_H = 36           # la última corrida del pipeline (morning o evening)
# palabra que tiene que traer el nombre de la liga en ESPN
ESPN_EXPECTED = {
    "soccer_epl": "Premier", "soccer_efl_champ": "Championship", "soccer_spain_la_liga": "LALIGA",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_italy_serie_a": "Serie A", "soccer_france_ligue_one": "Ligue 1",
    "soccer_netherlands_eredivisie": "Eredivisie", "soccer_belgium_first_div": "Belgian",
    "soccer_portugal_primeira_liga": "Portuguese", "soccer_turkey_super_league": "Turkish",
    "soccer_greece_super_league": "Greek", "soccer_spl": "Scottish", "soccer_sweden_allsvenskan": "Swedish",
    "soccer_norway_eliteserien": "Norwegian", "soccer_mexico_ligamx": "Mexican", "soccer_brazil_campeonato": "Brazilian",
    "soccer_argentina_primera_division": "Argentine", "soccer_usa_mls": "MLS", "soccer_china_superleague": "Chinese",
    "soccer_japan_j_league": "Japanese", "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League", "soccer_conmebol_copa_libertadores": "Libertadores",
}


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def coverage_problems(state: dict | None, now: pd.Timestamp | None = None) -> list:
    """Lo que entró al modelo en la última corrida (model_state pipeline_coverage)."""
    from src.utils.output_checks import feature_coverage
    if not state:
        return ["Cobertura: ninguna corrida del pipeline la guardó todavía"]
    now = now or pd.Timestamp.now(tz="UTC")
    age_h = (now - pd.Timestamp(state.get("at"))).total_seconds() / 3600 if state.get("at") else None
    out = feature_coverage(state)
    if age_h is None or age_h > COVERAGE_MAX_AGE_H:
        out.append(f"Cobertura: la última corrida guardada es de hace {age_h:.0f} h" if age_h is not None
                   else "Cobertura: sin fecha")
    return out


def espn_problems(names: dict, leagues: dict) -> list:
    """names: {sport_key: nombre de la liga que devuelve ESPN o None}."""
    out = []
    for key, slug in leagues.items():
        want = ESPN_EXPECTED.get(key)
        got = names.get(key)
        if got is None:
            out.append(f"ESPN: {key} → {slug} no responde")
        elif want and want.lower() not in str(got).lower():
            out.append(f"ESPN: {key} → {slug} es '{got}', no {want}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Corrida
# ─────────────────────────────────────────────────────────────────────────────

def _espn_names(leagues: dict, get=None) -> dict:
    from src.features.espn_lineups import _get
    out = {}
    for key, slug in leagues.items():
        data = _get(slug, "scoreboard", "limit=1", get)
        out[key] = ((data or {}).get("leagues") or [{}])[0].get("name") if data else None
    return out


def run_system_audit(include_daily: bool = True, espn_get=None) -> dict:
    """{chequeo: [problemas]} (vacío = en orden). Cada problema va como ERROR."""
    results = {}

    def check(name, fn):
        try:
            results[name] = list(fn() or [])
        except Exception as e:
            results[name] = [f"no se pudo revisar ({type(e).__name__}: {str(e)[:120]})"]

    if include_daily:
        from src.utils.output_checks import run_output_checks
        check("Salidas y datos de entrada", run_output_checks)
    from scripts.learn_team_aliases import check_names_without_history, check_source_names
    check("Equipos próximos con historia", lambda: [f"{t} ({l})" for l, t, *_ in check_names_without_history()])
    check("Nombres de Understat y ESPN", lambda: [f"{name}: {len(bad)}" for name, bad in check_source_names()])

    def coverage():
        from src.utils.model_state import load_state
        return coverage_problems(load_state("pipeline_coverage"))
    check("Lo que entra al modelo", coverage)

    def elo():
        from src.features.elo_rating import compute_elo
        bad = [t for t, v in compute_elo().items() if pd.isna(v)]
        return [f"Elo NaN en {len(bad)} equipos ({', '.join(sorted(bad)[:3])}…)"] if bad else []
    check("Elo", elo)

    def espn():
        from scripts.backfill_espn_results import ESPN_LEAGUES
        return espn_problems(_espn_names(ESPN_LEAGUES, espn_get), ESPN_LEAGUES)
    check("Ligas de ESPN", espn)

    bad = {k: v for k, v in results.items() if v}
    for name, problems in bad.items():
        for p in problems:
            log.error(f"❌ Auditoría del sistema — {name}: {p}")
    print("\n🩺 AUDITORÍA DEL SISTEMA")
    for name, problems in results.items():
        print(f"   {'✅' if not problems else '❌'} {name}" + (f": {len(problems)} problema(s)" if problems else ""))
    return results


if __name__ == "__main__":
    out = run_system_audit()
    sys.exit(1 if any(out.values()) else 0)
