"""Tests de las primitivas de parseo del auditor estatico (T004).

Cubren las tres piezas sobre las que se apoyan todos los checkers:
grafo de imports (`tools/audit/graph.py`), lectura de workflows
(`tools/audit/workflows.py`) y escaneo de DDL (`tools/audit/sqlscan.py`).

Las garantias fuertes se prueban contra arboles sinteticos en tmp_path: un
arbol controlado permite afirmar "exactamente estos ciclos" o "estos tres
tipos de DDL" sin depender de cuanto codigo de produccion exista en el
checkout. Contra el repositorio real se prueban los invariantes que si tienen
que valer siempre: determinismo, orden estable y cobertura de archivos.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


def _escribir(raiz: Path, relativa: str, contenido: str) -> Path:
    """Crea un archivo (con sus directorios) dentro del arbol sintetico."""
    ruta = raiz / relativa
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


# ---------------------------------------------------------------------------
# build_module_index
# ---------------------------------------------------------------------------

def test_module_index_mapea_nombres_punteados_sin_init(tmp_path):
    """Resuelve nombres punteados aunque no exista ningun __init__.py.

    src/ del repositorio real no tiene marcadores de paquete (namespace
    implicito), asi que el indice se deriva de la ruta, no de __init__.py.
    """
    from tools.audit.graph import build_module_index

    _escribir(tmp_path, "src/pipeline/bet_decision.py", "X = 1\n")
    _escribir(tmp_path, "config/settings.py", "Y = 2\n")

    indice = build_module_index(tmp_path)

    assert "src.pipeline.bet_decision" in indice
    assert "config.settings" in indice
    assert indice["config.settings"].name == "settings.py"


def test_module_index_colapsa_init_al_nombre_del_paquete(tmp_path):
    """`pkg/__init__.py` se indexa como `pkg`, no como `pkg.__init__`."""
    from tools.audit.graph import build_module_index

    _escribir(tmp_path, "tools/audit/__init__.py", "")
    _escribir(tmp_path, "tools/audit/model.py", "")

    indice = build_module_index(tmp_path)

    assert "tools.audit" in indice
    assert "tools.audit.model" in indice
    assert "tools.audit.__init__" not in indice


def test_module_index_salta_directorios_excluidos(tmp_path):
    """No indexa .git, __pycache__, .venv ni archive/."""
    from tools.audit.graph import build_module_index

    _escribir(tmp_path, "src/vivo.py", "")
    _escribir(tmp_path, "archive/retirado.py", "")
    _escribir(tmp_path, "__pycache__/basura.py", "")
    _escribir(tmp_path, ".venv/lib/paquete.py", "")

    indice = build_module_index(tmp_path)

    assert "src.vivo" in indice
    assert not [nombre for nombre in indice if "archive" in nombre]
    assert not [nombre for nombre in indice if "__pycache__" in nombre]
    assert not [nombre for nombre in indice if "venv" in nombre]


# ---------------------------------------------------------------------------
# build_import_graph
# ---------------------------------------------------------------------------

def test_import_graph_resuelve_imports_dentro_de_funciones(tmp_path):
    """AC1: un import escrito en el cuerpo de una funcion produce arista.

    Es el patron dominante en tests/ y en varios scripts/ del repositorio;
    si el grafo solo mirara el nivel superior, esos modulos apareceran como
    huerfanos y el checker de alcanzabilidad mentiria.
    """
    from tools.audit.graph import build_import_graph

    _escribir(tmp_path, "src/objetivo.py", "VALOR = 1\n")
    _escribir(
        tmp_path,
        "tests/test_algo.py",
        "def test_uno():\n"
        "    from src.objetivo import VALOR\n"
        "    assert VALOR == 1\n",
    )

    grafo = build_import_graph(tmp_path)

    assert "src.objetivo" in grafo["tests.test_algo"]


def test_import_graph_resuelve_import_perezoso_dentro_de_metodo(tmp_path):
    """Tambien cuenta un `import x.y` diferido dentro de un metodo de clase."""
    from tools.audit.graph import build_import_graph

    _escribir(tmp_path, "src/utils/helper.py", "def f():\n    return 1\n")
    _escribir(
        tmp_path,
        "src/consumidor.py",
        "class C:\n"
        "    def run(self):\n"
        "        import src.utils.helper as h\n"
        "        return h.f()\n",
    )

    grafo = build_import_graph(tmp_path)

    assert "src.utils.helper" in grafo["src.consumidor"]


def test_import_graph_recorta_el_simbolo_importado_hasta_el_modulo(tmp_path):
    """`from pkg.mod import Simbolo` apunta a `pkg.mod`, no a `pkg.mod.Simbolo`."""
    from tools.audit.graph import build_import_graph

    _escribir(tmp_path, "tools/audit/model.py", "class Finding:\n    pass\n")
    _escribir(
        tmp_path,
        "tools/audit/report.py",
        "from tools.audit.model import Finding\n",
    )

    grafo = build_import_graph(tmp_path)

    assert grafo["tools.audit.report"] == {"tools.audit.model"}


def test_import_graph_ignora_dependencias_externas(tmp_path):
    """Solo hay aristas hacia modulos internos; stdlib y terceros no cuentan."""
    from tools.audit.graph import build_import_graph

    _escribir(tmp_path, "src/solo.py", "import os\nimport pandas as pd\n")

    grafo = build_import_graph(tmp_path)

    assert grafo["src.solo"] == set()


def test_import_graph_incluye_todo_modulo_como_clave(tmp_path):
    """Un modulo sin imports sigue apareciendo en el grafo con conjunto vacio."""
    from tools.audit.graph import build_import_graph

    _escribir(tmp_path, "src/aislado.py", "VALOR = 0\n")

    grafo = build_import_graph(tmp_path)

    assert grafo["src.aislado"] == set()


def test_import_graph_del_repositorio_real_no_explota():
    """Sobre el checkout real produce un grafo cuyas aristas son nodos validos."""
    from tools.audit.graph import build_import_graph, build_module_index

    grafo = build_import_graph(REPO_ROOT)
    indice = build_module_index(REPO_ROOT)

    assert set(grafo) == set(indice)
    for destinos in grafo.values():
        assert destinos <= set(indice)


# ---------------------------------------------------------------------------
# find_cycles
# ---------------------------------------------------------------------------

def test_find_cycles_detecta_ciclo_de_dos_modulos():
    """Un ciclo A -> B -> A se reporta una sola vez, en forma canonica."""
    from tools.audit.graph import find_cycles

    grafo = {"a": {"b"}, "b": {"a"}}

    assert find_cycles(grafo) == [["a", "b"]]


def test_find_cycles_es_determinista_y_ordenado():
    """AC3: dos llamadas sobre el mismo grafo devuelven la lista identica."""
    from tools.audit.graph import find_cycles

    grafo = {
        "z": {"y"},
        "y": {"z", "x"},
        "x": {"z"},
        "m": {"n"},
        "n": {"m"},
        "solo": set(),
    }

    primera = find_cycles(grafo)
    segunda = find_cycles(grafo)

    assert primera == segunda
    assert primera == sorted(primera)
    # Cada ciclo arranca en su nodo lexicograficamente menor.
    for ciclo in primera:
        assert ciclo[0] == min(ciclo)


def test_find_cycles_sin_ciclos_devuelve_lista_vacia():
    """Un DAG no produce ningun ciclo."""
    from tools.audit.graph import find_cycles

    assert find_cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []


def test_find_cycles_detecta_autoimport():
    """Un modulo que se importa a si mismo es un ciclo de longitud 1."""
    from tools.audit.graph import find_cycles

    assert find_cycles({"a": {"a"}}) == [["a"]]


# ---------------------------------------------------------------------------
# read_workflows
# ---------------------------------------------------------------------------

_WORKFLOW_TESTS = """name: Tests
on:
  push:
    branches: [ main ]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        env:
          DB_URL: postgresql://dummy:dummy@localhost/dummy
        run: python -m pytest tests/ -v --tb=short
