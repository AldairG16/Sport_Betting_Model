"""Tests del checker de filtracion de credenciales (T010).

Dos contratos, y el segundo importa mas que el primero:

1. una credencial literal en fuente versionada es S1, con nombre y ubicacion, y
2. el VALOR jamas aparece en el hallazgo — ni en la cita, ni en el impacto, ni
   en la remediacion, ni en el JSON serializado. Un checker de secretos que
   filtra el secreto es peor que no tener checker.

Ademas se fija lo que NO se reporta: los placeholders de `.env.example` y las
referencias `${{ secrets.X }}` de los workflows.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arbol(raiz, archivos):
    """Materializa un arbol sintetico {ruta relativa: contenido}."""
    for relativa, contenido in archivos.items():
        ruta = raiz / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
    return raiz


def _hallazgos_de(raiz):
    """Corre el checker y devuelve solo la lista de hallazgos."""
    from tools.audit.checks.secrets import check_secrets

    hallazgos, _ = check_secrets(raiz)
    return hallazgos


def _serializado(hallazgo):
    """Todo el texto que el hallazgo puede llegar a escribir al artefacto."""
    import json

    from tools.audit.report import finding_to_dict

    return json.dumps(finding_to_dict(hallazgo), ensure_ascii=False)


# Valor sintetico con forma de llave. NO es una credencial real.
_VALOR = "9f3a1c7e5b2d4f6a8c0e"

_MODULO_CON_LLAVE = f'''"""Cliente que embebe la llave en el codigo."""

ODDS_API_KEY = "{_VALOR}"


def cliente():
    return ODDS_API_KEY
'''

_MODULO_LIMPIO = '''"""Cliente que lee la llave del entorno."""

import os

from config.settings import env_str

ODDS_API_KEY = env_str("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
'''

_ENV_EXAMPLE = """# Plantilla de configuracion — sin valores reales
DB_URL=postgresql+psycopg2://usuario:contrasena@localhost:5432/sports_betting
ODDS_API_KEY=tu_api_key_aqui
TELEGRAM_BOT_TOKEN=tu_bot_token
TELEGRAM_CHAT_ID=tu_chat_id
ANTHROPIC_API_KEY=your_anthropic_key_here
"""

# Reproduce scripts/orchestrator.py:113-115: el nombre termina en KEY pero el
# valor es una sport key del catalogo de The Odds API.
_MODULO_SPORT_CATALOGO = '''"""Activacion del Mundial 2026."""

from datetime import date


def _check_world_cup_activation():
    WORLD_CUP_START = date(2026, 6, 11)
    WORLD_CUP_END = date(2026, 7, 19)
    WORLD_CUP_KEY = "soccer_fifa_world_cup"
    return WORLD_CUP_KEY, WORLD_CUP_START, WORLD_CUP_END
'''

_ENV_CON_SLUG = """DEFAULT_SPORT_KEY=soccer_epl
MARKET_KEY=over_2_5
"""

_WORKFLOW = """name: morning
on:
  schedule:
    - cron: '0 12 * * *'
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python scripts/orchestrator.py --mode morning
        env:
          DB_URL: ${{ secrets.DB_URL }}
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
"""


# ---------------------------------------------------------------------------
# Deteccion
# ---------------------------------------------------------------------------

def test_credencial_literal_es_s1(tmp_path):
    """Una llave commiteada ya esta comprometida: no admite matices."""
    raiz = _arbol(tmp_path, {"src/utils/client.py": _MODULO_CON_LLAVE})

    hallazgos = _hallazgos_de(raiz)

    assert len(hallazgos) == 1
    assert hallazgos[0].severity.value == "S1"
    assert hallazgos[0].category.value == "secret-leakage"


def test_hallazgo_lleva_nombre_y_ubicacion(tmp_path):
    """Sin nombre ni ancla el hallazgo no es accionable."""
    raiz = _arbol(tmp_path, {"src/utils/client.py": _MODULO_CON_LLAVE})

    hallazgo = _hallazgos_de(raiz)[0]

    assert "ODDS_API_KEY" in hallazgo.impact
    assert "src/utils/client.py:3" in hallazgo.anchors


def test_el_valor_nunca_llega_al_artefacto(tmp_path):
    """La regla que no se negocia: el secreto no viaja al reporte."""
    raiz = _arbol(tmp_path, {"src/utils/client.py": _MODULO_CON_LLAVE})

    hallazgo = _hallazgos_de(raiz)[0]
    texto = _serializado(hallazgo)

    assert _VALOR not in texto
    assert "ODDS_API_KEY" in texto
    assert "<redacted>" in texto


def test_token_con_forma_reconocible_se_detecta(tmp_path):
    """Una llave de Anthropic se reconoce por su forma, sin nombre alrededor."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/debug.py": (
                '"""Prueba manual."""\n\n'
                'CABECERA = {"x-api-key": "sk-ant-' + "a1b2c3d4e5f6g7h8i9j0" + '"}\n'
            )
        },
    )

    hallazgos = _hallazgos_de(raiz)

    assert len(hallazgos) >= 1
    assert all(h.severity.value == "S1" for h in hallazgos)
    assert "a1b2c3d4e5f6g7h8i9j0" not in _serializado(hallazgos[0])


