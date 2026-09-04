"""Tests de la corrida completa del auditor: `python -m tools.audit` (T014).

Cubren las tres garantias que hacen util al comando:

1. Corre entero sin ningun secreto configurado y escribe sus artefactos.
2. Reproduce los defectos de linea base sobre un arbol sintetico controlado.
3. No toca nada: ni base de datos, ni Odds API, ni Anthropic, ni el lock del
   orchestrator.

La linea base se verifica contra un arbol SINTETICO montado en `tmp_path` y
no contra el repo vivo. Es deliberado: varios de esos defectos ya fueron
remediados por tareas anteriores de este mismo blueprint (las constantes de
configuracion ya viven en `config/settings.py`), asi que exigirselos al arbol
real seria exigir una regresion. El arbol sintetico prueba lo que importa —
que el auditor SABE detectarlos — sin quedar atado a en que punto de la
remediacion esta el repo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ast
import json


TIMESTAMP_FIJO = "2026-01-05T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escribir(ruta: Path, contenido: str) -> Path:
    """Crea el archivo y sus directorios padre."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _arbol_con_defectos(base: Path) -> Path:
    """Arbol sintetico que planta los defectos de linea base a proposito.

    Contiene, uno por uno:
    - una referencia rota (`import config.database`, modulo inexistente);
    - un modulo huerfano (nadie lo alcanza desde workflows ni tests);
    - una constante de configuracion declarada FUERA de `config/settings.py`
      (`API_CREDITS_STOP_THRESHOLD`);
    - deriva de documentacion entre `README.md` y `.env.example`.
    """
    raiz = base / "repo"

    _escribir(
        raiz / "config" / "settings.py",
        "import os\n"
        "\n"
        "def env_int(nombre, default):\n"
        "    crudo = os.environ.get(nombre, '')\n"
        "    return int(crudo) if crudo.strip() else default\n"
        "\n"
        "INITIAL_BANKROLL = env_int('INITIAL_BANKROLL', 100)\n",
    )

    # Referencia rota: `config.database` no existe en este arbol.
    # Constante descentralizada: se declara aqui y no en settings.py.
    _escribir(
        raiz / "scripts" / "update_upcoming_matches.py",
        "import os\n"
        "from config.database import get_engine\n"
        "\n"
        "API_CREDITS_STOP_THRESHOLD = int(os.environ.get(\n"
        "    'API_CREDITS_STOP_THRESHOLD', '50'))\n"
        "\n"
        "def update_all():\n"
        "    return get_engine()\n",
    )

    # Huerfano: ningun workflow ni test lo alcanza.
    _escribir(
        raiz / "src" / "betting" / "legacy_sizer.py",
        "def sizer_viejo(bankroll):\n"
        "    return bankroll * 0.01\n",
    )

    _escribir(
        raiz / ".github" / "workflows" / "morning.yml",
        "name: morning\n"
        "on:\n"
        "  schedule:\n"
        "    - cron: '0 12 * * *'\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: python scripts/update_upcoming_matches.py\n"
        "        env:\n"
        "          DB_URL: ${{ secrets.DB_URL }}\n",
    )

    # Deriva documental: el README promete un default y .env.example otro.
    _escribir(
        raiz / "README.md",
        "# Repo de prueba\n"
        "\n"
        "```env\n"
        "INITIAL_BANKROLL=100\n"
        "API_CREDITS_STOP_THRESHOLD=50\n"
        "```\n",
    )
    _escribir(
        raiz / ".env.example",
        "INITIAL_BANKROLL=250\n"
        "API_CREDITS_STOP_THRESHOLD=900\n",
    )

    _escribir(raiz / "requirements.txt", "PyYAML==6.0.2\n")
    return raiz


def _correr(raiz: Path, destino: Path, extra=()):
    """Ejecuta el CLI en proceso y devuelve su codigo de salida."""
    from tools.audit.__main__ import main

    argv = [
        "--root", str(raiz),
        "--out", str(destino),
        "--timestamp", TIMESTAMP_FIJO,
    ]
    return main(argv + list(extra))


def _categorias(datos) -> set:
    """Categorias presentes en los hallazgos de un artefacto JSON."""
    return {h["category"] for h in datos["findings"]}


def _modulos_del_auditor():
    """Todos los archivos .py del paquete `tools/audit`."""
    raiz = Path(__file__).resolve().parent.parent / "tools" / "audit"
    return sorted(raiz.rglob("*.py"))


# ---------------------------------------------------------------------------
# 1. Corre sin secretos y publica artefactos
# ---------------------------------------------------------------------------

