"""Tests del checker de convenciones y del checker de dueños solapados (T011).

Fijan cuatro contratos:

1. cada una de las SEIS convenciones documentadas en CLAUDE.md tiene su
   propia `ConventionRule` con un `rule_id` distinto, y cada una se ejercita
   aqui con un caso violador y (donde aplica) su contraparte conforme,
2. la regla `env-accessor` acusa las lecturas crudas de `scripts/watchdog.py`
   y NO acusa las lecturas que viven dentro de `env_int`/`env_float`/
   `env_str` en `config/settings.py` — esas SON la definicion de la regla,
3. un cron sub-horario en minuto pico se reporta; el `7,22,37,52` de
   `pre_kickoff.yml` no, y
4. `check_ownership` agrupa los tres modulos de calibracion y el par
   `value_bet`/`value_bets`, pero deja pasar `fetch_results` /
   `fetch_results_backup_fbdata`, que es redundancia deliberada.

Todo corre sobre arboles sinteticos en `tmp_path`: los checkers son
puramente estaticos y no deben depender de que el arbol de produccion este
presente ni de su estado en un commit concreto.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arbol(raiz, archivos):
    """Materializa un arbol sintetico {ruta relativa: contenido}."""
    for relativa, contenido in archivos.items():
        ruta = Path(raiz) / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
    return Path(raiz)


def _convenciones(raiz):
    """Corre el checker de convenciones y devuelve solo los hallazgos."""
    from tools.audit.checks.conventions import check_conventions

    hallazgos, _ = check_conventions(Path(raiz))
    return hallazgos


def _por_regla(hallazgos, rule_id):
    """Hallazgos cuya evidencia lleva la clave de una regla concreta."""
    return [h for h in hallazgos if any(ev.key == rule_id for ev in h.evidence)]


def _rutas(hallazgos):
    """Rutas de todas las evidencias, aplanadas."""
    return {ev.path for h in hallazgos for ev in h.evidence}


def _anclas(hallazgos):
    """Todas las anclas legibles, aplanadas."""
    return [ancla for h in hallazgos for ancla in h.anchors]


# ---------------------------------------------------------------------------
# Fuentes sinteticas reutilizadas
# ---------------------------------------------------------------------------

# Copia fiel del contrato de config/settings.py: las lecturas crudas viven
# DENTRO de los accesores. Son la definicion de la convencion, no su ruptura.
_SETTINGS = '''"""Configuracion central. Sus lecturas crudas definen la regla."""

import os


def env_str(nombre, default=""):
    valor = os.environ.get(nombre, "")
    return valor.strip() or default


def env_int(nombre, default):
    valor = os.environ.get(nombre, "").strip()
    return int(valor) if valor else default


def env_float(nombre, default):
    valor = os.environ.get(nombre, "").strip()
    return float(valor) if valor else default


DB_URL = os.environ.get("DB_URL", "")
'''

# Copia del patron real de scripts/watchdog.py:27-28 — dos lecturas crudas.
_WATCHDOG = '''"""Watchdog: silencio significa salud."""

import os

import requests


def _telegram(mensaje):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print(mensaje)
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": mensaje, "parse_mode": "HTML"},
        timeout=10,
    )
'''


# ---------------------------------------------------------------------------
# Contrato del registro de reglas
# ---------------------------------------------------------------------------

def test_las_seis_reglas_tienen_rule_id_distinto():
    """RULES declara exactamente las seis convenciones, sin IDs repetidos."""
    from tools.audit.checks.conventions import RULES

    ids = [regla.rule_id for regla in RULES]

    assert len(ids) == 6, f"se esperaban 6 reglas, hay {len(ids)}: {ids}"
    assert len(set(ids)) == 6, f"rule_id duplicado en {ids}"
    assert set(ids) == {
        "env-accessor",
        "edge-market",
        "ah-group",
        "distinct-on-upcoming",
        "normalize-team",
        "off-peak-cron",
    }


def test_cada_regla_es_invocable_de_forma_aislada(tmp_path):
    """Cada `detection` respeta el contrato (root) -> (hallazgos, inspeccionadas)."""
    from tools.audit.checks.conventions import RULES

    raiz = _arbol(tmp_path, {"src/vacio.py": "VALOR = 1\n"})

    for regla in RULES:
        hallazgos, inspeccionadas = regla.detection(raiz)
        assert isinstance(hallazgos, list), regla.rule_id
        assert isinstance(inspeccionadas, int), regla.rule_id
        assert regla.description, regla.rule_id


# ---------------------------------------------------------------------------
# Regla 1: env-accessor
# ---------------------------------------------------------------------------

def test_env_accessor_acusa_watchdog_y_perdona_settings(tmp_path):
    """Las lecturas crudas de watchdog se reportan; las de settings.py no."""
    raiz = _arbol(
        tmp_path,
        {
            "config/settings.py": _SETTINGS,
            "scripts/watchdog.py": _WATCHDOG,
        },
    )

    hallazgos = _por_regla(_convenciones(raiz), "env-accessor")
    rutas = _rutas(hallazgos)

    assert "scripts/watchdog.py" in rutas
    assert "config/settings.py" not in rutas, (
        "config/settings.py es la fuente unica de los accesores: sus "
        "lecturas crudas definen la convencion, no la violan"
    )
    # Las DOS lecturas de watchdog (token y chat), cada una con su linea.
    lineas = sorted(
        ev.line
        for h in hallazgos
        for ev in h.evidence
        if ev.path == "scripts/watchdog.py"
    )
    assert len(lineas) == 2, f"se esperaban 2 lecturas crudas, hay {lineas}"


def test_env_accessor_no_acusa_al_propio_auditor(tmp_path):
    """`tools/` y `tests/` quedan fuera de alcance: no son codigo de produccion."""
    raiz = _arbol(
        tmp_path,
        {
            "tools/algo.py": 'import os\nX = os.environ.get("X", "")\n',
            "tests/test_algo.py": 'import os\nY = os.environ.get("Y", "")\n',
        },
    )

    assert _por_regla(_convenciones(raiz), "env-accessor") == []


# ---------------------------------------------------------------------------
# Regla 2: edge-market
# ---------------------------------------------------------------------------

def test_edge_market_acusa_el_filtro_sobre_edge_crudo(tmp_path):
    """Filtrar por la clave `edge` se reporta; por `edge_market` no."""
    raiz = _arbol(
        tmp_path,
        {
            "src/pipeline/malo.py": (
                "def filtrar(bets):\n"
                '    return [b for b in bets if b["edge"] > 0.05]\n'
            ),
            "src/pipeline/bueno.py": (
                "def filtrar(bets):\n"
                '    return [b for b in bets if b["edge_market"] > 0.05]\n'
            ),
        },
    )

    hallazgos = _por_regla(_convenciones(raiz), "edge-market")
    rutas = _rutas(hallazgos)

    assert "src/pipeline/malo.py" in rutas
    assert "src/pipeline/bueno.py" not in rutas
    assert all(h.severity.value == "S1" for h in hallazgos)


def test_edge_market_no_acusa_una_asignacion(tmp_path):
    """Escribir la clave `edge` no es filtrar por ella: no se reporta."""
    raiz = _arbol(
        tmp_path,
        {
            "src/pipeline/calc.py": (
                "def anotar(bet, prob, odds):\n"
                '    bet["edge"] = prob * odds - 1\n'
                '    bet["edge_market"] = prob - 1 / odds\n'
                "    return bet\n"
            )
        },
    )

    assert _por_regla(_convenciones(raiz), "edge-market") == []


# ---------------------------------------------------------------------------
# Regla 3: ah-group
# ---------------------------------------------------------------------------

def test_ah_group_acusa_el_lookup_sin_resolver_el_grupo(tmp_path):
    """Indexar MIN_EDGE_BY_MARKET por variable sin `_ah_group()` se reporta."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/malo.py": (
                "MIN_EDGE_BY_MARKET = {}\n\n\n"
                "def min_edge(mkt):\n"
                "    return MIN_EDGE_BY_MARKET.get(mkt, 0.05)\n"
            ),
            "src/models/bueno.py": (
                "MIN_EDGE_BY_MARKET = {}\n\n\n"
                "def _ah_group(mkt):\n"
                "    return mkt\n\n\n"
                "def min_edge(mkt):\n"
                "    return MIN_EDGE_BY_MARKET.get(_ah_group(mkt), 0.05)\n"
            ),
        },
    )

    hallazgos = _por_regla(_convenciones(raiz), "ah-group")
    rutas = _rutas(hallazgos)

    assert "src/models/malo.py" in rutas
    assert "src/models/bueno.py" not in rutas


