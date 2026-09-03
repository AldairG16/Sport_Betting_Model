"""Tests del checker de riesgo en el manejo de errores (T010).

Fijan tres contratos:

1. la clasificacion de un handler en `reraise` / `logged` / `silent`,
2. que un handler SILENCIOSO sobre una ruta de LLM, notificacion o escritura
   a la base sea S1 — y que el mismo handler LOGUEADO no lo sea, y
3. que un filtro que descarta filas nulas se reporte sin importar en que
   script viva.
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


def _handler(fuente):
    """Primer `ast.ExceptHandler` de un fragmento de codigo."""
    import ast

    return ast.parse(fuente).body[0].handlers[0]


def _hallazgos_de(raiz):
    """Corre el checker y devuelve solo la lista de hallazgos."""
    from tools.audit.checks.error_handling import check_error_handling

    hallazgos, _ = check_error_handling(raiz)
    return hallazgos


def _s1(hallazgos):
    """Solo los hallazgos de severidad S1."""
    return [h for h in hallazgos if h.severity.value == "S1"]


def _anclas(hallazgos):
    """Todas las anclas de todos los hallazgos, aplanadas."""
    return [ancla for h in hallazgos for ancla in h.anchors]


_LLM_SILENCIOSO = '''"""Analista que llama al LLM y se traga el fallo."""

import anthropic


def analizar(cliente, prompt):
    try:
        respuesta = cliente.messages.create(model="claude-haiku-4-5")
        return respuesta
    except Exception:
        pass
    return None
'''

_LLM_LOGUEADO = '''"""Mismo llamado al LLM, pero dejando rastro."""

import anthropic


def analizar(cliente, prompt):
    try:
        return cliente.messages.create(model="claude-haiku-4-5")
    except Exception as e:
        print(f"[ANALYST] fallo el LLM: {e}")
    return None
'''

_NOTIFY_SILENCIOSO = '''"""Envio a Telegram que se traga el fallo de entrega."""

import requests


def enviar(token, chat_id, texto):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
            timeout=10,
        )
    except Exception:
        pass
'''

_DB_SILENCIOSO = '''"""Escritura a la base que se traga el fallo."""


def guardar(conn, fila):
    try:
        conn.execute("INSERT INTO bets_history (market) VALUES (:m)", fila)
        conn.commit()
    except Exception:
        pass
'''

_CALCULO_SILENCIOSO = '''"""Handler silencioso fuera de toda ruta de riesgo."""


def ratio(a, b):
    try:
        return a / b
    except Exception:
        return 0.0
'''


# ---------------------------------------------------------------------------
# classify_handler
# ---------------------------------------------------------------------------

def test_classify_handler_pass_es_silent():
    """Un cuerpo que solo tiene `pass` traga la excepcion sin rastro."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler("try:\n    f()\nexcept Exception:\n    pass\n")
    assert classify_handler(handler) == "silent"


def test_classify_handler_ellipsis_es_silent():
    """`...` es tan silencioso como `pass`."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler("try:\n    f()\nexcept ValueError:\n    ...\n")
    assert classify_handler(handler) == "silent"


def test_classify_handler_print_es_logged():
    """Imprimir deja rastro: el fallo es observable."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler(
        "try:\n    f()\nexcept Exception as e:\n    print('fallo', e)\n"
    )
    assert classify_handler(handler) == "logged"


def test_classify_handler_logging_es_logged():
    """Un `logging.error(...)` tambien deja rastro."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler(
        "try:\n    f()\nexcept Exception:\n    logging.exception('boom')\n"
    )
    assert classify_handler(handler) == "logged"


def test_classify_handler_raise_es_reraise():
    """Volver a lanzar mantiene el fallo visible aguas arriba."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler("try:\n    f()\nexcept Exception:\n    raise\n")
    assert classify_handler(handler) == "reraise"


