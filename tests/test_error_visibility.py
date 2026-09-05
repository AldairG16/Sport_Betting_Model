"""
tests/test_error_visibility.py
==============================
Contrato de VISIBILIDAD de fallos (T017).

Que fija este archivo:
un handler ancho (`except Exception` / `except BaseException` / `except:`)
en una ruta de LLM, notificacion o escritura a DB no puede tragarse el
error. O lo reporta -- imprimiendo/logueando algo que incluye la excepcion
-- o lo re-lanza. Nunca `pass` a secas.

Por que importa: `except Exception: pass` alrededor de la llamada al
analista ya causo un dia entero de fallos silenciosos en produccion
(documentado en CLAUDE.md). El sintoma era "sin value bets hoy", que es
indistinguible de un dia sano.

La verificacion es ESTATICA (ast). No se importan los scripts: arrastran
config.database / anthropic / la conexion real a Postgres, y el objetivo
es que la suite corra sin secretos ni DB.

Ojo con el matiz: el objetivo es visibilidad, NO fragilidad. Un handler
que existe porque el feed de arriba es genuinamente poco fiable (CSV de
football-data con 24-36 h de lag, 422 de Odds API) tiene que seguir
manteniendo la corrida viva. Por eso el contrato pide un diagnostico, no
un re-raise.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# Los cuatro modulos remediados en T017.
TOUCHED = (
    "scripts/pre_kickoff_analyst.py",
    "scripts/notify_telegram.py",
    "scripts/orchestrator.py",
    "scripts/audit_analyst_calibration.py",
)

BROAD = {"Exception", "BaseException"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _parse(rel_path: str):
    """Devuelve (source, ast.Module) de un archivo del repo."""
    path = _repo_root() / rel_path
    assert path.exists(), f"{rel_path} no existe"
    src = path.read_text(encoding="utf-8")
    return src, ast.parse(src, filename=rel_path)


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """True si el handler captura Exception/BaseException o es bare `except:`."""
    if handler.type is None:
        return True
    node = handler.type
    elements = node.elts if isinstance(node, ast.Tuple) else [node]
    names = []
    for element in elements:
        if isinstance(element, ast.Name):
            names.append(element.id)
        elif isinstance(element, ast.Attribute):
            names.append(element.attr)
    return any(name in BROAD for name in names)


def _broad_handlers(tree: ast.Module):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_broad(node)
    ]


def _body_is_only_pass(handler: ast.ExceptHandler) -> bool:
    """Un cuerpo compuesto solo de `pass` / `...` es un tragado silencioso."""
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        return False
    return True


def _reports_exception(handler: ast.ExceptHandler) -> bool:
    """El handler hace visible el fallo?

    Dos formas validas:
      1. re-lanza (`raise` pelado o `raise X`), o
      2. liga la excepcion (`as e`) y usa ese nombre en el cuerpo --
         print, logger.log, formateo en un f-string, append a la lista de
         errores por bet, etc.
    """
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
    if not handler.name:
        return False
    bound = handler.name
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == bound:
                return True
    return False


def _describe(rel_path: str, handler: ast.ExceptHandler) -> str:
    return f"{rel_path}:{handler.lineno}"


# ============================================================
# CRITERIO 1 -- ningun `except ...: pass`
# ============================================================
@pytest.mark.parametrize("rel_path", TOUCHED)
def test_no_silent_pass_handler(rel_path):
    """Ningun handler ancho puede tener un cuerpo que sea solo `pass`."""
    _, tree = _parse(rel_path)
    offenders = [
        _describe(rel_path, h)
        for h in _broad_handlers(tree)
        if _body_is_only_pass(h)
    ]
    assert not offenders, (
        "Handlers anchos que se tragan el error sin dejar rastro:\n  "
        + "\n  ".join(offenders)
    )


# ============================================================
# CRITERIO 2 -- todo handler ancho emite un diagnostico con la excepcion
# ============================================================
@pytest.mark.parametrize("rel_path", TOUCHED)
def test_every_broad_handler_surfaces_the_exception(rel_path):
    """Cada handler ancho reporta la excepcion o la re-lanza."""
    _, tree = _parse(rel_path)
    offenders = [
        _describe(rel_path, h)
        for h in _broad_handlers(tree)
        if not _reports_exception(h)
    ]
    assert not offenders, (
        "Handlers anchos sin diagnostico (ni `as e` usado, ni re-raise):\n  "
        + "\n  ".join(offenders)
    )


# ============================================================
# CRITERIO 3 -- run_step sigue aislando fallos (visibilidad != fragilidad)
# ============================================================
def test_run_step_still_isolates_step_failures():
    """run_step reporta y devuelve False: un step roto no aborta la corrida."""
    _, tree = _parse("scripts/orchestrator.py")
    run_step = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "run_step"
        ),
        None,
    )
    assert run_step is not None, "orchestrator.py ya no define run_step"

    handlers = [h for h in ast.walk(run_step) if isinstance(h, ast.ExceptHandler)]
    assert handlers, "run_step dejo de capturar el fallo del step"

    for handler in handlers:
        raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
        assert not raises, (
            f"run_step re-lanza en la linea {handler.lineno}: un step fallido "
            "abortaria el pipeline entero"
        )
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert returns, (
            f"El handler de run_step en la linea {handler.lineno} ya no devuelve "
            "un valor -- el contrato es reportar y seguir"
        )


def test_run_step_failure_is_logged_with_the_exception():
    """El fallo de un step queda en el log con la excepcion, no en silencio."""
    _, tree = _parse("scripts/orchestrator.py")
    run_step = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_step"
    )
    handlers = [h for h in ast.walk(run_step) if isinstance(h, ast.ExceptHandler)]
    assert all(_reports_exception(h) for h in handlers)


# ============================================================
# CRITERIO 4 -- el filtro NULL de la auditoria deja de ser silencioso
# ============================================================
def test_calibration_audit_does_not_filter_nulls_inside_sql():
    """El WHERE ya no puede esconder filas: el descarte pasa a ser explicito."""
    src, _ = _parse("scripts/audit_analyst_calibration.py")
    lowered = " ".join(src.lower().split())
    assert "probability is not null" not in lowered, (
        "El filtro NULL sigue dentro del SQL: las filas descartadas no se "
        "pueden contar ni reportar"
    )


def test_calibration_audit_reports_excluded_row_count():
    """Se imprime cuantas filas descarto el filtro NULL, con el numero real."""
    _, tree = _parse("scripts/audit_analyst_calibration.py")

    reporting_prints = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.JoinedStr):
                continue
            literal = "".join(
                part.value
                for part in arg.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ).lower()
            interpolates = any(
                isinstance(part, ast.FormattedValue) for part in arg.values
            )
            if interpolates and any(
                token in literal for token in ("exclu", "descartad", "filtrad")
            ):
                reporting_prints.append(node.lineno)

    assert reporting_prints, (
        "audit_analyst_calibration.py no imprime el conteo de filas excluidas "
        "por el filtro de probability NULL"
    )


# ============================================================
# No se abre un segundo canal de alerta
# ============================================================
@pytest.mark.parametrize(
    "rel_path",
    # notify_telegram.py queda fuera a proposito: ESE es el canal. Es el
    # unico modulo autorizado a hablar con api.telegram.org.
    [p for p in TOUCHED if p != "scripts/notify_telegram.py"],
)
def test_no_second_alerting_channel(rel_path):
    """Los fallos recien visibles usan el Telegram/heartbeat que ya existe."""
    src, _ = _parse(rel_path)
    assert "api.telegram.org" not in src, (
        f"{rel_path} habla directo con la API de Telegram: hay que enrutar por "
        "send_message / send_message_prekickoff, no abrir otro canal"
    )