def test_la_corrida_completa_termina_sin_secretos_configurados(
    tmp_path, monkeypatch
):
    """Sin DB_URL ni claves de API el comando sale 0 y escribe latest.json."""
    for variable in ("DB_URL", "ODDS_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"

    assert _correr(raiz, destino) == 0
    assert (destino / "latest.json").is_file()
    assert (destino / "latest.md").is_file()


def test_el_artefacto_json_declara_las_diez_categorias(tmp_path):
    """Una categoria ausente se leeria como 'sin defectos'; nunca falta."""
    from tools.audit.model import Category

    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    esperadas = {cat.value for cat in Category}

    assert set(datos["counts"]["by_category"]) == esperadas
    assert set(datos["inspected"]) == esperadas


def test_todo_hallazgo_trae_ancla_impacto_y_arreglo(tmp_path):
    """Un hallazgo sin ancla o sin arreglo no es accionable."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    assert datos["findings"], "el arbol sintetico debe producir hallazgos"

    for hallazgo in datos["findings"]:
        assert hallazgo["evidence"], hallazgo["id"]
        assert hallazgo["impact"].strip(), hallazgo["id"]
        assert hallazgo["remediation"].strip(), hallazgo["id"]
        assert hallazgo["severity"] in {"S1", "S2", "S3", "S4"}
        for evidencia in hallazgo["evidence"]:
            assert evidencia["path"].strip(), hallazgo["id"]


# ---------------------------------------------------------------------------
# 2. Reproduce los defectos de linea base
# ---------------------------------------------------------------------------

def test_la_corrida_reproduce_los_defectos_de_linea_base(tmp_path):
    """Referencia rota, huerfano, config descentralizada y deriva documental."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    presentes = _categorias(datos)

    for categoria in (
        "broken-reference",
        "orphan-code",
        "config-inconsistency",
        "doc-drift",
    ):
        assert categoria in presentes, (
            f"la categoria '{categoria}' no se reprodujo; "
            f"presentes: {sorted(presentes)}"
        )


def test_la_config_descentralizada_se_ancla_en_la_constante_correcta(tmp_path):
    """El hallazgo nombra API_CREDITS_STOP_THRESHOLD y su archivo:linea."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    config = [
        h for h in datos["findings"]
        if h["category"] == "config-inconsistency"
    ]
    assert config, "no se detecto la constante fuera de config/settings.py"

    anclas = [
        evidencia
        for hallazgo in config
        for evidencia in hallazgo["evidence"]
        if evidencia.get("key") == "API_CREDITS_STOP_THRESHOLD"
    ]
    assert anclas, "ningun hallazgo apunta a API_CREDITS_STOP_THRESHOLD"
    assert any(a.get("line") for a in anclas), "falta el numero de linea"


def test_la_referencia_rota_apunta_al_import_inexistente(tmp_path):
    """El import de `config.database` se reporta con su archivo:linea."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    rotas = [
        h for h in datos["findings"] if h["category"] == "broken-reference"
    ]
    assert rotas, "no se detecto la referencia rota"
    assert any(
        "update_upcoming_matches.py" in evidencia["path"]
        for hallazgo in rotas
        for evidencia in hallazgo["evidence"]
    )


def test_los_hallazgos_de_riesgo_de_dinero_van_arriba(tmp_path):
    """El orden publicado pone S1 antes que S3: se lee de arriba hacia abajo."""
    from tools.audit.model import Severity
    from tools.audit.rank import rank

    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"
    _correr(raiz, destino)

    datos = json.loads((destino / "latest.json").read_text(encoding="utf-8"))
    severidades = [h["severity"] for h in datos["findings"]]

    assert severidades == sorted(severidades), (
        "el artefacto no esta ordenado por severidad"
    )

    # `rank` es total: nunca deja dos hallazgos con la misma clave de orden.
    from tools.audit.diff import load_run

    corrida = load_run(destino / "latest.json")
    claves = [
        (Severity(f.severity).value, f.category.value)
        for f in rank(corrida.findings)
    ]
    assert claves == sorted(claves)


# ---------------------------------------------------------------------------
# 3. El auditor no toca nada
# ---------------------------------------------------------------------------