def test_classify_handler_raise_gana_sobre_log():
    """Si loguea Y relanza, el fallo sigue siendo visible: es `reraise`."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler(
        "try:\n    f()\nexcept Exception as e:\n"
        "    print(e)\n    raise RuntimeError('x') from e\n"
    )
    assert classify_handler(handler) == "reraise"


def test_classify_handler_def_anidado_no_cuenta_como_reraise():
    """Un `raise` dentro de un `def` anidado no se ejecuta al manejar el error."""
    from tools.audit.checks.error_handling import classify_handler

    handler = _handler(
        "try:\n    f()\nexcept Exception:\n"
        "    def luego():\n        raise ValueError('x')\n"
    )
    assert classify_handler(handler) == "silent"


# ---------------------------------------------------------------------------
# Handlers silenciosos sobre rutas de riesgo -> S1
# ---------------------------------------------------------------------------

def test_silencioso_en_ruta_llm_es_s1(tmp_path):
    """Tragarse un fallo del LLM apaga al analista sin que nadie lo note."""
    raiz = _arbol(tmp_path, {"scripts/analyst.py": _LLM_SILENCIOSO})

    criticos = _s1(_hallazgos_de(raiz))

    assert len(criticos) == 1
    assert criticos[0].category.value == "error-handling-risk"
    assert "scripts/analyst.py:10" in criticos[0].anchors


def test_silencioso_en_ruta_notify_es_s1_y_apunta_al_watchdog(tmp_path):
    """La salud de entrega se observa en runtime: el hallazgo cita al watchdog."""
    from tools.audit.checks.error_handling import RUNTIME_OWNER

    raiz = _arbol(tmp_path, {"scripts/notify.py": _NOTIFY_SILENCIOSO})

    criticos = _s1(_hallazgos_de(raiz))

    assert len(criticos) == 1
    assert criticos[0].runtime_owner == RUNTIME_OWNER
    assert RUNTIME_OWNER == "scripts/watchdog.py"


def test_silencioso_en_ruta_db_write_es_s1(tmp_path):
    """Un INSERT que falla en silencio reporta exito sin guardar la fila."""
    raiz = _arbol(tmp_path, {"src/models/save.py": _DB_SILENCIOSO})

    criticos = _s1(_hallazgos_de(raiz))

    assert len(criticos) == 1
    assert any("src/models/save.py" in a for a in criticos[0].anchors)


def test_logueado_en_la_misma_ruta_no_es_s1(tmp_path):
    """El mismo llamado al LLM, pero logueado, no es una perdida silenciosa."""
    raiz = _arbol(tmp_path, {"scripts/analyst.py": _LLM_LOGUEADO})

    assert _s1(_hallazgos_de(raiz)) == []


def test_silencioso_fuera_de_ruta_de_riesgo_no_es_s1(tmp_path):
    """Deuda tecnica, no perdida de dinero: se reporta pero no como S1."""
    raiz = _arbol(tmp_path, {"src/utils/calc.py": _CALCULO_SILENCIOSO})

    hallazgos = _hallazgos_de(raiz)

    assert _s1(hallazgos) == []
    assert [h.severity.value for h in hallazgos] == ["S3"]


def test_remediacion_propone_visibilidad_no_aborto(tmp_path):
    """Un handler load-bearing no se arregla tumbando el pipeline."""
    raiz = _arbol(tmp_path, {"scripts/notify.py": _NOTIFY_SILENCIOSO})

    remediacion = _s1(_hallazgos_de(raiz))[0].remediation.lower()

    assert "visibilidad" in remediacion
    assert "abortar" in remediacion


# ---------------------------------------------------------------------------
# Exclusion silenciosa de datos
# ---------------------------------------------------------------------------

def test_where_is_not_null_se_reporta(tmp_path):
    """Un filtro que descarta nulos enmascara un fetch fallido aguas arriba."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/report.py": (
                '"""Consulta de resultados."""\n\n'
                'SQL = "SELECT id FROM matches WHERE home_goals IS NOT NULL"\n'
            )
        },
    )

    hallazgos = _hallazgos_de(raiz)

    assert len(hallazgos) == 1
    assert "scripts/report.py:3" in hallazgos[0].anchors
    assert "home_goals" in hallazgos[0].impact


def test_where_is_not_null_se_reporta_en_cualquier_script(tmp_path):
    """El defecto no depende de en que archivo viva la consulta."""
    raiz = _arbol(
        tmp_path,
        {
            "src/models/clv.py": (
                '"""Otro consumidor."""\n\n'
                'SQL = "SELECT * FROM bets_history WHERE clv IS NOT NULL"\n'
            ),
            "queries/pendientes.sql": (
                "SELECT * FROM bets_history WHERE closing_odds IS NOT NULL;\n"
            ),
        },
    )

    rutas = {ancla.split(":")[0] for ancla in _anclas(_hallazgos_de(raiz))}

    assert rutas == {"src/models/clv.py", "queries/pendientes.sql"}


def test_exclusion_se_marca_incierta(tmp_path):
    """Cuantos nulos son legitimos solo se sabe mirando los datos."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/report.py": (
                '"""Consulta."""\n\n'
                'SQL = "SELECT id FROM matches WHERE corners IS NOT NULL"\n'
            )
        },
    )

    hallazgo = _hallazgos_de(raiz)[0]

    assert hallazgo.uncertain is True
    assert hallazgo.severity.value == "S2"


# ---------------------------------------------------------------------------
# Contrato del registro
# ---------------------------------------------------------------------------

def test_checker_registrado_y_con_contrato_de_retorno(tmp_path):
    """Un checker no registrado deja su categoria vacia en silencio."""
    from tools.audit.checks.error_handling import check_error_handling  # noqa: F401
    from tools.audit.registry import get_check

    spec = get_check("error-handling-risk")

    assert spec is not None
    assert spec.category.value == "error-handling-risk"

    raiz = _arbol(tmp_path, {"scripts/analyst.py": _LLM_SILENCIOSO})
    hallazgos, inspeccionadas = spec.run(raiz)

    assert hallazgos
    # "0 hallazgos, 0 ubicaciones" delata al checker que no corrio.
    assert inspeccionadas > 0


def test_ids_estables_entre_corridas(tmp_path):
    """El artefacto tiene que ser byte-identico sobre el mismo arbol."""
    raiz = _arbol(
        tmp_path,
        {
            "scripts/analyst.py": _LLM_SILENCIOSO,
            "scripts/notify.py": _NOTIFY_SILENCIOSO,
            "src/utils/calc.py": _CALCULO_SILENCIOSO,
        },
    )

    primera = [h.id for h in _hallazgos_de(raiz)]
    segunda = [h.id for h in _hallazgos_de(raiz)]

    assert primera == segunda
    assert len(set(primera)) == len(primera)