"""

_WORKFLOW_MORNING = """name: Morning
on:
  schedule:
    - cron: '0 12 * * *'
jobs:
  morning:
    runs-on: ubuntu-latest
    steps:
      - name: Run orchestrator
        env:
          ODDS_API_KEY: dummy-token-placeholder
          TELEGRAM_BOT_TOKEN: dummy-token-placeholder
        run: python scripts/orchestrator.py --mode morning
"""


def _arbol_con_workflows(tmp_path: Path) -> Path:
    _escribir(tmp_path, ".github/workflows/tests.yml", _WORKFLOW_TESTS)
    _escribir(tmp_path, ".github/workflows/morning.yml", _WORKFLOW_MORNING)
    return tmp_path


def test_read_workflows_uno_por_archivo_con_run_commands(tmp_path):
    """AC2: un WorkflowInfo por archivo, cada uno con run_commands no vacio."""
    from tools.audit.workflows import read_workflows

    raiz = _arbol_con_workflows(tmp_path)
    archivos = sorted((raiz / ".github" / "workflows").glob("*.yml"))

    infos = read_workflows(raiz)

    assert len(infos) == len(archivos)
    assert [info.path for info in infos] == [
        ".github/workflows/morning.yml",
        ".github/workflows/tests.yml",
    ]
    for info in infos:
        assert info.run_commands, f"{info.path} sin comandos de entrada"


def test_read_workflows_extrae_cron_env_y_comando(tmp_path):
    """Cron, claves de env y comando salen del workflow tal cual estan escritos."""
    from tools.audit.workflows import read_workflows

    raiz = _arbol_con_workflows(tmp_path)

    por_ruta = {info.path: info for info in read_workflows(raiz)}
    morning = por_ruta[".github/workflows/morning.yml"]

    assert morning.name == "Morning"
    assert morning.crons == ["0 12 * * *"]
    assert morning.env_keys == {"ODDS_API_KEY", "TELEGRAM_BOT_TOKEN"}
    assert morning.run_commands == ["python scripts/orchestrator.py --mode morning"]


def test_read_workflows_no_pierde_el_bloque_on_por_el_booleano_yaml(tmp_path):
    """YAML 1.1 convierte la clave `on` en True; el cron debe sobrevivir."""
    import yaml

    from tools.audit.workflows import read_workflows

    raiz = _arbol_con_workflows(tmp_path)
    crudo = yaml.safe_load(
        (raiz / ".github" / "workflows" / "morning.yml").read_text(encoding="utf-8")
    )

    # Precondicion del bug que este test protege.
    assert "on" not in crudo and True in crudo

    morning = [i for i in read_workflows(raiz) if i.path.endswith("morning.yml")][0]
    assert morning.crons == ["0 12 * * *"]


def test_read_workflows_sin_carpeta_devuelve_lista_vacia(tmp_path):
    """Sin .github/workflows/ no revienta: devuelve lista vacia."""
    from tools.audit.workflows import read_workflows

    assert read_workflows(tmp_path) == []


def test_read_workflows_del_repositorio_cubre_todos_los_archivos():
    """AC2 sobre el checkout real: uno por archivo, con run_commands no vacio."""
    from tools.audit.workflows import read_workflows, workflows_dir

    carpeta = workflows_dir(REPO_ROOT)
    if carpeta.is_dir():
        archivos = sorted(
            ruta
            for patron in ("*.yml", "*.yaml")
            for ruta in carpeta.glob(patron)
        )
    else:
        archivos = []

    infos = read_workflows(REPO_ROOT)

    assert len(infos) == len(archivos)
    for info in infos:
        assert info.run_commands, f"{info.path} sin comandos de entrada"


# ---------------------------------------------------------------------------
# scan_ddl
# ---------------------------------------------------------------------------

_MODULO_CON_DDL = '''
"""Modulo sintetico con los tres sabores de DDL del proyecto."""


def crear(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS bets_history (id serial)")


def migrar(conn):
    conn.execute("ALTER TABLE bets_history ADD COLUMN clv double precision")


def volcar(df, engine):
    df.to_sql("upcoming_matches", engine, if_exists="append", index=False)
'''


def test_scan_ddl_detecta_create_alter_y_to_sql(tmp_path):
    """AC4: los tres tipos de sitio DDL se reportan con tabla y linea."""
    from tools.audit.sqlscan import scan_ddl

    _escribir(tmp_path, "src/models/save_bets.py", _MODULO_CON_DDL)

    sitios = scan_ddl(tmp_path)
    por_tipo = {sitio.kind for sitio in sitios}

    assert "create" in por_tipo
    assert "alter" in por_tipo
    assert "to_sql" in por_tipo
    assert {sitio.table for sitio in sitios} == {"bets_history", "upcoming_matches"}
    for sitio in sitios:
        assert sitio.path == "src/models/save_bets.py"
        assert sitio.line > 0


def test_scan_ddl_lee_archivos_sql_ademas_de_python(tmp_path):
    """El DDL suelto en .sql tambien entra al inventario."""
    from tools.audit.sqlscan import scan_ddl

    _escribir(
        tmp_path,
        "migrations/001.sql",
        "ALTER TABLE bankroll ADD COLUMN nota text;\n",
    )

    sitios = scan_ddl(tmp_path)

    assert [(s.path, s.table, s.kind) for s in sitios] == [
        ("migrations/001.sql", "bankroll", "alter")
    ]


def test_scan_ddl_es_determinista_y_ordenado(tmp_path):
    """Dos escaneos del mismo arbol dan exactamente la misma lista ordenada."""
    from tools.audit.sqlscan import scan_ddl

    _escribir(tmp_path, "src/a.py", _MODULO_CON_DDL)
    _escribir(tmp_path, "src/b.py", _MODULO_CON_DDL)

    primera = scan_ddl(tmp_path)
    segunda = scan_ddl(tmp_path)

    assert primera == segunda
    assert primera == sorted(
        primera, key=lambda s: (s.path, s.line, s.kind, s.table)
    )


def test_scan_ddl_ignora_texto_que_no_es_ddl(tmp_path):
    """Menciones sueltas de la palabra TABLE no generan falsos positivos."""
    from tools.audit.sqlscan import scan_ddl

    _escribir(
        tmp_path,
        "src/prosa.py",
        '"""Esta tabla no crea nada. TABLE suelto."""\n',
    )

    assert scan_ddl(tmp_path) == []


def test_scan_ddl_del_repositorio_real_es_estable():
    """Sobre el checkout real: sin excepciones, ordenado y con campos validos."""
    from tools.audit.sqlscan import scan_ddl

    sitios = scan_ddl(REPO_ROOT)

    assert sitios == sorted(
        sitios, key=lambda s: (s.path, s.line, s.kind, s.table)
    )
    for sitio in sitios:
        assert sitio.kind in {"create", "alter", "to_sql"}
        assert sitio.line > 0
        assert sitio.table


# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------

def test_pyyaml_esta_pineado_en_requirements():
    """AC5: workflows.py depende de PyYAML y todo workflow instala de aqui."""
    contenido = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

    lineas = [
        linea.strip()
        for linea in contenido.splitlines()
        if linea.strip().lower().startswith("pyyaml")
    ]

    assert lineas, "PyYAML ausente de requirements.txt"
    assert all("==" in linea for linea in lineas), "PyYAML sin version fijada"
