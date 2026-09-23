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
