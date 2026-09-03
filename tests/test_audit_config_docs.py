"""Tests de los checkers de configuracion y deriva documental (T009).

Cubre `config-inconsistency` y `doc-drift`.

Las garantias fuertes se prueban contra arboles sinteticos en tmp_path que
reproducen la forma real del repositorio auditado: `config/settings.py` que
revienta en tiempo de import sin `DB_URL`, `API_CREDITS_STOP_THRESHOLD`
declarada en `scripts/update_upcoming_matches.py` en vez de en settings, el
README documentandola con un numero distinto al de `.env.example`, y un
`weekly.yml` que ejecuta el mismo codigo que `morning.yml` sin declarar las
mismas variables. Un arbol controlado permite afirmar "exactamente estos
hallazgos y en esta linea" sin depender de cuanto codigo de produccion tenga
el checkout. Contra el repositorio real solo se prueban los invariantes que
siempre tienen que valer: contrato de retorno, registro y determinismo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Valor con pinta de credencial. Se planta en el arbol sintetico para poder
# afirmar que NUNCA aparece en un hallazgo (los artefactos se commitean).
SECRETO_DE_PRUEBA = "sk-ant-api03-nunca-debe-salir-en-el-reporte"


# ---------------------------------------------------------------------------
# Constructores del arbol sintetico
# ---------------------------------------------------------------------------

def _escribir(raiz: Path, relativa: str, contenido: str) -> Path:
    """Crea un archivo (con sus directorios) dentro del arbol sintetico."""
    ruta = raiz / relativa
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _linea_de(ruta: Path, fragmento: str) -> int:
    """Numero de linea (1-based) donde aparece el fragmento.

    Se calcula del archivo real en vez de escribirse a mano: si alguien edita
    la cabecera del arbol sintetico, el test sigue anclando donde debe.
    """
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    for numero, texto in enumerate(lineas, start=1):
        if fragmento in texto:
            return numero
    raise AssertionError(f"No se encontro {fragmento!r} en {ruta}")


def _settings(raiz: Path) -> Path:
    """`config/settings.py` real: revienta en el IMPORT si falta `DB_URL`.

    Esa es la propiedad que obliga a leerlo con `ast`. Si algun dia el checker
    intentara importarlo, este archivo hace fallar el test en vez de dejar que
    la auditoria se rompa recien en el cron semanal sin secretos.
    """
    return _escribir(
        raiz,
        "config/settings.py",
        "import os\n"
        "\n"
        "\n"
        "def env_int(name, default):\n"
        "    v = os.environ.get(name, '')\n"
        "    return default if not v.strip() else int(v)\n"
        "\n"
        "\n"
        "DB_URL = os.environ.get('DB_URL', '')\n"
        "if not DB_URL:\n"
        "    raise RuntimeError('DB_URL no configurada')\n"
        "\n"
        "API_TTL_HOURS = env_int('API_TTL_HOURS', 8)\n"
        "INITIAL_BANKROLL = env_int('INITIAL_BANKROLL', 100)\n",
    )


def _workflow(
    raiz: Path, nombre: str, comando: str, variables: dict[str, str]
) -> Path:
    """Workflow con un step `run:` y un bloque `env:`, como los reales."""
    bloque = "".join(
        f"          {clave}: {valor}\n" for clave, valor in variables.items()
    )
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
        f"      - run: {comando}\n"
        "        env:\n" + bloque,
    )


def _arbol_con_constante_descentralizada(raiz: Path) -> Path:
    """Reproduce el defecto real: la constante fuera de `config/settings.py`.

    `API_CREDITS_STOP_THRESHOLD` usa el accesor correcto (`env_int`), asi que
    el defecto es exclusivamente su UBICACION.
    """
    _settings(raiz)
    return _escribir(
        raiz,
        "scripts/update_upcoming_matches.py",
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
        "\n"
        "from config.settings import env_int\n"
        "\n"
        "# Umbral de creditos: por debajo de esto se deja de pegarle a la API.\n"
        "API_CREDITS_STOP_THRESHOLD = env_int('API_CREDITS_STOP_THRESHOLD', 500)\n"
        "MAX_CREDITS_PER_RUN = env_int('MAX_CREDITS_PER_RUN', 800)\n",
    )


# ---------------------------------------------------------------------------
# Lectura de `config/settings.py` sin importarlo
# ---------------------------------------------------------------------------

def test_extract_settings_constants_lee_sin_importar(tmp_path):
    """Las constantes salen por `ast`, con `DB_URL` ausente del entorno.

    El settings sintetico lanza RuntimeError en tiempo de import: si el
    checker lo importara, esta llamada propagaria la excepcion en vez de
    devolver el diccionario.
    """
    from tools.audit.checks.config_docs import extract_settings_constants

    _settings(tmp_path)

    constantes = extract_settings_constants(tmp_path)

    assert constantes["API_TTL_HOURS"] == "8"
    assert constantes["INITIAL_BANKROLL"] == "100"


def test_extract_settings_constants_sin_archivo_devuelve_vacio(tmp_path):
    """Un subarbol sin `config/settings.py` no revienta: devuelve `{}`."""
    from tools.audit.checks.config_docs import extract_settings_constants

    assert extract_settings_constants(tmp_path) == {}


def test_checkers_corren_con_db_url_ausente(tmp_path, monkeypatch):
    """AC4: con `DB_URL` fuera del entorno, ambos checkers completan.

    Es la garantia que hace posible el workflow semanal sin secretos
    (FR-006). Se borra la variable que `tests/conftest.py` inyecta.
    """
    from tools.audit.checks.config_docs import check_config, check_doc_drift

    monkeypatch.delenv("DB_URL", raising=False)
    _arbol_con_constante_descentralizada(tmp_path)

    hallazgos_config, inspeccionadas_config = check_config(tmp_path)
    hallazgos_drift, inspeccionadas_drift = check_doc_drift(tmp_path)

    assert isinstance(hallazgos_config, list)
    assert isinstance(hallazgos_drift, list)
    assert inspeccionadas_config > 0
    assert inspeccionadas_drift > 0


# ---------------------------------------------------------------------------
# config-inconsistency: constantes fuera de la fuente unica de verdad
# ---------------------------------------------------------------------------

def test_constante_fuera_de_settings_se_ancla_en_su_linea_real(tmp_path):
    """AC1: la constante descentralizada se reporta en su `archivo:linea`."""
    from tools.audit.checks.config_docs import check_config
    from tools.audit.model import Category, Severity

    ruta = _arbol_con_constante_descentralizada(tmp_path)
    linea_esperada = _linea_de(ruta, "API_CREDITS_STOP_THRESHOLD = env_int")

    hallazgos, inspeccionadas = check_config(tmp_path)

    umbral = [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "API_CREDITS_STOP_THRESHOLD" for ev in hallazgo.evidence)
    ]
    assert len(umbral) == 1, "Se esperaba exactamente un hallazgo del umbral"

    hallazgo = umbral[0]
    assert hallazgo.category is Category.CONFIG_INCONSISTENCY
    assert hallazgo.severity is Severity.S1
    assert hallazgo.anchors == [
        f"scripts/update_upcoming_matches.py:{linea_esperada}"
    ]
    assert "config/settings.py" in hallazgo.remediation
    assert inspeccionadas > 0


def test_usar_el_accesor_correcto_no_absuelve_la_ubicacion(tmp_path):
    """El defecto es DONDE se declara, no COMO se lee.

    `API_CREDITS_STOP_THRESHOLD` ya usa `env_int`; si el checker solo mirara
    el accesor la dejaria pasar y la configuracion seguiria partida en dos.
    """
    from tools.audit.checks.config_docs import constantes_descentralizadas

    _arbol_con_constante_descentralizada(tmp_path)

    nombres = {d.nombre for d in constantes_descentralizadas(tmp_path)}

    assert "API_CREDITS_STOP_THRESHOLD" in nombres
    assert "MAX_CREDITS_PER_RUN" in nombres


def test_constantes_dentro_de_settings_no_se_reportan(tmp_path):
    """La fuente unica de verdad no se acusa a si misma."""
    from tools.audit.checks.config_docs import check_config

    _settings(tmp_path)

    hallazgos, _ = check_config(tmp_path)

    assert hallazgos == []


def test_constante_local_del_auditor_no_se_reporta(tmp_path):
    """`tools/` y `tests/` quedan exentos: no son codigo de produccion."""
    from tools.audit.checks.config_docs import check_config

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "tools/audit/demo.py",
        "import os\n\nAUDIT_MAX_FILES = os.environ.get('AUDIT_MAX_FILES', '10')\n",
    )

    hallazgos, _ = check_config(tmp_path)

    assert hallazgos == []


# ---------------------------------------------------------------------------
# config-inconsistency: matriz `env:` de los workflows
# ---------------------------------------------------------------------------

def _arbol_con_matriz_incompleta(raiz: Path) -> Path:
    """`morning.yml` declara el umbral, `weekly.yml` corre lo mismo sin el."""
    _settings(raiz)
    ruta = _escribir(
        raiz,
        "scripts/orchestrator.py",
        "import os\n"
        "\n"
        "\n"
        "def fetch():\n"
        "    umbral = os.environ.get('API_CREDITS_STOP_THRESHOLD', '500')\n"
        "    return umbral\n",
    )
    _workflow(
        raiz,
        "morning.yml",
        "python scripts/orchestrator.py --mode morning",
        {
            "DB_URL": "${{ secrets.DB_URL }}",
            "API_CREDITS_STOP_THRESHOLD": "${{ secrets.API_CREDITS_STOP_THRESHOLD }}",
        },
    )
    _workflow(
        raiz,
        "weekly.yml",
        "python scripts/orchestrator.py --mode weekly",
        {"DB_URL": "${{ secrets.DB_URL }}"},
    )
    return ruta


def test_workflow_que_consume_sin_declarar_se_reporta(tmp_path):
    """AC3: variable consumida por la ruta del workflow y ausente de su `env:`."""
    from tools.audit.checks.config_docs import check_config
    from tools.audit.model import Severity

    ruta = _arbol_con_matriz_incompleta(tmp_path)
    linea_consumo = _linea_de(ruta, "API_CREDITS_STOP_THRESHOLD")

    hallazgos, _ = check_config(tmp_path)

    huecos = [
        hallazgo
        for hallazgo in hallazgos
        if any(
            ev.path == ".github/workflows/weekly.yml"
            and ev.key == "API_CREDITS_STOP_THRESHOLD"
            for ev in hallazgo.evidence
        )
    ]
    assert len(huecos) == 1, "Se esperaba un solo hueco en weekly.yml"

    hallazgo = huecos[0]
    assert hallazgo.severity is Severity.S1

    anclas = hallazgo.anchors
    # El hallazgo cita las tres patas: el workflow incompleto, el codigo que
    # consume la variable y el workflow hermano que si la declara.
    assert ".github/workflows/weekly.yml:API_CREDITS_STOP_THRESHOLD" in anclas
    assert f"scripts/orchestrator.py:{linea_consumo}" in anclas
    assert ".github/workflows/morning.yml:API_CREDITS_STOP_THRESHOLD" in anclas


def test_workflow_completo_no_se_reporta(tmp_path):
    """`morning.yml` si declara la variable: no genera hallazgo propio."""
    from tools.audit.checks.config_docs import check_config

    _arbol_con_matriz_incompleta(tmp_path)

    hallazgos, _ = check_config(tmp_path)

    assert not [
        hallazgo
        for hallazgo in hallazgos
        if any(
            ev.path == ".github/workflows/morning.yml" and ev.line is None
            for ev in hallazgo.evidence
        )
        and hallazgo.evidence[0].path == ".github/workflows/morning.yml"
    ]


def test_variable_que_nadie_suministra_no_es_un_hueco(tmp_path):
    """Una variable que ningun workflow inyecta corre con su default.

    Eso es una decision deliberada, no una omision: reportarla convertiria
    las ~27 variables con default sano en ruido y enterraria el hueco real.
    """
    from tools.audit.checks.config_docs import check_config

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "scripts/solitario.py",
        "import os\n\n\ndef leer():\n"
        "    return os.environ.get('NADIE_ME_INYECTA', '1')\n",
    )
    _workflow(
        tmp_path,
        "solo.yml",
        "python scripts/solitario.py",
        {"DB_URL": "${{ secrets.DB_URL }}"},
    )

    hallazgos, _ = check_config(tmp_path)

    assert not [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "NADIE_ME_INYECTA" for ev in hallazgo.evidence)
    ]


# ---------------------------------------------------------------------------
# doc-drift: contradicciones entre fuentes
# ---------------------------------------------------------------------------

def _arbol_con_drift(raiz: Path) -> None:
    """README dice 50, `.env.example` dice 500. El desacuerdo real del repo."""
    _settings(raiz)
    _escribir(
        raiz,
        ".env.example",
        "# Plantilla de entorno\n"
        "ODDS_API_KEY=tu_api_key_aqui\n"
        "API_CREDITS_STOP_THRESHOLD=500\n",
    )
    _escribir(
        raiz,
        "README.md",
        "# Demo\n"
        "\n"
        "```env\n"
        "ODDS_API_KEY=tu_api_key_aqui\n"
        "API_CREDITS_STOP_THRESHOLD=50\n"
        "```\n",
    )


def test_drift_entre_readme_y_env_example_cita_ambos_lados(tmp_path):
    """AC2: el desacuerdo se reporta citando TODAS las ubicaciones."""
    from tools.audit.checks.config_docs import check_doc_drift
    from tools.audit.model import Category

    _arbol_con_drift(tmp_path)

    hallazgos, inspeccionadas = check_doc_drift(tmp_path)

    umbral = [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "API_CREDITS_STOP_THRESHOLD" for ev in hallazgo.evidence)
    ]
    assert len(umbral) == 1

    hallazgo = umbral[0]
    assert hallazgo.category is Category.DOC_DRIFT

    rutas = {ev.path for ev in hallazgo.evidence}
    assert rutas == {"README.md", ".env.example"}, (
        "Un reporte de un solo lado obliga a buscar el otro a mano"
    )

    # Ambos valores en disputa quedan escritos en el impacto: el operador
    # sabe cual es cual sin abrir los dos archivos.
    assert "`50`" in hallazgo.impact
    assert "`500`" in hallazgo.impact
    assert inspeccionadas > 0


def test_fuentes_de_acuerdo_no_generan_hallazgo(tmp_path):
    """Cuando README y `.env.example` coinciden, no hay deriva."""
    from tools.audit.checks.config_docs import check_doc_drift

    _settings(tmp_path)
    _escribir(tmp_path, ".env.example", "API_CREDITS_STOP_THRESHOLD=500\n")
    _escribir(
        tmp_path,
        "README.md",
        "```env\nAPI_CREDITS_STOP_THRESHOLD=500\n```\n",
    )

    hallazgos, _ = check_doc_drift(tmp_path)

    assert not [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "API_CREDITS_STOP_THRESHOLD" for ev in hallazgo.evidence)
    ]


def test_drift_entre_dos_fuentes_ejecutables_es_s1(tmp_path):
    """Codigo contra codigo cambia el comportamiento real, no solo la lectura."""
    from tools.audit.checks.config_docs import check_doc_drift
    from tools.audit.model import Severity

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "src/utils/anthropic_budget.py",
        "from config.settings import env_float\n"
        "\n"
        "ANTHROPIC_DAILY_BUDGET_USD = env_float('ANTHROPIC_DAILY_BUDGET_USD', 0.20)\n",
    )
    _escribir(
        tmp_path,
        "scripts/gate.py",
        "from config.settings import env_float\n"
        "\n"
        "ANTHROPIC_DAILY_BUDGET_USD = env_float('ANTHROPIC_DAILY_BUDGET_USD', 0.30)\n",
    )

    hallazgos, _ = check_doc_drift(tmp_path)

    presupuesto = [
        hallazgo
        for hallazgo in hallazgos
        if any(
            ev.key == "ANTHROPIC_DAILY_BUDGET_USD" for ev in hallazgo.evidence
        )
    ]
    assert len(presupuesto) == 1
    assert presupuesto[0].severity is Severity.S1


# ---------------------------------------------------------------------------
# doc-drift: comentarios que contradicen su constante
# ---------------------------------------------------------------------------

def test_comentario_que_contradice_la_constante_se_reporta(tmp_path):
    """El comentario afirma 30, la constante vale 7."""
    from tools.audit.checks.config_docs import check_doc_drift

    _settings(tmp_path)
    ruta = _escribir(
        tmp_path,
        "scripts/limpieza.py",
        "LOG_RETENTION_DAYS = 7   # se conservan 30 dias de logs\n",
    )
    linea = _linea_de(ruta, "LOG_RETENTION_DAYS")

    hallazgos, _ = check_doc_drift(tmp_path)

    comentario = [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "LOG_RETENTION_DAYS" for ev in hallazgo.evidence)
    ]
    assert len(comentario) == 1
    assert f"scripts/limpieza.py:{linea}" in comentario[0].anchors
    assert "`30`" in comentario[0].impact
    assert "`7`" in comentario[0].impact


def test_porcentaje_y_fraccion_no_son_deriva(tmp_path):
    """`0.05` comentado como `5%` es el mismo valor en otra unidad.

    Reportarlo seria justo el ruido que hace que nadie reabra el reporte.
    """
    from tools.audit.checks.config_docs import check_doc_drift

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "scripts/stake.py",
        "MAX_STAKE_PCT = 0.05    # tope duro por bet: 5% del bankroll\n",
    )

    hallazgos, _ = check_doc_drift(tmp_path)

    assert not [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "MAX_STAKE_PCT" for ev in hallazgo.evidence)
    ]


def test_comentario_de_acuerdo_no_genera_hallazgo(tmp_path):
    """Cuando el comentario dice lo mismo que la constante, no pasa nada."""
    from tools.audit.checks.config_docs import check_doc_drift

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "scripts/limpieza.py",
        "LOG_RETENTION_DAYS = 30   # se conservan 30 dias de logs\n",
    )

    hallazgos, _ = check_doc_drift(tmp_path)

    assert not [
        hallazgo
        for hallazgo in hallazgos
        if any(ev.key == "LOG_RETENTION_DAYS" for ev in hallazgo.evidence)
    ]


# ---------------------------------------------------------------------------
# Lectura de documentacion
# ---------------------------------------------------------------------------

def test_extract_documented_values_reconoce_las_tres_formas(tmp_path):
    """`NAME=value`, `| NAME | value |` y la prosa con `default`."""
    from tools.audit.checks.config_docs import extract_documented_values

    ruta = _escribir(
        tmp_path,
        "CLAUDE.md",
        "# Notas\n"
        "\n"
        "```env\n"
        "FETCH_DAYS_AHEAD=7\n"
        "```\n"
        "\n"
        "| Variable | Valor |\n"
        "|---|---|\n"
        "| INITIAL_BANKROLL | 100 |\n"
        "\n"
        "El presupuesto ANTHROPIC_DAILY_BUDGET_USD (default $0.30) gatea todo.\n",
    )

    valores = extract_documented_values(ruta)

    assert valores["FETCH_DAYS_AHEAD"] == "7"
    assert valores["INITIAL_BANKROLL"] == "100"
    assert valores["ANTHROPIC_DAILY_BUDGET_USD"] == "0.30"


def test_extract_documented_values_ignora_plantillas_y_secretos(tmp_path):
    """Ni los huecos por rellenar ni las credenciales entran a comparacion."""
    from tools.audit.checks.config_docs import extract_documented_values

    ruta = _escribir(
        tmp_path,
        ".env.example",
        "ODDS_API_KEY=tu_api_key_aqui\n"
        f"ANTHROPIC_API_KEY={SECRETO_DE_PRUEBA}\n"
        "DB_URL=postgresql+psycopg2://user:pass@localhost/db\n"
        "FETCH_DAYS_AHEAD=7\n",
    )

    valores = extract_documented_values(ruta)

    assert "ODDS_API_KEY" not in valores
    assert "ANTHROPIC_API_KEY" not in valores
    assert "DB_URL" not in valores
    assert valores["FETCH_DAYS_AHEAD"] == "7"


def test_extract_documented_values_sin_archivo_devuelve_vacio(tmp_path):
    """Un archivo inexistente devuelve `{}` en vez de reventar."""
    from tools.audit.checks.config_docs import extract_documented_values

    assert extract_documented_values(tmp_path / "no_existe.md") == {}


# ---------------------------------------------------------------------------
# Secretos: el auditor no puede ser el peor leak del repositorio
# ---------------------------------------------------------------------------

def test_ningun_hallazgo_expone_un_valor_secreto(tmp_path):
    """AC5: solo nombres de variable y ubicaciones, jamas el valor resuelto."""
    from tools.audit.checks.config_docs import check_config, check_doc_drift

    _settings(tmp_path)
    _escribir(
        tmp_path,
        ".env.example",
        f"ANTHROPIC_API_KEY={SECRETO_DE_PRUEBA}\n"
        "API_CREDITS_STOP_THRESHOLD=500\n",
    )
    _escribir(
        tmp_path,
        "README.md",
        "```env\n"
        f"ANTHROPIC_API_KEY={SECRETO_DE_PRUEBA}\n"
        "API_CREDITS_STOP_THRESHOLD=50\n"
        "```\n",
    )
    _escribir(
        tmp_path,
        "scripts/agente.py",
        "import os\n"
        "\n"
        f"ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', "
        f"'{SECRETO_DE_PRUEBA}')\n",
    )

    hallazgos = check_config(tmp_path)[0] + check_doc_drift(tmp_path)[0]
    assert hallazgos, "El arbol tiene defectos reales: algo debe reportarse"

    for hallazgo in hallazgos:
        texto = " ".join(
            [hallazgo.impact, hallazgo.remediation]
            + [f"{ev.anchor} {ev.quote}" for ev in hallazgo.evidence]
        )
        assert SECRETO_DE_PRUEBA not in texto, (
            f"El hallazgo {hallazgo.id} filtro un valor secreto"
        )


def test_el_nombre_de_la_variable_secreta_si_sobrevive(tmp_path):
    """Redactar el valor no puede borrar el nombre: sin el no es accionable."""
    from tools.audit.checks.config_docs import check_config

    _settings(tmp_path)
    _escribir(
        tmp_path,
        "scripts/agente.py",
        "import os\n\nANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')\n",
    )

    hallazgos, _ = check_config(tmp_path)

    assert any(
        "ANTHROPIC_API_KEY" in hallazgo.impact for hallazgo in hallazgos
    )


# ---------------------------------------------------------------------------
# Registro, contrato y determinismo contra el repositorio real
# ---------------------------------------------------------------------------

def test_ambos_checkers_estan_registrados():
    """Un checker escrito pero no registrado deja su categoria en cero."""
    import tools.audit.checks.config_docs  # noqa: F401  (registra al importar)
    from tools.audit.model import Category
    from tools.audit.registry import get_check

    config = get_check("config-inconsistency")
    drift = get_check("doc-drift")

    assert config is not None
    assert drift is not None
    assert config.category is Category.CONFIG_INCONSISTENCY
    assert drift.category is Category.DOC_DRIFT


def test_contrato_de_retorno_contra_el_repositorio_real():
    """El registro valida `(list[Finding], int)`; se ejecuta por esa via."""
    from tools.audit.registry import get_check

    for nombre in ("config-inconsistency", "doc-drift"):
        hallazgos, inspeccionadas = get_check(nombre).run(REPO_ROOT)
        assert isinstance(hallazgos, list)
        assert isinstance(inspeccionadas, int)
        assert inspeccionadas >= 0


def test_dos_corridas_sobre_el_mismo_arbol_dan_lo_mismo():
    """Determinismo: sin el, el diff corrida-a-corrida es ilegible (FR-030)."""
    from tools.audit.checks.config_docs import check_config, check_doc_drift

    for checker in (check_config, check_doc_drift):
        primera, inspeccionadas_1 = checker(REPO_ROOT)
        segunda, inspeccionadas_2 = checker(REPO_ROOT)

        assert [h.id for h in primera] == [h.id for h in segunda]
        assert inspeccionadas_1 == inspeccionadas_2


def test_las_anclas_nunca_usan_separador_de_windows(tmp_path):
    """El artefacto tiene que ser identico en Windows local y en ubuntu."""
    from tools.audit.checks.config_docs import check_config

    _arbol_con_constante_descentralizada(tmp_path)

    hallazgos, _ = check_config(tmp_path)
    assert hallazgos

    for hallazgo in hallazgos:
        for evidencia in hallazgo.evidence:
            assert "\\" not in evidencia.path