# ---------------------------------------------------------------------------
# Regla 4: distinct-on-upcoming
# ---------------------------------------------------------------------------

_SELECT_SIN_DISTINCT = '''"""Lector ingenuo de la tabla de proximos partidos."""

SQL = """
    SELECT home_team_norm, away_team_norm, odds
      FROM upcoming_matches
     WHERE match_day = :day
"""
'''

_SELECT_CON_DISTINCT = '''"""Lector correcto de la tabla de proximos partidos."""

SQL = """
    SELECT DISTINCT ON (home_team_norm, away_team_norm, sport_key)
           home_team_norm, away_team_norm, odds
      FROM upcoming_matches
     WHERE match_day = :day
     ORDER BY home_team_norm, away_team_norm, sport_key, updated_at DESC
"""
'''


def test_distinct_on_acusa_el_select_sin_deduplicar(tmp_path):
    """Un SELECT sobre upcoming_matches sin DISTINCT ON se reporta."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/lector_malo.py": _SELECT_SIN_DISTINCT,
            "scripts/lector_bueno.py": _SELECT_CON_DISTINCT,
        },
    )

    hallazgos = _por_regla(_convenciones(raiz), "distinct-on-upcoming")
    rutas = _rutas(hallazgos)

    assert "scripts/lector_malo.py" in rutas
    assert "scripts/lector_bueno.py" not in rutas


# ---------------------------------------------------------------------------
# Regla 5: normalize-team
# ---------------------------------------------------------------------------

_CONSULTA_SIN_NORMALIZAR = '''"""Consulta por equipo sin normalizar el nombre."""

SQL = """
    SELECT result
      FROM matches
     WHERE home_team = :home AND away_team = :away
