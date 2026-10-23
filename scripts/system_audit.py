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
  7. Sesgo del modelo que queda después de la corrección (side_bias), global
     y por región, en el 1X2, más de 2.5 y ambos anotan, y que el weekly lo
     siga aprendiendo (23-oct-26: el local en las 5 grandes y los goles en
     América llevaban semanas sesgados sin que nada avisara).
  8. Alineaciones de ESPN con su partido y su equipo (23-oct-26: la de
     Gimnasia Mendoza se guardó también como la de Gimnasia LP y llegaron
     avisos de 8 y 9 titulares fuera que no existían).
  9. Dixon-Coles al día con la historia: un equipo de los próximos partidos
     con la historia que pide el ajuste (MIN_MATCHES en 3 años) que no está
     en él (23-oct-26: el ajuste del 19-oct era anterior a la carga de las
     segundas divisiones y 6 partidos del fin de semana se saltaban).
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
BIAS_MAX_AGE_D = 8                # el weekly aprende el sesgo cada domingo
RESIDUAL_MAX = 0.03               # sesgo que queda después de corregir: más de 3 pt…
RESIDUAL_T = 3.0                  # …y a 3 errores estándar de 0 (no es ruido)
RESIDUAL_MIN_N = 30
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


def bias_problems(residuals: list, state: dict | None, now: pd.Timestamp | None = None) -> list:
    """El sesgo que queda después de corregir (side_bias.residuals) y la edad
    de lo aprendido."""
    names = {"home_win": "local", "draw": "empate", "away_win": "visitante",
             "over25": "más de 2.5", "btts": "ambos anotan"}
    out = []
    at = (state or {}).get("updated_at")
    now = now or pd.Timestamp.now(tz="UTC")
    if not at:
        out.append("Sesgo: el weekly todavía no lo aprende")
    elif (now - pd.Timestamp(at)).total_seconds() / 86400 > BIAS_MAX_AGE_D:
        out.append(f"Sesgo: lo aprendido es de hace {(now - pd.Timestamp(at)).days} días (el weekly no corrió)")
    for r in residuals:
        if r["n"] < RESIDUAL_MIN_N or not r["se"]:
            continue
        if abs(r["mean"]) > RESIDUAL_MAX and abs(r["mean"]) / r["se"] > RESIDUAL_T:
            out.append(f"Sesgo {names.get(r['market'], r['market'])} ({r['scope']}): queda "
                       f"{r['mean'] * 100:+.1f} ±{r['se'] * 100:.1f} pt después de corregir (n={r['n']})")
    return out


def lineup_problems(found: dict) -> list:
    """espn_lineups.identity_problems → problemas de la auditoría."""
    out = []
    if found.get("shared_events"):
        out.append(f"Alineaciones: {len(found['shared_events'])} partido(s) de ESPN guardados en más de un partido "
                   f"({', '.join(found['shared_events'][:3])})")
    if found.get("foreign"):
        out.append(f"Alineaciones: {len(found['foreign'])} alineación(es) terminadas que no comparten jugadores "
                   f"con su equipo (ids {', '.join(map(str, found['foreign'][:5]))})")
    return out


def dc_problems(counts: dict, dc_teams: set, min_matches: int) -> list:
    """counts: {equipo próximo: partidos en la ventana del ajuste}. Los que
    tienen historia de sobra y no están en el ajuste Dixon-Coles."""
    missing = sorted(t for t, n in counts.items() if n >= min_matches and t not in dc_teams)
    if not missing:
        return []
    return [f"Dixon-Coles: {len(missing)} equipo(s) próximos con historia que no están en el ajuste "
            f"({', '.join(missing[:6])}): el ajuste es más viejo que la historia; corre el paso "
            f"'Fit DC-MLE parameters' del weekly"]


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

def _upcoming_history_counts(days: int) -> dict:
    """{equipo de los próximos 3 días: partidos con resultado en los últimos `days` días}."""
    from sqlalchemy import text
    from config.database import engine
    with engine.connect() as conn:
        rows = conn.execute(text("""
            WITH up AS (SELECT DISTINCT home_team_norm AS t FROM upcoming_matches
                        WHERE match_date BETWEEN NOW() AND NOW() + INTERVAL '3 days'
                        UNION SELECT DISTINCT away_team_norm FROM upcoming_matches
                        WHERE match_date BETWEEN NOW() AND NOW() + INTERVAL '3 days')
            SELECT up.t, COUNT(m.t) FROM up LEFT JOIN (
                SELECT LOWER(home_team) AS t FROM matches WHERE home_goals IS NOT NULL AND date >= CURRENT_DATE - :d
                UNION ALL SELECT LOWER(away_team) FROM matches WHERE home_goals IS NOT NULL AND date >= CURRENT_DATE - :d
            ) m ON m.t = up.t
            GROUP BY up.t"""), {"d": days}).fetchall()
    return {r[0]: int(r[1]) for r in rows}


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

    def bias():
        from src.models.side_bias import load_side_bias, read_pairs, residuals
        state = load_side_bias()
        return bias_problems(residuals(read_pairs(), state), state)
    check("Sesgo del modelo", bias)

    def lineups():
        from config.database import engine
        from src.features.espn_lineups import identity_problems, load_identity_rows
        return lineup_problems(identity_problems(load_identity_rows(engine)))
    check("Alineaciones de ESPN", lineups)

    def dc():
        from src.models.dc_mle_fitter import MAX_SEASONS, MIN_MATCHES
        from src.utils.model_state import load_state
        teams = {k.lower() for k in ((load_state("dc_params") or {}).get("teams") or {})}
        return dc_problems(_upcoming_history_counts(MAX_SEASONS * 365), teams, MIN_MATCHES)
    check("Dixon-Coles al día", dc)

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