# ---------------------------------------------------------------------------
# Lo que NO se reporta
# ---------------------------------------------------------------------------

def test_env_example_no_se_reporta(tmp_path):
    """Los placeholders de la plantilla son el punto del archivo."""
    raiz = _arbol(tmp_path, {".env.example": _ENV_EXAMPLE})

    assert _hallazgos_de(raiz) == []


def test_referencias_de_workflow_no_se_reportan(tmp_path):
    """`${{ secrets.X }}` es la forma correcta, no una filtracion."""
    raiz = _arbol(tmp_path, {".github/workflows/morning.yml": _WORKFLOW})

    assert _hallazgos_de(raiz) == []


def test_lectura_desde_el_entorno_no_se_reporta(tmp_path):
    """`env_str(...)` y `os.environ` son referencias, no secretos."""
    raiz = _arbol(tmp_path, {"config/settings.py": _MODULO_LIMPIO})

    assert _hallazgos_de(raiz) == []


def test_sport_key_no_se_reporta(tmp_path):
    """`WORLD_CUP_KEY = "soccer_fifa_world_cup"` es catalogo, no credencial.

    Es el falso positivo real de scripts/orchestrator.py:115: el nombre termina
    en KEY, asi que entra por nombre, pero el valor es una sport key de The
    Odds API. Un S1 permanente y falso en el tope del ranking vacia de
    significado a la severidad que responde "que puede costarme dinero".
    """
    raiz = _arbol(tmp_path, {"scripts/orchestrator.py": _MODULO_SPORT_CATALOGO})

    assert _hallazgos_de(raiz) == []


def test_slug_fuera_de_python_tampoco_se_reporta(tmp_path):
    """La misma exencion aplica a la deteccion por texto (.env, .yml)."""
    raiz = _arbol(tmp_path, {"config/catalog.env": _ENV_CON_SLUG})

    assert _hallazgos_de(raiz) == []


def test_la_exencion_de_slug_no_apaga_la_deteccion(tmp_path):
    """Excusar los slugs no puede volverse un escondite para una llave real.

    Mismo nombre `*_KEY`, valor sin forma de slug: sigue siendo S1. Y un token
    con forma reconocible se detecta aunque el nombre parezca de catalogo,
    porque `_hits_forma` no mira el nombre de la variable.
    """
    raiz = _arbol(
        tmp_path,
        {
            "src/utils/client.py": (
                '"""Cliente."""\n\n'
                f'WORLD_CUP_KEY = "{_VALOR}"\n'
            ),
            "src/utils/otro.py": (
                '"""Otro cliente."""\n\n'
                'SPORT_KEY = "sk-ant-' + "a1b2c3d4e5f6g7h8i9j0" + '"\n'
            ),
        },
    )

    hallazgos = _hallazgos_de(raiz)
    nombres = {h.evidence[0].key for h in hallazgos}

    assert all(h.severity.value == "S1" for h in hallazgos)
    assert "WORLD_CUP_KEY" in nombres
    assert "ANTHROPIC_API_KEY" in nombres