def _imports_del_modulo(ruta: Path) -> set:
    """Nombres de modulo importados por un archivo, via `ast`.

    Se usa `ast` y no `grep` a proposito: `grep` confundiria un patron
    regex que contiene la palabra 'anthropic' con un import real, y el
    checker de error-handling tiene exactamente ese literal.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    nombres = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                nombres.add(alias.name)
        elif isinstance(nodo, ast.ImportFrom):
            if nodo.module:
                nombres.add(nodo.module)
    return nombres


def test_el_paquete_de_auditoria_no_importa_base_de_datos_ni_apis():
    """Cero SQLAlchemy, cero requests, cero anthropic, cero config.settings."""
    prohibidos = {
        "sqlalchemy",
        "psycopg2",
        "requests",
        "httpx",
        "urllib.request",
        "anthropic",
        "config.settings",
        "config.database",
    }

    ofensores = {}
    for ruta in _modulos_del_auditor():
        importados = _imports_del_modulo(ruta)
        malos = {
            nombre
            for nombre in importados
            if nombre in prohibidos
            or any(nombre.startswith(f"{p}.") for p in prohibidos)
        }
        if malos:
            ofensores[str(ruta)] = sorted(malos)

    assert not ofensores, f"el auditor importa lo que no debe: {ofensores}"


def test_el_paquete_de_auditoria_no_toca_el_lock_del_orchestrator():
    """Ni adquiere el lock ni lo nombra: puede correr durante el cron."""
    ofensores = []
    for ruta in _modulos_del_auditor():
        texto = ruta.read_text(encoding="utf-8")
        if "_acquire_lock" in texto or "orchestrator.lock" in texto:
            ofensores.append(str(ruta))

    assert not ofensores, f"el auditor referencia el lock: {ofensores}"


def test_la_corrida_solo_escribe_bajo_el_directorio_de_salida(tmp_path):
    """Nada del arbol auditado cambia: la auditoria es de solo lectura."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"

    antes = {
        ruta: ruta.read_bytes()
        for ruta in sorted(raiz.rglob("*"))
        if ruta.is_file()
    }

    _correr(raiz, destino)

    despues = {
        ruta: ruta.read_bytes()
        for ruta in sorted(raiz.rglob("*"))
        if ruta.is_file() and "__pycache__" not in ruta.parts
    }

    for ruta, contenido in despues.items():
        assert ruta in antes, f"el auditor creo {ruta} fuera de --out"
        assert antes[ruta] == contenido, f"el auditor modifico {ruta}"


# ---------------------------------------------------------------------------
# 4. El gate de CI
# ---------------------------------------------------------------------------

def test_el_gate_de_ci_falla_mientras_haya_un_import_que_no_resuelve(tmp_path):
    """`--ci` sale 1 con una referencia rota viva en el arbol."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"

    assert _correr(raiz, destino, extra=["--ci"]) == 1


def test_el_gate_de_ci_no_publica_artefactos(tmp_path):
    """Un latest.json parcial corromperia la linea base del diff."""
    raiz = _arbol_con_defectos(tmp_path)
    destino = tmp_path / "audits"

    _correr(raiz, destino, extra=["--ci"])

    assert not (destino / "latest.json").exists()
    assert not (destino / "latest.md").exists()


def test_el_gate_de_ci_pasa_en_un_arbol_limpio(tmp_path):
    """Sin imports rotos ni constantes sueltas, el gate sale 0."""
    raiz = tmp_path / "limpio"
    _escribir(
        raiz / "config" / "settings.py",
        "import os\n"
        "\n"
        "def env_int(nombre, default):\n"
        "    crudo = os.environ.get(nombre, '')\n"
        "    return int(crudo) if crudo.strip() else default\n"
        "\n"
        "INITIAL_BANKROLL = env_int('INITIAL_BANKROLL', 100)\n",
    )
    _escribir(
        raiz / "scripts" / "sano.py",
        "from config.settings import INITIAL_BANKROLL\n"
        "\n"
        "def main():\n"
        "    return INITIAL_BANKROLL\n",
    )
    _escribir(raiz / "requirements.txt", "PyYAML==6.0.2\n")

    assert _correr(raiz, tmp_path / "audits", extra=["--ci"]) == 0


def test_el_subconjunto_del_gate_sale_del_registro_no_de_una_lista(tmp_path):
    """Los nombres del gate existen como checkers registrados de verdad.

    Si alguien renombra un checker del gate, este test lo delata en vez de
    dejar que el gate apunte en silencio a un fantasma y deje pasar todo.
    """
    from tools.audit.__main__ import CHECKS_GATE
    from tools.audit.checks import load_all
    from tools.audit.registry import all_checks

    load_all()
    registrados = {spec.name for spec in all_checks()}

    faltantes = set(CHECKS_GATE) - registrados
    assert not faltantes, (
        f"el gate nombra checkers que no existen: {sorted(faltantes)}"
    )


def test_la_corrida_completa_usa_todos_los_checkers_registrados(tmp_path):
    """El CLI itera `all_checks()`; una lista fija ocultaria un checker nuevo."""
    from tools.audit.__main__ import build_run
    from tools.audit.checks import load_all
    from tools.audit.registry import all_checks, check, isolated_registry
    from tools.audit.model import Category

    raiz = _arbol_con_defectos(tmp_path)

    def _checker_de_prueba(root):
        return [], 999

    with isolated_registry():
        check("zzz-checker-de-prueba", Category.DOC_DRIFT)(_checker_de_prueba)
        assert [s.name for s in all_checks()] == ["zzz-checker-de-prueba"]

        corrida = build_run(raiz, timestamp=TIMESTAMP_FIJO)
        # Las 999 ubicaciones solo pueden venir del checker recien
        # registrado: el CLI lo tomo del registro, no de una lista.
        assert corrida.inspected["doc-drift"] == 999

    # Fuera del contexto aislado el registro real sigue intacto.
    load_all()
    assert len(all_checks()) > 1
