"""
tests/test_settings_contract.py
===============================
Contrato de configuracion centralizada (T015).

Que se protege aqui:
  1. Toda constante que gatea comportamiento de produccion vive en
     config/settings.py y en ningun otro lado.
  2. Los defaults relocalizados NO cambiaron de valor: este batch mueve,
     no re-tunea. Un default distinto cambia cuantos creditos se gastan.
  3. Importar config.settings con las env vars ausentes devuelve los
     defaults documentados y NO levanta ValueError. Ese ValueError
     (int("") cuando un secret de GH Actions se expande a "") congelo
     upcoming_matches desde el 17-abr-26.
  4. Los workflows que pegan al fetch path pasan los mismos gates de
     creditos. weekly.yml corria sin ellos.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# Defaults congelados. Si un valor de aqui cambia, el cambio es
# deliberado y hay que justificarlo en el commit — no es un ajuste libre.
DEFAULTS_CONGELADOS = {
    "API_CREDITS_STOP_THRESHOLD": 500,
    "MAX_CREDITS_PER_RUN": 800,
    "MAX_ENRICHMENTS_PER_RUN": 40,
    "ENRICH_DAYS_AHEAD": 2,
    "ENRICH_CACHE_TTL_HOURS": 12,
}


def _asignaciones_env(fuente: str) -> dict:
    """Mapea nombre -> (helper, default) para cada `X = env_*(...)` de modulo."""
    import ast

    arbol = ast.parse(fuente)
    encontrado = {}
    for nodo in arbol.body:
        if not isinstance(nodo, ast.Assign):
            continue
        if not isinstance(nodo.value, ast.Call):
            continue
        func = nodo.value.func
        if not isinstance(func, ast.Name) or not func.id.startswith("env_"):
            continue
        if len(nodo.value.args) != 2:
            continue
        default = nodo.value.args[1]
        if not isinstance(default, ast.Constant):
            continue
        for destino in nodo.targets:
            if isinstance(destino, ast.Name):
                encontrado[destino.id] = (func.id, default.value)
    return encontrado


def test_settings_declara_los_umbrales_de_creditos(repo_root):
    """config/settings.py es el unico hogar de los umbrales de creditos."""
    fuente = (repo_root / "config" / "settings.py").read_text(encoding="utf-8")
    asignaciones = _asignaciones_env(fuente)

    for nombre in DEFAULTS_CONGELADOS:
        assert nombre in asignaciones, (
            f"{nombre} no se declara en config/settings.py — la centralizacion "
            f"de configuracion (FR-023) exige que viva ahi"
        )


def test_defaults_relocalizados_no_cambiaron(repo_root):
    """Los defaults se mueven tal cual: 500 / 800 / 40 / 2 / 12."""
    fuente = (repo_root / "config" / "settings.py").read_text(encoding="utf-8")
    asignaciones = _asignaciones_env(fuente)

    for nombre, esperado in DEFAULTS_CONGELADOS.items():
        helper, default = asignaciones[nombre]
        assert helper == "env_int", (
            f"{nombre} debe leerse con env_int, no con {helper}"
        )
        assert default == esperado, (
            f"{nombre} cambio de default: {default} != {esperado}. "
            f"Este batch relocaliza, no re-tunea."
        )


def test_helpers_env_devuelven_default_con_valor_vacio():
    """env_int/env_float/env_str absorben el "" de un secret sin configurar."""
    import os

    from config.settings import env_float, env_int, env_str

    os.environ["_CONTRATO_TMP"] = ""
    try:
        # Sin este contrato, int("") levanta ValueError y mata el pipeline
        # antes de escribir nada.
        assert env_int("_CONTRATO_TMP", 500) == 500
        assert env_float("_CONTRATO_TMP", 1.5) == 1.5
        assert env_str("_CONTRATO_TMP", "eu") == "eu"

        os.environ["_CONTRATO_TMP"] = "   "
        assert env_int("_CONTRATO_TMP", 800) == 800
        assert env_str("_CONTRATO_TMP", "eu") == "eu"

        os.environ["_CONTRATO_TMP"] = "no-es-un-numero"
        assert env_int("_CONTRATO_TMP", 40) == 40
        assert env_float("_CONTRATO_TMP", 0.25) == 0.25
    finally:
        os.environ.pop("_CONTRATO_TMP", None)

    assert env_int("_CONTRATO_VAR_QUE_NO_EXISTE", 7) == 7


def test_import_con_env_vars_ausentes_no_truena(repo_root):
    """Importar config.settings sin ninguna env var relevante da los defaults.

    Corre en un subproceso con un entorno minimo (solo lo que Windows
    necesita para arrancar python, mas DB_URL dummy) para que el .env o el
    entorno del desarrollador no contaminen el resultado.
    """
    import json
    import os
    import subprocess

    entorno = {
        var: os.environ[var]
        for var in (
            "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "COMSPEC",
            "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
        )
        if var in os.environ
    }
    # Unico valor inyectado: settings.py levanta RuntimeError sin DB_URL.
    # No es una credencial — no resuelve a ninguna base real.
    entorno["DB_URL"] = "postgresql+psycopg2://dummy:dummy@localhost/dummy"
    entorno["PYTHONIOENCODING"] = "utf-8"

    nombres = sorted(DEFAULTS_CONGELADOS)
    programa = (
        "import json, config.settings as s; "
        "print(json.dumps({n: getattr(s, n) for n in "
        + repr(nombres)
        + "}))"
    )

    proceso = subprocess.run(
        [sys.executable, "-c", programa],
        cwd=str(repo_root),
        env=entorno,
        # DEVNULL y no heredar: bajo la captura de pytest en Windows el stdin
        # del proceso padre es un handle invalido y DuplicateHandle revienta
        # con WinError 6 al correr la suite completa.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert proceso.returncode == 0, (
        "importar config.settings sin env vars fallo:\n" + proceso.stderr
    )
    assert "ValueError" not in proceso.stderr

    valores = json.loads(proceso.stdout.strip().splitlines()[-1])
    assert valores == DEFAULTS_CONGELADOS


def test_update_upcoming_matches_no_declara_constantes(repo_root):
    """El script importa los umbrales; no declara ninguno localmente."""
    ruta = repo_root / "scripts" / "update_upcoming_matches.py"
    fuente = ruta.read_text(encoding="utf-8")

    declaradas = _asignaciones_env(fuente)
    assert declaradas == {}, (
        f"scripts/update_upcoming_matches.py todavia declara "
        f"{sorted(declaradas)} — deben vivir en config/settings.py"
    )


def test_update_upcoming_matches_importa_los_umbrales(repo_root):
    """Los cinco nombres se importan explicitamente desde config.settings."""
    import ast

    ruta = repo_root / "scripts" / "update_upcoming_matches.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "config.settings":
            importados.update(alias.name for alias in nodo.names)

    faltantes = set(DEFAULTS_CONGELADOS) - importados
    assert not faltantes, (
        f"scripts/update_upcoming_matches.py no importa {sorted(faltantes)} "
        f"desde config.settings"
    )


def test_watchdog_lee_credenciales_con_env_str(repo_root):
    """watchdog.py no hace lecturas crudas del entorno para credenciales."""
    fuente = (repo_root / "scripts" / "watchdog.py").read_text(encoding="utf-8")

    lectura_cruda = "os.environ" + ".get"
    assert lectura_cruda not in fuente, (
        "scripts/watchdog.py todavia lee el entorno crudo; usar env_str "
        "de config.settings"
    )
    assert 'env_str("TELEGRAM_BOT_TOKEN", "")' in fuente
    assert 'env_str("TELEGRAM_CHAT_ID", "")' in fuente
    assert "from config.settings import env_str" in fuente


def test_weekly_yml_gatea_creditos_como_los_demas(repo_root):
    """weekly.yml pega al mismo fetch path: necesita los mismos gates."""
    fuente = (repo_root / ".github" / "workflows" / "weekly.yml").read_text(
        encoding="utf-8"
    )

    for nombre in ("API_CREDITS_STOP_THRESHOLD", "MAX_CREDITS_PER_RUN",
                   "MAX_ENRICHMENTS_PER_RUN"):
        esperado = f"{nombre}:"
        assert esperado in fuente, (
            f"weekly.yml no pasa {nombre} — el run del lunes correria con "
            f"defaults distintos a morning/closing/evening"
        )
        assert f"secrets.{nombre}" in fuente, (
            f"weekly.yml declara {nombre} pero no lo cablea a su secret"
        )
