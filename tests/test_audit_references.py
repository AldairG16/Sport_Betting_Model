"""Tests del checker de referencias del auditor estatico (T008).

Cubre las tres categorias que salen del grafo de imports:
`broken-reference`, `orphan-code` y `circular-dependency`.

Las garantias fuertes se prueban contra arboles sinteticos en tmp_path
reproduciendo la forma real del repositorio auditado (load_10_seasons.py
importando `src.data.collector`, `src/markets/*` sin nadie que los importe,
`src/dashboard/betting_dashboard.py` alcanzable desde el pipeline). Un arbol
controlado permite afirmar "exactamente estos hallazgos" sin depender de
cuanto codigo de produccion exista en el checkout. Contra el repositorio real
solo se prueban los invariantes que siempre tienen que valer: el contrato de
retorno, el registro y el determinismo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Constructores del arbol sintetico
# ---------------------------------------------------------------------------

def _escribir(raiz: Path, relativa: str, contenido: str) -> Path:
    """Crea un archivo (con sus directorios) dentro del arbol sintetico."""
    ruta = raiz / relativa
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _workflow(raiz: Path, nombre: str, comando: str) -> Path:
    """Workflow minimo con un unico step `run:`, como los reales."""
    return _escribir(
        raiz,
        f".github/workflows/{nombre}",
        "name: demo\n"
        "on:\n"
        "  schedule:\n"
        "    - cron: '0 12 * * *'\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"      - run: {comando}\n",
    )


def _arbol_base(raiz: Path) -> None:
    """Reproduce la forma del repositorio auditado.

    - un workflow que ejecuta `scripts/orchestrator.py`
    - el orquestador importa el pipeline, y el pipeline importa el dashboard
      (por eso el dashboard NO es huerfano, pese a parecerlo)
    - `src/markets/*` no lo importa nadie
    - `load_10_seasons.py` importa `src.data.collector`, que no existe
    """
    _workflow(raiz, "morning.yml", "python scripts/orchestrator.py --mode morning")

    _escribir(
        raiz,
        "scripts/orchestrator.py",
        "from src.pipeline.prediction_pipeline import run_prediction_pipeline\n",
    )
    _escribir(
        raiz,
        "src/pipeline/prediction_pipeline.py",
        "from src.dashboard.betting_dashboard import render\n"
        "import requests\n",
    )
    _escribir(raiz, "src/dashboard/betting_dashboard.py", "def render():\n    return 1\n")

    for modulo in (
        "asian_handicap",
        "btts",
        "correct_score",
        "market_1x2",
        "over_under",
    ):
        _escribir(raiz, f"src/markets/{modulo}.py", "VALOR = 1\n")

    _escribir(raiz, "src/data/queries.py", "SQL = 'select 1'\n")
    _escribir(
        raiz,
        "load_10_seasons.py",
        "from src.data.collector import download_season\n\n"
        "download_season(2015)\n",
    )


def _rutas(hallazgos) -> set[str]:
    """Conjunto de rutas ancladas por los hallazgos."""
    return {ev.path for hallazgo in hallazgos for ev in hallazgo.evidence}


# ---------------------------------------------------------------------------
# broken-reference
# ---------------------------------------------------------------------------

def test_broken_reference_detecta_import_inexistente(tmp_path):
    """`load_10_seasons.py` importa `src.data.collector`, que no existe.

    Es el hallazgo S1 de referencia: tiene que salir con ancla `archivo:linea`.
    """
    from tools.audit.checks.references import check_broken_references
    from tools.audit.model import Category, Severity

    _arbol_base(tmp_path)

    hallazgos, inspeccionados = check_broken_references(tmp_path)

    assert inspeccionados > 0
    rotos = [h for h in hallazgos if h.evidence[0].path == "load_10_seasons.py"]
    assert len(rotos) == 1

    roto = rotos[0]
    assert roto.category is Category.BROKEN_REFERENCE
    assert roto.severity is Severity.S1
    assert roto.evidence[0].line == 1
    assert roto.anchors == ["load_10_seasons.py:1"]
    assert "src.data.collector" in roto.impact


def test_broken_reference_ignora_dependencias_externas(tmp_path):
    """stdlib y paquetes de PyPI no son referencias rotas del repositorio."""
    from tools.audit.checks.references import check_broken_references

    _escribir(
        tmp_path,
        "src/utils/http.py",
        "import requests\n"
        "import ast\n"
        "from yaml import safe_load\n"
        "from sqlalchemy.orm import Session\n",
    )

    hallazgos, inspeccionados = check_broken_references(tmp_path)

    assert hallazgos == []
    assert inspeccionados == 4


def test_broken_reference_acepta_paquete_namespace(tmp_path):
    """`src/` no tiene `__init__.py`: importar un directorio es valido."""
    from tools.audit.checks.references import check_broken_references

    _escribir(tmp_path, "src/models/save_bets.py", "def guardar():\n    return 1\n")
    _escribir(
        tmp_path,
        "scripts/run.py",
        "from src.models.save_bets import guardar\n"
        "from src import models\n",
    )

    hallazgos, _ = check_broken_references(tmp_path)

    assert hallazgos == []


def test_broken_reference_cubre_imports_dentro_de_funciones(tmp_path):
    """Los tests importan dentro del cuerpo; esos imports revientan igual."""
    from tools.audit.checks.references import check_broken_references

    _escribir(tmp_path, "src/pipeline/bet_decision.py", "def decide_bets():\n    return []\n")
    _escribir(
        tmp_path,
        "tests/test_algo.py",
        "def test_x():\n"
        "    from src.pipeline.no_existe import nada\n"
        "    assert nada\n",
    )

    hallazgos, _ = check_broken_references(tmp_path)

    assert len(hallazgos) == 1
    assert hallazgos[0].evidence[0].line == 2


def test_broken_reference_reporta_archivo_no_parseable(tmp_path):
    """Un archivo con SyntaxError rompe a todo el que lo importe."""
    from tools.audit.checks.references import check_broken_references
    from tools.audit.model import Severity

    _escribir(tmp_path, "scripts/roto.py", "def falta_dos_puntos()\n    pass\n")

    hallazgos, inspeccionados = check_broken_references(tmp_path)

    assert len(hallazgos) == 1
    assert hallazgos[0].severity is Severity.S1
    assert hallazgos[0].evidence[0].key == "syntax"
    # Un archivo ilegible sigue siendo una ubicacion inspeccionada: la
    # categoria no puede quedar en "0 hallazgos, 0 ubicaciones".
    assert inspeccionados == 1


# ---------------------------------------------------------------------------
# orphan-code
# ---------------------------------------------------------------------------

def test_orphans_no_reporta_modulo_alcanzable_transitivamente(tmp_path):
    """`src/dashboard/betting_dashboard.py` NO es huerfano.

    Lo importa `prediction_pipeline.py`, que a su vez cuelga del orquestador
    que ejecuta el workflow. Reportarlo seria el falso positivo que lleva a
    borrar codigo vivo.
    """
    from tools.audit.checks.references import check_orphans

    _arbol_base(tmp_path)

    hallazgos, inspeccionados = check_orphans(tmp_path)

    assert inspeccionados > 0
    assert "src/dashboard/betting_dashboard.py" not in _rutas(hallazgos)
    assert "scripts/orchestrator.py" not in _rutas(hallazgos)
    assert "src/pipeline/prediction_pipeline.py" not in _rutas(hallazgos)


def test_orphans_reporta_todos_los_modulos_de_markets(tmp_path):
    """Ningun entry point alcanza `src/markets/*`: los cinco son huerfanos."""
    from tools.audit.checks.references import check_orphans
    from tools.audit.model import Category, Severity

    _arbol_base(tmp_path)

    hallazgos, _ = check_orphans(tmp_path)
    rutas = _rutas(hallazgos)

    esperados = {
        "src/markets/asian_handicap.py",
        "src/markets/btts.py",
        "src/markets/correct_score.py",
        "src/markets/market_1x2.py",
        "src/markets/over_under.py",
    }
    assert esperados <= rutas

    markets = [
        h for h in hallazgos if h.evidence[0].path.startswith("src/markets/")
    ]
    assert len(markets) == 5
    for hallazgo in markets:
        assert hallazgo.category is Category.ORPHAN_CODE
        assert hallazgo.severity is Severity.S3
        assert hallazgo.uncertain is False


def test_orphans_toma_las_raices_de_los_workflows_no_de_una_lista_fija(tmp_path):
    """Un workflow nuevo amplia el conjunto de raices automaticamente."""
    from tools.audit.checks.references import check_orphans

    _arbol_base(tmp_path)

    antes = _rutas(check_orphans(tmp_path)[0])
    assert "src/markets/btts.py" in antes

    _workflow(tmp_path, "markets.yml", "python -m src.markets.btts")

    despues = _rutas(check_orphans(tmp_path)[0])
    assert "src/markets/btts.py" not in despues


def test_orphans_cuenta_tests_como_entry_point(tmp_path):
    """`tests/` es raiz: un modulo que solo usa la suite no es huerfano."""
    from tools.audit.checks.references import check_orphans

    _arbol_base(tmp_path)
    _escribir(tmp_path, "src/utils/team_normalizer.py", "def normalize_team(x):\n    return x\n")
    _escribir(
        tmp_path,
        "tests/test_normalizer.py",
        "def test_norm():\n"
        "    from src.utils.team_normalizer import normalize_team\n"
        "    assert normalize_team('X') == 'X'\n",
    )

    hallazgos, _ = check_orphans(tmp_path)

    assert "src/utils/team_normalizer.py" not in _rutas(hallazgos)


def test_orphans_marca_uncertain_cuando_el_import_es_dinamico(tmp_path):
    """Un destino construido en runtime se marca incierto, no muerto.

    `import_module(f"src.markets.{nombre}")` no deja rastro estatico; afirmar
    que esos modulos estan muertos invita a borrarlos y tumbar el cargador.
    """
    from tools.audit.checks.references import check_orphans

    _arbol_base(tmp_path)
    _escribir(
        tmp_path,
        "src/pipeline/market_loader.py",
        "import importlib\n\n\n"
        "def cargar(nombre):\n"
        '    return importlib.import_module(f"src.markets.{nombre}")\n',
    )
    # El cargador cuelga del pipeline, que si es alcanzable desde el workflow.
    _escribir(
        tmp_path,
        "src/pipeline/prediction_pipeline.py",
        "from src.dashboard.betting_dashboard import render\n"
        "from src.pipeline.market_loader import cargar\n",
    )

    hallazgos, _ = check_orphans(tmp_path)
    markets = [
        h for h in hallazgos if h.evidence[0].path.startswith("src/markets/")
    ]

    assert len(markets) == 5
    for hallazgo in markets:
        assert hallazgo.uncertain is True
        assert "INCIERTO" in hallazgo.impact


def test_orphans_trata_el_import_dinamico_constante_como_arista_real(tmp_path):
    """`import_module("src.markets.btts")` SI resuelve: no es huerfano."""
    from tools.audit.checks.references import check_orphans

    _arbol_base(tmp_path)
    _escribir(
        tmp_path,
        "src/pipeline/prediction_pipeline.py",
        "import importlib\n"
        "from src.dashboard.betting_dashboard import render\n\n"
        'BTTS = importlib.import_module("src.markets.btts")\n',
    )

    hallazgos, _ = check_orphans(tmp_path)
    rutas = _rutas(hallazgos)

    assert "src/markets/btts.py" not in rutas
    assert "src/markets/over_under.py" in rutas


# ---------------------------------------------------------------------------
# circular-dependency
# ---------------------------------------------------------------------------

def test_cycles_s2_cuando_el_ciclo_toca_un_entry_point(tmp_path):
    """Un ciclo alcanzable desde un workflow afecta produccion: S2."""
    from tools.audit.checks.references import check_cycles
    from tools.audit.model import Category, Severity

    _workflow(tmp_path, "morning.yml", "python scripts/orchestrator.py --mode morning")
    _escribir(tmp_path, "scripts/orchestrator.py", "from src.models.a import A\n")
    _escribir(tmp_path, "src/models/a.py", "from src.models.b import B\n")
    _escribir(tmp_path, "src/models/b.py", "from src.models.a import A\n")

    hallazgos, inspeccionados = check_cycles(tmp_path)

    assert inspeccionados > 0
    assert len(hallazgos) == 1
    assert hallazgos[0].category is Category.CIRCULAR_DEPENDENCY
    assert hallazgos[0].severity is Severity.S2
    assert sorted(ev.path for ev in hallazgos[0].evidence) == [
        "src/models/a.py",
        "src/models/b.py",
    ]


def test_cycles_s3_cuando_el_ciclo_es_de_scripts_manuales(tmp_path):
    """El mismo ciclo, sin workflow que lo alcance, baja a S3."""
    from tools.audit.checks.references import check_cycles
    from tools.audit.model import Severity

    _workflow(tmp_path, "morning.yml", "python scripts/orchestrator.py --mode morning")
    _escribir(tmp_path, "scripts/orchestrator.py", "VALOR = 1\n")
    _escribir(tmp_path, "scripts/one_shot_a.py", "from scripts.one_shot_b import B\n")
    _escribir(tmp_path, "scripts/one_shot_b.py", "from scripts.one_shot_a import A\n")

    hallazgos, _ = check_cycles(tmp_path)

    assert len(hallazgos) == 1
    assert hallazgos[0].severity is Severity.S3


def test_cycles_no_reporta_nada_en_un_arbol_acilico(tmp_path):
    """Sin ciclos no hay hallazgos, pero si ubicaciones inspeccionadas."""
    from tools.audit.checks.references import check_cycles

    _arbol_base(tmp_path)

    hallazgos, inspeccionados = check_cycles(tmp_path)

    assert hallazgos == []
    assert inspeccionados > 0


# ---------------------------------------------------------------------------
# Registro y determinismo sobre el repositorio real
# ---------------------------------------------------------------------------

def test_los_tres_checkers_estan_registrados():
    """Un checker escrito pero no registrado deja su categoria en cero."""
    import tools.audit.checks.references  # noqa: F401  (registra al importar)
    from tools.audit.model import Category
    from tools.audit.registry import get_check

    esperado = {
        "broken-reference": Category.BROKEN_REFERENCE,
        "orphan-code": Category.ORPHAN_CODE,
        "circular-dependency": Category.CIRCULAR_DEPENDENCY,
    }
    for nombre, categoria in esperado.items():
        spec = get_check(nombre)
        assert spec is not None, f"checker '{nombre}' sin registrar"
        assert spec.category is categoria


def test_contrato_de_retorno_sobre_el_repositorio_real():
    """Los tres devuelven (list[Finding], int) contra el checkout real."""
    from tools.audit.checks.references import (
        check_broken_references,
        check_cycles,
        check_orphans,
    )
    from tools.audit.model import Finding

    for checker in (check_broken_references, check_orphans, check_cycles):
        hallazgos, inspeccionados = checker(REPO_ROOT)
        assert isinstance(hallazgos, list)
        assert isinstance(inspeccionados, int) and inspeccionados >= 0
        assert all(isinstance(h, Finding) for h in hallazgos)
        assert all(h.evidence for h in hallazgos), "hallazgo sin ancla"


def test_salida_deterministica_entre_corridas():
    """Dos corridas sobre el mismo arbol producen los mismos IDs, en orden."""
    from tools.audit.checks.references import (
        check_broken_references,
        check_cycles,
        check_orphans,
    )

    for checker in (check_broken_references, check_orphans, check_cycles):
        primera = [h.id for h in checker(REPO_ROOT)[0]]
        segunda = [h.id for h in checker(REPO_ROOT)[0]]
        assert primera == segunda


def test_los_checkers_del_auditor_no_se_reportan_como_huerfanos():
    """`checks/__init__.py` los carga con `import_module(f"{__name__}...")`.

    El prefijo dinamico se resuelve sustituyendo `__name__`, asi que los
    checkers no pueden salir como codigo muerto en el repositorio real.
    """
    from tools.audit.checks.references import check_orphans

    hallazgos, _ = check_orphans(REPO_ROOT)
    huerfanos = {
        h.evidence[0].path
        for h in hallazgos
        if not h.uncertain
    }
    assert "tools/audit/checks/references.py" not in huerfanos