"""
'''

_CONSULTA_NORMALIZADA = '''"""Consulta por equipo con el nombre ya normalizado."""

from src.utils.team_normalizer import normalize_team

SQL = """
    SELECT result
      FROM matches
     WHERE home_team = :home AND away_team = :away
"""


def buscar(conn, home, away):
    return conn.execute(
        SQL, {"home": normalize_team(home), "away": normalize_team(away)}
    )
'''


def test_normalize_team_acusa_la_consulta_sin_normalizar(tmp_path):
    """Filtrar `matches` por equipo sin `normalize_team()` se reporta."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/sin_normalizar.py": _CONSULTA_SIN_NORMALIZAR,
            "scripts/normalizada.py": _CONSULTA_NORMALIZADA,
        },
    )

    hallazgos = _por_regla(_convenciones(raiz), "normalize-team")
    rutas = _rutas(hallazgos)

    assert "scripts/sin_normalizar.py" in rutas
    assert "scripts/normalizada.py" not in rutas


# ---------------------------------------------------------------------------
# Regla 6: off-peak-cron
# ---------------------------------------------------------------------------

_WF_PICO = """name: peak
on:
  schedule:
    - cron: '*/15 * * * *'
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: python scripts/algo.py
"""

_WF_PRE_KICKOFF = """name: pre_kickoff
on:
  schedule:
    - cron: '7,22,37,52 * * * *'
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: python scripts/pre_kickoff_analyst.py
"""

_WF_DIARIO = """name: morning
on:
  schedule:
    - cron: '0 12 * * *'
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: python scripts/orchestrator.py --mode morning
"""


def test_off_peak_cron_acusa_solo_el_cron_sub_horario_en_pico(tmp_path):
    """`*/15` se reporta; `7,22,37,52` y un cron diario en `:00` no."""
    raiz = _arbol(
        tmp_path,
        {
            ".github/workflows/peak.yml": _WF_PICO,
            ".github/workflows/pre_kickoff.yml": _WF_PRE_KICKOFF,
            ".github/workflows/morning.yml": _WF_DIARIO,
        },
    )

    hallazgos = [
        h
        for h in _convenciones(raiz)
        if any((ev.key or "").startswith("cron:") for ev in h.evidence)
    ]
    rutas = _rutas(hallazgos)

    assert ".github/workflows/peak.yml" in rutas
    assert ".github/workflows/pre_kickoff.yml" not in rutas, (
        "7,22,37,52 es la referencia conforme de CLAUDE.md"
    )
    assert ".github/workflows/morning.yml" not in rutas, (
        "un cron diario no es sub-horario: GH Actions no lo descarta"
    )


