# Los tests unitarios no tocan la DB. Si DB_URL no está configurada
# localmente, settings.py lanza RuntimeError al importarse — damos un
# valor dummy (igual que hace .github/workflows/tests.yml).
import os

if not os.environ.get("DB_URL"):
    os.environ["DB_URL"] = "postgresql://dummy:dummy@localhost/dummy"

import sys  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Copias en memoria de un proceso de larga vida (10-nov-26): los titulares
    del dashboard (30 min) no pasan de una prueba a otra."""
    app = sys.modules.get("dashboard.app")
    if app is not None and hasattr(app, "_PROFILES_CACHE"):
        app._PROFILES_CACHE.clear()
    yield
