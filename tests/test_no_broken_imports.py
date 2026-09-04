"""Puerta de regresion: ningun import del repositorio queda sin resolver (T016).

Este es el test que cierra el S1 de `broken-reference`. No reimplementa un
tercer parser de imports: reutiliza `tools/audit/graph.py` y el checker
`check_broken_references`, que ya resuelven nombres punteados con `ast` sin
importar jamas el modulo auditado (importar `config.settings` sin `DB_URL`
lanza RuntimeError en tiempo de import).

QUE CUENTA COMO REFERENCIA ROTA
-------------------------------
Un import interno esta roto cuando su destino **no existe en el arbol de
trabajo ni en ninguna revision del repositorio**. Ese fue exactamente el caso
de `load_10_seasons.py`: importaba `src.data.collector`, un modulo que nunca
fue commiteado, asi que el script era ImportError garantizado desde el dia uno.

La tolerancia hacia la historia de git existe por una razon concreta y no
afloja la puerta para el defecto que importa: un checkout parcial (un worktree
que solo materializa una parte del arbol) deja modulos de produccion ausentes
del disco aunque el repositorio los conozca perfectamente, y reportarlos como
referencias rotas seria un falso positivo del checkout, no un defecto del
codigo. Un import a un nombre que nunca existio sigue fallando el test — que
es el caso que revienta un cron en produccion.

`archive/` queda fuera del recorrido (`EXCLUDED_DIRS` en
`tools/audit/graph.py`), de modo que el codigo retirado no puede fallar la
puerta. A cambio, todo `.py` archivado debe tener su fila de disposicion en
`archive/ARCHIVE.md`, y eso tambien se verifica aqui.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive"
REGISTRO = ARCHIVE_DIR / "ARCHIVE.md"

COLUMNAS_REGISTRO = (
    "Module",
    "Archived on",
    "Reason",
    "Disposition",
    "Safe to re-run?",
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _escribir(raiz: Path, relativa: str, contenido: str) -> Path:
    """Crea un archivo (con sus directorios) dentro de un arbol sintetico."""
    ruta = raiz / relativa
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _rutas_candidatas(destino: str) -> list[str]:
    """Archivos que podrian materializar un nombre punteado de modulo."""
    partes = destino.split(".")
    base = "/".join(partes)
    return [f"{base}.py", f"{base}/__init__.py"]


def _conocido_por_git(destino: str) -> bool:
    """El modulo existe en alguna revision del repositorio.

    Devuelve False cuando git no esta disponible: ante la duda la puerta se
    endurece, nunca se afloja.
    """
    for candidato in _rutas_candidatas(destino):
        try:
            salida = subprocess.run(
                ["git", "log", "--all", "--oneline", "-1", "--", candidato],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if salida.returncode == 0 and salida.stdout.strip():
            return True
    return False


def _filas_registro() -> list[dict[str, str]]:
    """Filas de la tabla de `archive/ARCHIVE.md` como dicts por columna."""
    filas: list[dict[str, str]] = []
    encabezado: list[str] | None = None
    for linea in REGISTRO.read_text(encoding="utf-8").splitlines():
        recortada = linea.strip()
        if not recortada.startswith("|"):
            encabezado = None
            continue
        celdas = [c.strip() for c in recortada.strip("|").split("|")]
        if encabezado is None:
            encabezado = celdas
            continue
        if all(set(c) <= {"-", ":"} and c for c in celdas):
            # Linea separadora de la tabla markdown.
            continue
        if len(celdas) == len(encabezado):
            filas.append(dict(zip(encabezado, celdas)))
    return filas


# ---------------------------------------------------------------------------
# La puerta contra el repositorio real
# ---------------------------------------------------------------------------

def test_ningun_import_del_repositorio_queda_sin_resolver():
    """Ningun modulo importa un destino que nunca existio en el repositorio."""
    from tools.audit.checks.references import check_broken_references

    hallazgos, inspeccionados = check_broken_references(REPO_ROOT)
    assert inspeccionados > 0, "el walk no inspecciono ningun import"

    rotos = []
    for hallazgo in hallazgos:
        evidencia = hallazgo.evidence[0]
        if evidencia.key == "syntax":
            continue
        if _conocido_por_git(evidencia.key):
            # Ausente de este checkout, pero presente en el repositorio.
            continue
        rotos.append(f"{evidencia.path}:{evidencia.line} -> {evidencia.key}")

    assert not rotos, (
        "imports que no resuelven a ningun modulo existente ni historico:\n  "
        + "\n  ".join(sorted(rotos))
        + "\nCrear el modulo destino, apuntar el import al real, o retirar el "
          "archivo a archive/ con su fila en archive/ARCHIVE.md."
    )


def test_ningun_archivo_del_repositorio_falla_al_parsear():
    """Un .py que no parsea revienta a todo el que lo importe."""
    from tools.audit.checks.references import check_broken_references

    hallazgos, _ = check_broken_references(REPO_ROOT)
    con_sintaxis_rota = [
        h.evidence[0].path for h in hallazgos if h.evidence[0].key == "syntax"
    ]
    assert not con_sintaxis_rota, (
        "archivos que no parsean: " + ", ".join(sorted(con_sintaxis_rota))
    )


def test_el_grafo_cubre_todos_los_modulos_indexados():
    """Cada modulo del indice aparece en el grafo: el walk no salto ninguno."""
    from tools.audit.graph import build_import_graph, build_module_index

    indice = build_module_index(REPO_ROOT)
    grafo = build_import_graph(REPO_ROOT)

    assert indice, "el indice de modulos salio vacio"
    assert set(grafo) == set(indice)
    # Toda arista apunta a un modulo real del indice, no a un nombre inventado.
    for origen, destinos in grafo.items():
        for destino in destinos:
            assert destino in indice, f"{origen} -> {destino} no esta indexado"


# ---------------------------------------------------------------------------
# archive/ queda fuera del recorrido
# ---------------------------------------------------------------------------

def test_archive_esta_excluido_del_recorrido():
    """El codigo retirado no puede fallar la puerta de imports."""
    from tools.audit.graph import EXCLUDED_DIRS, build_module_index

    assert "archive" in EXCLUDED_DIRS

    indice = build_module_index(REPO_ROOT)
    archivados = [
        nombre for nombre in indice if nombre == "archive" or nombre.startswith("archive.")
    ]
    assert not archivados, f"modulos archivados dentro del indice: {archivados}"


def test_un_import_roto_si_falla_la_puerta(tmp_path):
    """La puerta no es vacua: un destino inexistente se reporta con file:line."""
    from tools.audit.checks.references import check_broken_references

    _escribir(tmp_path, "src/data/queries.py", "VALOR = 1\n")
    _escribir(
        tmp_path,
        "load_10_seasons.py",
        "from src.data.collector import download_season\n",
    )

    hallazgos, _ = check_broken_references(tmp_path)
    rotos = {(h.evidence[0].path, h.evidence[0].key) for h in hallazgos}
    assert ("load_10_seasons.py", "src.data.collector") in rotos


def test_el_mismo_modulo_bajo_archive_no_falla_la_puerta(tmp_path):
    """Archivar es una disposicion valida: el archivo retirado deja de contar."""
    from tools.audit.checks.references import check_broken_references

    _escribir(tmp_path, "src/data/queries.py", "VALOR = 1\n")
    _escribir(
        tmp_path,
        "archive/load_10_seasons.py",
        "from src.data.collector import download_season\n",
    )

    hallazgos, _ = check_broken_references(tmp_path)
    assert not hallazgos, [h.evidence[0].path for h in hallazgos]


# ---------------------------------------------------------------------------
# El registro de disposiciones
# ---------------------------------------------------------------------------

def test_el_registro_de_archive_existe_con_sus_columnas():
    """archive/ARCHIVE.md es la tabla que rinde cuentas por lo retirado."""
    assert REGISTRO.is_file(), "falta archive/ARCHIVE.md"

    filas = _filas_registro()
    assert filas, "el registro no tiene ninguna fila"
    for columna in COLUMNAS_REGISTRO:
        assert columna in filas[0], f"falta la columna {columna!r}"


def test_cada_modulo_archivado_tiene_su_fila():
    """Un archivo depositado en archive/ sin disposicion rompe la suite."""
    if not ARCHIVE_DIR.is_dir():
        return

    filas = _filas_registro()
    registrados = {fila["Module"].strip("`") for fila in filas}

    for ruta in sorted(ARCHIVE_DIR.rglob("*.py")):
        nombre = ruta.name
        assert any(nombre == r or r.endswith(nombre) for r in registrados), (
            f"{ruta.relative_to(REPO_ROOT).as_posix()} esta archivado sin fila "
            f"en archive/ARCHIVE.md"
        )


def test_cada_fila_responde_si_es_seguro_reejecutar():
    """Ninguna celda vacia: una disposicion sin motivo es un borrado disfrazado."""
    for fila in _filas_registro():
        for columna in COLUMNAS_REGISTRO:
            valor = fila[columna].strip()
            assert valor, (
                f"la fila {fila['Module']!r} tiene vacia la columna {columna!r}"
            )


def test_load_10_seasons_esta_archivado_y_documentado():
    """El S1 concreto quedo cerrado: fuera de la raiz y con su disposicion."""
    assert not (REPO_ROOT / "load_10_seasons.py").exists(), (
        "load_10_seasons.py sigue en la raiz: su import de src.data.collector "
        "es una referencia rota permanente"
    )
    assert (ARCHIVE_DIR / "load_10_seasons.py").is_file()

    filas = {fila["Module"].strip("`"): fila for fila in _filas_registro()}
    assert "load_10_seasons.py" in filas
    respuesta = filas["load_10_seasons.py"]["Safe to re-run?"].lower()
    # "nunca corrio" y "one-shot ya aplicado" son respuestas distintas y la
    # fila tiene que elegir una, no quedarse en un "no" ambiguo.
    assert "nunca corrio" in respuesta or "one-shot" in respuesta