# ---------------------------------------------------------------------------
# Checker de dueños solapados
# ---------------------------------------------------------------------------

def _arbol_ownership(tmp_path):
    """Arbol con los tres casos que importan: calibracion, value_bet(s) y el par exento."""
    return _arbol(
        tmp_path,
        {
            # Tres modulos que comparten el token de dominio `calibration`.
            "src/models/calibration_monitor.py": (
                "def apply_calibration(prob, market):\n    return prob\n"
            ),
            "src/features/calibration_report.py": (
                "def build_report(bets):\n    return {}\n"
            ),
            "scripts/audit_analyst_calibration.py": (
                "def main():\n    return 0\n"
            ),
            # Par de funciones que son variante plural una de la otra.
            "src/betting/value_bet.py": (
                "def value_bet(prob, odds):\n    return prob * odds - 1\n"
            ),
            "src/markets/value_bets.py": (
                "def value_bets(bets):\n    return list(bets)\n"
            ),
            # Redundancia DELIBERADA: primario + fallback documentado.
            "scripts/fetch_results.py": "def fetch_results(day):\n    return []\n",
            "scripts/fetch_results_backup_fbdata.py": (
                "def fetch_results_backup_fbdata(day):\n    return []\n"
            ),
        },
    )


def _ownership(raiz):
    """Corre el checker de dueños y devuelve solo los hallazgos."""
    from tools.audit.checks.ownership import check_ownership

    hallazgos, _ = check_ownership(Path(raiz))
    return hallazgos


def test_ownership_agrupa_los_tres_modulos_de_calibracion(tmp_path):
    """Los tres modulos con token `calibration` salen en un solo hallazgo."""
    from tools.audit.checks.ownership import find_module_overlaps

    raiz = _arbol_ownership(tmp_path)
    solapes = find_module_overlaps(raiz)

    assert "calibration" in solapes, f"tokens agrupados: {sorted(solapes)}"
    rutas = {modulo.ruta for modulo in solapes["calibration"]}
    assert rutas == {
        "scripts/audit_analyst_calibration.py",
        "src/features/calibration_report.py",
        "src/models/calibration_monitor.py",
    }

    # Y el checker completo lo emite como UN solo hallazgo con las 3 anclas:
    # reportar una sola obligaria al operador a buscar las otras a mano.
    hallazgos = _ownership(raiz)
    calibracion = [
        h
        for h in hallazgos
        if rutas <= {ev.path for ev in h.evidence}
    ]
    assert len(calibracion) == 1, f"anclas vistas: {_anclas(hallazgos)}"


def test_ownership_agrupa_value_bet_y_value_bets(tmp_path):
    """`value_bet` y `value_bets` son la misma responsabilidad con plural trivial."""
    from tools.audit.checks.ownership import find_function_overlaps

    raiz = _arbol_ownership(tmp_path)
    solapes = find_function_overlaps(raiz)
    nombres = {f.nombre for grupo in solapes.values() for f in grupo}

    assert {"value_bet", "value_bets"} <= nombres, (
        f"funciones agrupadas: {sorted(nombres)}"
    )

    anclas = _anclas(_ownership(raiz))
    assert any(ancla.startswith("src/betting/value_bet.py:") for ancla in anclas)
    assert any(ancla.startswith("src/markets/value_bets.py:") for ancla in anclas)


def test_ownership_perdona_el_par_fetch_results(tmp_path):
    """`fetch_results` / `fetch_results_backup_fbdata` es redundancia deliberada."""
    raiz = _arbol_ownership(tmp_path)
    rutas = _rutas(_ownership(raiz))

    assert "scripts/fetch_results_backup_fbdata.py" not in rutas, (
        "el fallback de football-data.co.uk es intencional: The Odds API "
        "primero, fbdata cuando la liga no esta cubierta"
    )
    assert "scripts/fetch_results.py" not in rutas


def test_ownership_reporta_ubicaciones_inspeccionadas(tmp_path):
    """El contador distingue 'no encontre nada' de 'no mire nada'."""
    from tools.audit.checks.ownership import check_ownership

    raiz = _arbol_ownership(tmp_path)
    hallazgos, inspeccionadas = check_ownership(raiz)

    assert inspeccionadas > 0
    assert all(h.category.value == "ownership-overlap" for h in hallazgos)
    assert all(h.evidence for h in hallazgos), "un hallazgo sin ancla no es accionable"