def test_predicado_de_enum_es_estrecho():
    """El colador acepta catalogo y rechaza cualquier cosa con forma de llave."""
    from tools.audit.checks.secrets import es_valor_enum

    assert es_valor_enum("soccer_fifa_world_cup") is True
    assert es_valor_enum("over_2_5") is True
    assert es_valor_enum("soccer_epl_1x2") is True
    assert es_valor_enum("SOCCER_FIFA_WORLD_CUP") is True

    # Sin separadores no hay slug que valga.
    assert es_valor_enum(_VALOR) is False
    # Caja mezclada: firma de token generado.
    assert es_valor_enum("soccer_Fifa_World_Cup") is False
    # Segmentos aleatorios aunque haya separadores.
    assert es_valor_enum("9f3a1c7e5b_2d4f6a8c0e") is False
    # Por encima del largo maximo no se excusa nada.
    assert es_valor_enum("_".join(["liga"] * 12)) is False


def test_fixture_de_la_suite_baja_a_s2_pero_no_desaparece(tmp_path):
    """La suite necesita literales con forma de llave para probar la redaccion.

    Suprimirlos abriria un escondite perfecto para una credencial real, asi
    que el hallazgo sigue ahi: solo baja de severidad y queda marcado como
    incierto para que S1 conserve su significado.
    """
    raiz = _arbol(tmp_path, {"tests/test_algo.py": _MODULO_CON_LLAVE})

    hallazgos = _hallazgos_de(raiz)

    assert len(hallazgos) == 1
    assert hallazgos[0].severity.value == "S2"
    assert hallazgos[0].uncertain is True
    assert _VALOR not in _serializado(hallazgos[0])


def test_arbol_limpio_igual_inspecciona_ubicaciones(tmp_path):
    """0 hallazgos con 0 ubicaciones seria indistinguible de no haber corrido."""
    from tools.audit.checks.secrets import check_secrets

    raiz = _arbol(
        tmp_path,
        {
            "config/settings.py": _MODULO_LIMPIO,
            ".env.example": _ENV_EXAMPLE,
            ".github/workflows/morning.yml": _WORKFLOW,
        },
    )

    hallazgos, inspeccionadas = check_secrets(raiz)

    assert hallazgos == []
    assert inspeccionadas >= 3


# ---------------------------------------------------------------------------
# Contrato del registro
# ---------------------------------------------------------------------------

def test_checker_registrado(tmp_path):
    """Un checker no registrado deja su categoria vacia en silencio."""
    from tools.audit.checks.secrets import check_secrets  # noqa: F401
    from tools.audit.registry import get_check

    spec = get_check("secret-leakage")

    assert spec is not None
    assert spec.category.value == "secret-leakage"

    raiz = _arbol(tmp_path, {"src/utils/client.py": _MODULO_CON_LLAVE})
    hallazgos, inspeccionadas = spec.run(raiz)

    assert len(hallazgos) == 1
    assert inspeccionadas > 0


def test_ids_estables_entre_corridas(tmp_path):
    """El artefacto tiene que ser byte-identico sobre el mismo arbol."""
    raiz = _arbol(
        tmp_path,
        {
            "src/utils/client.py": _MODULO_CON_LLAVE,
            "config/settings.py": _MODULO_LIMPIO,
        },
    )

    primera = [h.id for h in _hallazgos_de(raiz)]
    segunda = [h.id for h in _hallazgos_de(raiz)]

    assert primera == segunda
    assert len(set(primera)) == len(primera)
