"""
tests/test_entrypoints.py
=========================
Los workflows ejecutan `python scripts/X.py`: en ese modo la raíz del repo
NO está en sys.path hasta que el script la agrega. pytest sí la agrega por
su cuenta, así que un `from src...` puesto antes del sys.path.append pasa
todos los tests y revienta en producción (pasó el 22-sep-26 al introducir
el logging en el orchestrator; lo atrapó una prueba manual).

Estos tests lanzan cada entrypoint en un subproceso con el mismo sys.path
que tendría en Actions, sin ejecutar su __main__.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
ENV = {**os.environ,
       "DB_URL": os.environ.get("DB_URL") or "postgresql://dummy:dummy@localhost/dummy",
       "PYTHONIOENCODING": "utf-8"}

# Scripts que corren los workflows (o sus pasos) como `python scripts/X.py`
SCRIPTS = ["orchestrator", "update_upcoming_matches", "update_closing_odds", "clv_gate",
           "fetch_results", "revalidate_pending_bets", "weekly_sanity_audit",
           "clv_audit", "watchdog", "resolve_pending_bets"]


def test_orchestrator_starts_as_a_script():
    r = subprocess.run([sys.executable, "scripts/orchestrator.py", "--help"],
                       cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "--mode" in r.stdout


def _lazy_imports(path: Path) -> list[tuple[str, str, int]]:
    """(módulo, nombre, línea) de cada `from X import y` DENTRO de funciones."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    found += [(node.module, a.name, node.lineno) for a in node.names]
    return found


PROJECT_PACKAGES = ("src", "scripts", "config", "dashboard")


def _project_from_imports() -> list[tuple[str, str, str]]:
    """(archivo, módulo, nombre) de cada `from <paquete del proyecto> import
    nombre` en el código de producción, a cualquier nivel (módulo o función)."""
    import ast
    found = []
    for pkg in PROJECT_PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                        and node.module.split(".")[0] in PROJECT_PACKAGES):
                    rel = str(path.relative_to(ROOT))
                    found += [(f"{rel}:{node.lineno}", node.module, a.name) for a in node.names
                              if a.name != "*"]
    return found


def test_every_project_import_resolves():
    """
    Cada `from src.x import y` del proyecto apunta a algo que existe. Un
    import "sin uso" puede ser un re-export del que depende otro módulo, y
    los imports dentro de funciones solo fallan cuando se ejecuta esa rama
    (a veces una vez por semana, en producción).
    """
    import importlib
    broken = []
    for where, module, name in _project_from_imports():
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as e:
            if (e.name or "").split(".")[0] in PROJECT_PACKAGES:
                broken.append(f"{where}: {module} ({e})")
            continue   # dependencia opcional de terceros
        if hasattr(mod, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")   # submódulo
        except ModuleNotFoundError:
            broken.append(f"{where}: {module}.{name}")
    assert not broken, "imports rotos:\n" + "\n".join(broken)


def test_orchestrator_lazy_imports_resolve():
    """Los pasos del orchestrator importan dentro de la función (arranque
    rápido): un nombre renombrado o borrado no rompe ningún test y revienta
    recién en la corrida de producción. Aquí se resuelven todos."""
    import importlib
    missing = []
    for module, name, line in _lazy_imports(ROOT / "scripts" / "orchestrator.py"):
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as e:
            # dependencia opcional de terceros (ej. soccerdata en CI): no es
            # un nombre del proyecto roto
            if (e.name or "").split(".")[0] in ("src", "scripts", "config"):
                missing.append(f"línea {line}: {module} ({e})")
            continue
        if not hasattr(mod, name):
            missing.append(f"línea {line}: {module}.{name}")
    assert not missing, "imports rotos en orchestrator.py:\n" + "\n".join(missing)


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_imports_with_actions_sys_path(name):
    """Ejecuta el nivel superior del script (imports) con sys.path[0] =
    scripts/, como `python scripts/X.py`, pero sin correr su __main__."""
    code = (
        "import sys, runpy\n"
        "sys.path = [p for p in sys.path if p not in ('', '.')]\n"
        f"sys.path.insert(0, r'{ROOT / 'scripts'}')\n"
        f"runpy.run_path(r'{ROOT / 'scripts' / (name + '.py')}', run_name='entrypoint_smoke')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "scripts", env=ENV,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
