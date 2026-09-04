"""Tests de sincronia entre `docs/schema.md` y el esquema real (T018).

El contrato que se fija aqui es el mismo que ya rige el inventario de scripts:
el documento NO es una lista redactada a mano que alguien recuerda actualizar,
sino una proyeccion del arbol de fuentes. La fuente de verdad es
`tools.audit.checks.schema.table_inventory()`, el mismo inventario que usa el
checker de deriva de esquema (T012). Si manana aparece un `CREATE TABLE` nuevo
en cualquier modulo, estos tests fallan hasta que el documento lo recoja.

QUE SE COMPARA Y QUE NO
-----------------------
`table_inventory` escanea el arbol completo con expresiones regulares sobre el
texto, porque el SQL vive dentro de literales de Python. Eso significa que
tambien ve:

- el SQL *sintetico* de `tests/` (los fixtures de `test_audit_schema.py`
  incluyen cosas como `ALTER TABLE t ...` a proposito, para ejercitar el
  scanner), y
- las expresiones regulares y la prosa del propio auditor en `tools/`, que
  mencionan `CREATE TABLE` y `to_sql` como patrones, no como esquema.

Ninguna de las dos es esquema de produccion. `tools/` en particular no puede
contener DDL real por diseno: es un analizador estatico que jamas abre una
conexion a PostgreSQL (FR-006). Por eso el conjunto autoritativo se restringe
a los sitios de produccion -- ver `_es_produccion`. La alternativa (documentar
una tabla llamada `t`) haria el documento falso, que es justo lo contrario de
lo que pide la tarea.

DIRECCIONES DEL CONTRATO
------------------------
1. Cobertura: toda tabla con DDL de produccion tiene seccion, exactamente una.
2. Respaldo: toda tabla documentada tiene DDL de produccion **o** una lectura
   / escritura de produccion. No se exige lo inverso (documentar toda tabla
   *referenciada*) porque el escaneo textual produce falsos positivos sobre
   prosa en espanol -- `FROM` sirve a Python y a SQL por igual.
3. Trazabilidad: toda seccion declara donde se define, con `archivo:linea`, y
   esas citas apuntan a lineas que existen de verdad.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DOC_RELPATH = "docs/schema.md"

# Prefijos cuyo "DDL" no es esquema de produccion (ver docstring del modulo).
NON_PRODUCTION_PREFIXES = ("tests/", "tools/")

# Un encabezado `## `nombre_de_tabla`` abre la seccion de una tabla.
_ENCABEZADO = re.compile(r"^##\s+`([a-z_][a-z0-9_]*)`\s*$", re.M)

# Marcador del bloque de trazabilidad dentro de cada seccion.
_DEFINIDA_EN = re.compile(r"^\*\*Definida en\*\*\s*$", re.M)

# Una cita es `ruta.py:123` o `ruta.sql:123` entre backticks.
_CITA = re.compile(r"`([^`\s]+\.(?:py|sql)):(\d+)`")


# ---------------------------------------------------------------------------
# Helpers de lectura del documento
# ---------------------------------------------------------------------------

def _documento(raiz):
    """Texto crudo de docs/schema.md."""
    ruta = Path(raiz) / DOC_RELPATH
    assert ruta.is_file(), f"Falta {DOC_RELPATH}"
    return ruta.read_text(encoding="utf-8")


def _secciones(texto):
    """[(tabla, cuerpo)] en orden de aparicion, conservando duplicados.

    Se devuelve una lista y no un dict a proposito: un dict silenciaria una
    tabla documentada dos veces, que es justo uno de los defectos a detectar.
    """
    partes = _ENCABEZADO.split(texto)
    # partes[0] es el preambulo; despues alternan (nombre, cuerpo).
    return [(partes[i], partes[i + 1]) for i in range(1, len(partes), 2)]


def _citas_de_definicion(cuerpo):
    """Citas `archivo:linea` que cuelgan del bloque **Definida en**."""
    marcador = _DEFINIDA_EN.search(cuerpo)
    if marcador is None:
        return []
    bloque = cuerpo[marcador.end():]
    # El bloque termina en el siguiente encabezado si lo hay.
    corte = re.search(r"^#{2,3}\s", bloque, re.M)
    if corte is not None:
        bloque = bloque[: corte.start()]
    return [(ruta, int(linea)) for ruta, linea in _CITA.findall(bloque)]


# ---------------------------------------------------------------------------
# Helpers de lectura del codigo (la fuente de verdad)
# ---------------------------------------------------------------------------

def _es_produccion(ruta):
    """True si la ruta relativa puede contener esquema real."""
    return not ruta.startswith(NON_PRODUCTION_PREFIXES)


def _tablas_con_ddl_de_produccion(raiz):
    """Tablas cuyo DDL vive fuera de tests/ y tools/."""
    from tools.audit.checks.schema import table_inventory

    return {
        tabla
        for tabla, sitios in table_inventory(Path(raiz)).items()
        if any(_es_produccion(sitio.path) for sitio in sitios)
    }


def _tablas_referenciadas_en_produccion(raiz):
    """Tablas que el codigo de produccion lee o escribe."""
    from tools.audit.checks.schema import referenced_tables

    return {
        tabla
        for tabla, evidencias in referenced_tables(Path(raiz)).items()
        if any(_es_produccion(evidencia.path) for evidencia in evidencias)
    }


# ---------------------------------------------------------------------------
# 1. Cobertura
# ---------------------------------------------------------------------------

def test_toda_tabla_con_ddl_de_produccion_esta_documentada(repo_root):
    """Ninguna tabla definida en el codigo puede faltar en el documento."""
    documentadas = {tabla for tabla, _ in _secciones(_documento(repo_root))}
    en_codigo = _tablas_con_ddl_de_produccion(repo_root)

    faltantes = sorted(en_codigo - documentadas)
    assert not faltantes, (
        f"Tablas con DDL en el codigo y sin seccion en {DOC_RELPATH}: "
        f"{faltantes}"
    )


def test_ninguna_tabla_se_documenta_dos_veces(repo_root):
    """Dos secciones para la misma tabla = dos verdades que pueden divergir."""
    nombres = [tabla for tabla, _ in _secciones(_documento(repo_root))]
    duplicadas = sorted({n for n in nombres if nombres.count(n) > 1})

    assert not duplicadas, f"Tablas documentadas mas de una vez: {duplicadas}"


def test_el_documento_no_esta_vacio(repo_root):
    """Un documento sin secciones pasaria por vacuidad los tests de cobertura."""
    assert _secciones(_documento(repo_root)), (
        f"{DOC_RELPATH} no declara ninguna tabla"
    )


# ---------------------------------------------------------------------------
# 2. Respaldo -- nada inventado
# ---------------------------------------------------------------------------

def test_toda_tabla_documentada_existe_en_el_codigo(repo_root):
    """Documentar una tabla que nadie define ni consulta es ficcion."""
    documentadas = {tabla for tabla, _ in _secciones(_documento(repo_root))}
    respaldadas = _tablas_con_ddl_de_produccion(
        repo_root
    ) | _tablas_referenciadas_en_produccion(repo_root)

    inventadas = sorted(documentadas - respaldadas)
    assert not inventadas, (
        f"Tablas documentadas sin DDL ni lectura/escritura en el codigo: "
        f"{inventadas}"
    )


# ---------------------------------------------------------------------------
# 3. Trazabilidad
# ---------------------------------------------------------------------------

def test_cada_seccion_declara_donde_se_define(repo_root):
    """Toda tabla trae un bloque **Definida en** con al menos una cita."""
    sin_citas = [
        tabla
        for tabla, cuerpo in _secciones(_documento(repo_root))
        if not _citas_de_definicion(cuerpo)
    ]

    assert not sin_citas, (
        "Secciones sin bloque **Definida en** con citas archivo:linea: "
        f"{sin_citas}"
    )


def test_las_citas_apuntan_a_lineas_que_existen(repo_root):
    """Una cita al vacio es peor que no citar: miente con precision."""
    raiz = Path(repo_root)
    rotas = []

    for tabla, cuerpo in _secciones(_documento(repo_root)):
        for ruta, linea in _citas_de_definicion(cuerpo):
            archivo = raiz / ruta
            if not archivo.is_file():
                rotas.append(f"{tabla}: no existe {ruta}")
                continue
            total = len(archivo.read_text(encoding="utf-8").splitlines())
            if not 1 <= linea <= total:
                rotas.append(
                    f"{tabla}: {ruta}:{linea} fuera de rango (1..{total})"
                )

    assert not rotas, f"Citas rotas en {DOC_RELPATH}: {rotas}"


# ---------------------------------------------------------------------------
# 4. El contrato muerde de verdad
# ---------------------------------------------------------------------------

def test_una_tabla_nueva_sin_documentar_rompe_la_cobertura(tmp_path):
    """Prueba de que la cobertura no pasa por vacuidad: arbol sintetico."""
    fuente = tmp_path / "scripts"
    fuente.mkdir()
    (fuente / "nueva_migracion.py").write_text(
        'SQL = "CREATE TABLE IF NOT EXISTS tabla_recien_nacida (id INT)"\n',
        encoding="utf-8",
    )

    detectadas = _tablas_con_ddl_de_produccion(tmp_path)

    # Con un documento sin esa seccion, la diferencia es no vacia y el test
    # de cobertura fallaria -- que es exactamente el comportamiento deseado.
    assert "tabla_recien_nacida" in detectadas
    assert sorted(detectadas - set()) == ["tabla_recien_nacida"]


def test_el_ddl_de_tests_y_tools_no_cuenta_como_produccion(tmp_path):
    """El SQL sintetico de fixtures no obliga a documentar tablas falsas."""
    for carpeta, nombre in (
        ("tests", "tabla_de_fixture"),
        ("tools", "tabla_de_regex"),
    ):
        destino = tmp_path / carpeta
        destino.mkdir()
        (destino / "modulo.py").write_text(
            f'SQL = "CREATE TABLE {nombre} (id INT)"\n', encoding="utf-8"
        )

    detectadas = _tablas_con_ddl_de_produccion(tmp_path)

    assert "tabla_de_fixture" not in detectadas
    assert "tabla_de_regex" not in detectadas
