"""
tests/conftest.py
=================
Bootstrap compartido para toda la suite de tests.

POR QUE ESTE ARCHIVO EXISTE:
config/settings.py levanta RuntimeError en tiempo de IMPORT cuando DB_URL
no esta definida (config/settings.py, seccion DATABASE). Cualquier test que
importe -- directa o transitivamente -- config.settings revienta durante la
fase de coleccion de pytest, antes de ejecutar una sola asercion.

Hasta ahora eso solo funcionaba en CI porque .github/workflows/tests.yml
inyectaba un DB_URL dummy en el bloque `env:` del step. Localmente (Windows,
sin .env) la suite fallaba. Este conftest mueve esa garantia al repo: pytest
importa conftest.py antes que cualquier modulo de test, asi que las variables
quedan puestas a nivel de modulo -- antes del primer import de settings.

Los tests unitarios NO tocan la base de datos: el valor dummy solo satisface
la validacion de arranque, nunca se abre una conexion.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

import pytest

# ============================================================
# ENTORNO DUMMY (nivel de modulo, no fixture)
# ============================================================
# Tiene que correr en el import de conftest.py, NO dentro de una fixture:
# para cuando una fixture se ejecuta, el modulo de test ya se importo y
# config.settings ya habria explotado.
#
# setdefault y no asignacion directa: si el desarrollador ya tiene un
# DB_URL real en su .env o en el entorno, se respeta tal cual. Solo
# rellenamos el hueco cuando la variable falta.
#
# El valor NO es una credencial: usuario/password/host son literales
# "dummy"/"localhost" y no resuelven a ninguna base real.
os.environ.setdefault(
    "DB_URL", "postgresql+psycopg2://dummy:dummy@localhost/dummy"
)

# Claves de API vacias. settings.py ya usa "" como default para todas
# estas, pero dejarlas explicitas documenta el contrato de la suite:
# ningun test debe pegarle a un servicio externo. Nunca poner aqui un
# valor con pinta de credencial real.
for _api_key_var in (
    "ODDS_API_KEY",
    "ANTHROPIC_API_KEY",
    "WEATHER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_BOT_TOKEN_PREKICKOFF",
    "TELEGRAM_CHAT_ID_PREKICKOFF",
):
    os.environ.setdefault(_api_key_var, "")


# ============================================================
# FIXTURES
# ============================================================
@pytest.fixture
def repo_root() -> Path:
    """Raiz del repositorio: el directorio que contiene requirements.txt.

    Se resuelve por estructura (tests/ cuelga de la raiz), no por cwd, para
    que la suite de el mismo resultado corriendo desde la raiz o desde
    tests/, en Windows local o en ubuntu-latest.
    """
    return Path(__file__).resolve().parent.parent
