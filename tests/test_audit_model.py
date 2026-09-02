"""Tests de los contratos de datos del auditor estatico (T001)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Categorias y severidades
# ---------------------------------------------------------------------------

def test_category_tiene_exactamente_las_diez_categorias():
    """El enum Category contiene las diez categorias mandatadas y ninguna mas."""
    from tools.audit.model import Category

    esperadas = {
        "broken-reference",
        "orphan-code",
        "config-inconsistency",
        "doc-drift",
        "schema-drift",
        "error-handling-risk",
        "convention-violation",
        "secret-leakage",
        "circular-dependency",
        "ownership-overlap",
    }
    obtenidas = {c.value for c in Category}

    assert obtenidas == esperadas
    assert len(list(Category)) == 10


def test_severity_es_la_escala_s1_a_s4():
    """Severity expone S1..S4 y se compara como string."""
    from tools.audit.model import Severity

    assert [s.value for s in Severity] == ["S1", "S2", "S3", "S4"]
    assert Severity.S1 == "S1"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_evidence_es_hashable_y_arma_anclas():
    """Evidence es frozen (hashable) y rinde ancla path:line o path:key."""
    from tools.audit.model import Evidence

    linea = Evidence(path="scripts/watchdog.py", line=27)
    clave = Evidence(path=".github/workflows/weekly.yml", key="env.MAX_CREDITS_PER_RUN")
    archivo = Evidence(path="src/betting/staking.py")

    assert linea.anchor == "scripts/watchdog.py:27"
    assert clave.anchor == ".github/workflows/weekly.yml:env.MAX_CREDITS_PER_RUN"
    assert archivo.anchor == "src/betting/staking.py"

    # frozen -> se puede meter en un set para deduplicar
    assert len({linea, linea, clave}) == 2


# ---------------------------------------------------------------------------
# ID estable
# ---------------------------------------------------------------------------

def _finding(path, line=10, categoria=None):
    from tools.audit.model import Category, Evidence, Finding, Severity

    return Finding(
        category=categoria or Category.BROKEN_REFERENCE,
        severity=Severity.S1,
        evidence=[Evidence(path=path, line=line, quote="from src.betting import x")],
        impact="El import no resuelve y mata el pipeline.",
        remediation="Reparar o archivar el modulo.",
    )


def test_findings_identicos_producen_el_mismo_id():
    """Dos Finding con misma categoria+evidencia comparten .id."""
    uno = _finding("src/pipeline/prediction_pipeline.py")
    dos = _finding("src/pipeline/prediction_pipeline.py")

    assert uno.id == dos.id
    assert uno.id.startswith("broken-reference-")


def test_el_id_sobrevive_a_mover_el_archivo_de_directorio():
    """Cambiar el directorio padre no cambia el .id (tolerancia a movimientos)."""
    antes = _finding("src/pipeline/prediction_pipeline.py")
    despues = _finding("archive/legacy/pipeline/prediction_pipeline.py")

    assert antes.id == despues.id


def test_el_id_ignora_el_numero_de_linea():
    """Insertar lineas arriba desplaza el ancla pero no reabre el hallazgo."""
    antes = _finding("scripts/orchestrator.py", line=120)
    despues = _finding("scripts/orchestrator.py", line=173)

    assert antes.id == despues.id


def test_el_id_cambia_con_la_categoria():
    """La categoria participa en el hash: distinta categoria, distinto id."""
    from tools.audit.model import Category

    ref = _finding("scripts/orchestrator.py")
    conv = _finding("scripts/orchestrator.py", categoria=Category.CONVENTION_VIOLATION)

    assert ref.id != conv.id


def test_el_id_cambia_con_evidencia_distinta():
    """Otro archivo distinto -> hallazgo distinto."""
    uno = _finding("scripts/orchestrator.py")
    dos = _finding("scripts/watchdog.py")

    assert uno.id != dos.id


def test_finding_normaliza_strings_a_enum():
    """Los checkers pueden pasar strings crudos; se normalizan a enum."""
    from tools.audit.model import Category, Evidence, Finding, Severity

    f = Finding(
        category="secret-leakage",
        severity="S1",
        evidence=[Evidence(path=".env.example", line=3)],
        impact="Credencial en el arbol de fuentes.",
        remediation="Rotar y mover a secrets de GH Actions.",
    )

    assert f.category is Category.SECRET_LEAKAGE
    assert f.severity is Severity.S1
    assert f.status == "open"
    assert f.effort == "S"
    assert f.uncertain is False


# ---------------------------------------------------------------------------
# AuditRun
# ---------------------------------------------------------------------------

def test_counts_by_severity_rellena_en_cero_las_cuatro_severidades():
    """Una severidad sin hallazgos vale 0, no desaparece del reporte."""
    from tools.audit.model import AuditRun, Category, Evidence, Finding, Severity

    findings = [
        Finding(
            category=Category.BROKEN_REFERENCE,
            severity=Severity.S1,
            evidence=[Evidence(path="a.py", line=1)],
            impact="i",
            remediation="r",
        ),
        Finding(
            category=Category.DOC_DRIFT,
            severity=Severity.S1,
            evidence=[Evidence(path="b.py", line=2)],
            impact="i",
            remediation="r",
        ),
        Finding(
            category=Category.ORPHAN_CODE,
            severity=Severity.S3,
            evidence=[Evidence(path="c.py", line=3)],
            impact="i",
            remediation="r",
        ),
    ]
    run = AuditRun(
        run_id="run-1",
        timestamp="2026-09-01T00:00:00Z",
        commit_sha="29a880a17b53f8d67698ee74cd298548d814ea12",
        findings=findings,
        inspected={"python_files": 139},
    )

    assert run.counts_by_severity() == {"S1": 2, "S2": 0, "S3": 1, "S4": 0}
    assert run.counts_by_category()["broken-reference"] == 1
    assert run.counts_by_category()["schema-drift"] == 0
    assert len(run.counts_by_category()) == 10


def test_audit_run_arranca_vacio_sin_argumentos_opcionales():
    """findings e inspected tienen default seguro."""
    from tools.audit.model import AuditRun

    run = AuditRun(run_id="r", timestamp="t", commit_sha="sha")

    assert run.findings == []
    assert run.inspected == {}
    assert run.counts_by_severity() == {"S1": 0, "S2": 0, "S3": 0, "S4": 0}


# ---------------------------------------------------------------------------
# Redaccion
# ---------------------------------------------------------------------------

def test_redact_borra_el_valor_y_conserva_el_nombre_de_la_variable():
    """El nombre de la variable sobrevive; el valor de la llave no."""
    from tools.audit.redact import redact

    salida = redact('ODDS_API_KEY = "a1b2c3d4e5f6g7h8i9j0"')

    assert "ODDS_API_KEY" in salida
    assert "a1b2c3d4e5f6g7h8i9j0" not in salida
    assert "<redacted>" in salida


def test_redact_cubre_varias_formas_de_asignacion():
    """Sin comillas, en dict y en cadena de conexion."""
    from tools.audit.redact import redact

    sin_comillas = redact("TELEGRAM_BOT_TOKEN=8123456789abcdefghij")
    assert "TELEGRAM_BOT_TOKEN" in sin_comillas
    assert "8123456789abcdefghij" not in sin_comillas

    en_dict = redact('{"api_key": "supersecretvalue123"}')
    assert "api_key" in en_dict
    assert "supersecretvalue123" not in en_dict

    conexion = redact("postgresql+psycopg2://usuario:contrasena123@localhost:5432/db")
    assert "contrasena123" not in conexion
    assert "localhost" in conexion


def test_redact_borra_tokens_reconocibles_sin_nombre_alrededor():
    """Un token con forma conocida se redacta aunque este suelto."""
    from tools.audit.redact import redact

    salida = redact("llamada con sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")

    assert "sk-ant-api03" not in salida
    assert "<redacted>" in salida


def test_redact_preserva_las_referencias_al_secreto():
    """env_str(...) es la linea CORRECTA: debe quedar legible."""
    from tools.audit.redact import redact

    salida = redact('TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN", "")')

    assert "env_str" in salida
    assert "<redacted>" not in salida


def test_redact_no_toca_texto_inocuo():
    """Prosa normal pasa intacta."""
    from tools.audit.redact import redact

    texto = "El pipeline filtra por edge_market, no por edge_ev."
    assert redact(texto) == texto


# ---------------------------------------------------------------------------
# Independencia de secretos (FR-006)
# ---------------------------------------------------------------------------

def test_el_modulo_importa_sin_db_url_configurada():
    """import tools.audit.model sale 0 aunque DB_URL no exista."""
    import os
    import subprocess

    entorno = dict(os.environ)
    entorno.pop("DB_URL", None)

    proc = subprocess.run(
        [sys.executable, "-c", "import tools.audit.model"],
        cwd=str(REPO_ROOT),
        env=entorno,
        stdin=subprocess.DEVNULL,  # el runner puede no tener stdin valido
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr


def test_ningun_modulo_de_audit_importa_config():
    """Nada bajo tools/ puede importar config.settings ni config.database."""
    for archivo in (REPO_ROOT / "tools").rglob("*.py"):
        fuente = archivo.read_text(encoding="utf-8")
        assert "import config" not in fuente, archivo
        assert "from config" not in fuente, archivo


def test_el_modelo_solo_usa_stdlib():
    """model.py y redact.py no importan terceros."""
    import ast

    terceros = {"yaml", "requests", "sqlalchemy", "pandas", "numpy", "pytest"}
    for nombre in ("model.py", "redact.py"):
        ruta = REPO_ROOT / "tools" / "audit" / nombre
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    assert alias.name.split(".")[0] not in terceros, nombre
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                assert nodo.module.split(".")[0] not in terceros, nombre
