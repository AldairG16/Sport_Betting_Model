"""Checker de deriva de esquema: donde se define cada tabla y quien la parte.

El DDL de este proyecto vive disperso en tres formas distintas:

1. `CREATE TABLE` explicitos embebidos en literales de Python o en .sql,
2. migraciones perezosas `ALTER TABLE` con `ADD COLUMN IF NOT EXISTS` que se
   ejecutan al vuelo desde el modulo que necesita la columna, y
3. creaciones implicitas via `DataFrame.to_sql(...)`, que fabrican la tabla
   (y adivinan los tipos) sin ningun DDL escrito en ningun lado.

Las tres tienen el mismo efecto sobre la base, pero solo la primera es
legible. Este modulo las inventaria juntas y levanta tres defectos:

- una tabla cuyas columnas se definen desde MAS DE UN modulo (nadie es dueno
  del esquema, y dos migraciones perezosas pueden contradecirse),
- una tabla que el codigo lee o escribe pero que NO tiene ninguna sentencia
  de creacion explicita (su forma real solo existe en produccion), y
- una alteracion NO aditiva (rename, cambio de tipo, NOT NULL, drop) que
  jamas debe aplicarse sola: contra ~79K filas historicas puede perder datos
  o romper lectores en caliente, asi que se marca para aprobacion explicita.

Analisis 100% estatico: se lee el arbol de fuentes y se aplican expresiones
regulares sobre el texto. Nunca se abre una conexion a PostgreSQL, ni
siquiera de solo lectura (FR-006).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import EXCLUDED_DIRS
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.registry import check
from tools.audit.sqlscan import SCANNED_SUFFIXES, DDLSite, scan_ddl

__all__ = [
    "ADDITIVE_OPERATIONS",
    "NON_ADDITIVE_OPERATIONS",
    "SchemaChange",
    "check_schema",
    "classify_alteration",
    "referenced_tables",
    "scan_schema_changes",
    "table_inventory",
]

# ---------------------------------------------------------------------------
# Vocabulario de operaciones
# ---------------------------------------------------------------------------

# Aditivo = agrega superficie sin tocar lo que ya existe. Es reversible y no
# puede corromper filas historicas, asi que puede proponerse como aplicable.
ADDITIVE_OPERATIONS = frozenset(
    {"add_column", "add_constraint", "create_index"}
)

# No aditivo = reescribe o destruye estructura existente. Contra la tabla
# `matches` (~79K filas) esto no se propone jamas de forma automatica.
NON_ADDITIVE_OPERATIONS = frozenset(
    {"drop_column", "rename", "type_change", "not_null"}
)

_OPERACIONES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("add_column", re.compile(r"\bADD\s+(?:COLUMN\b|IF\s+NOT\s+EXISTS\b)", re.I)),
    ("add_constraint", re.compile(r"\bADD\s+CONSTRAINT\b", re.I)),
    ("create_index", re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I)),
    ("drop_column", re.compile(r"\bDROP\s+COLUMN\b", re.I)),
    ("rename", re.compile(r"\bRENAME\b", re.I)),
    (
        "type_change",
        re.compile(
            r"\bALTER\s+(?:COLUMN\s+)?\w+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I
        ),
    ),
    ("not_null", re.compile(r"\bSET\s+NOT\s+NULL\b", re.I)),
)

# Severidad por operacion no aditiva. Perder una columna o renombrarla rompe
# lectores en produccion de inmediato; un cambio de tipo o un NOT NULL falla
# al aplicarse pero no borra nada.
_SEVERIDAD_NO_ADITIVA: dict[str, Severity] = {
    "drop_column": Severity.S1,
    "rename": Severity.S1,
    "type_change": Severity.S2,
    "not_null": Severity.S2,
}

# ---------------------------------------------------------------------------
# Referencias a tablas (lecturas y escrituras)
# ---------------------------------------------------------------------------

_IDENT = (
    r"[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)[\"'`\]]?"
)

_REFERENCIAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("select", re.compile(r"\bFROM\s+" + _IDENT, re.I)),
    ("join", re.compile(r"\bJOIN\s+" + _IDENT, re.I)),
    ("insert", re.compile(r"\bINSERT\s+INTO\s+" + _IDENT, re.I)),
    ("update", re.compile(r"\bUPDATE\s+" + _IDENT, re.I)),
    ("to_sql", re.compile(r"\.to_sql\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")),
)

# `from x import y` no es una lectura de tabla. Se descarta por linea completa
# porque el mismo `FROM` sirve a Python y a SQL.
_IMPORT_PY = re.compile(r"^\s*(?:from\s+[A-Za-z_][\w.]*\s+import\b|import\s)")

# Palabras que aparecen tras FROM/JOIN/UPDATE sin ser nombres de tabla.
_NO_ES_TABLA = frozenset(
    {
        "select",
        "values",
        "lateral",
        "unnest",
        "generate_series",
        "dual",
        "only",
        "table",
        "where",
        "set",
        "as",
        "the",
        "la",
        "el",
        "los",
        "las",
        "una",
        "un",
        "de",
        "que",
    }
)


@dataclass(frozen=True)
class SchemaChange:
    """Una alteracion concreta del esquema, clasificada por reversibilidad.

    `auto_applicable` es el campo que importa: una propuesta no aditiva NUNCA
    sale con `auto_applicable=True`, sin importar cuan obvia parezca.
    """

    path: str
    line: int
    table: str
    operation: str
    additive: bool
    auto_applicable: bool
    approval: str  # 'auto' | 'explicit-approval'
    statement: str = ""


def classify_alteration(statement: str) -> tuple[str, ...]:
    """Operaciones detectadas en una sentencia, ordenadas alfabeticamente."""
    encontradas = {
        nombre for nombre, patron in _OPERACIONES if patron.search(statement)
    }
    return tuple(sorted(encontradas))


def _normaliza_tabla(nombre: str) -> str:
    """`public.bets_history` -> `bets_history`. El esquema no distingue."""
    return nombre.rsplit(".", 1)[-1].lower()


def _archivos(raiz: Path) -> list[Path]:
    """Archivos escaneables del arbol, ordenados y sin directorios de ruido."""
    encontrados: list[Path] = []
    for ruta in raiz.rglob("*"):
        if not ruta.is_file() or ruta.suffix not in SCANNED_SUFFIXES:
            continue
        partes = ruta.relative_to(raiz).parts
        if any(parte in EXCLUDED_DIRS for parte in partes[:-1]):
            continue
        encontrados.append(ruta)
    return sorted(encontrados, key=lambda p: p.relative_to(raiz).as_posix())


def _leer_arbol(raiz: Path) -> dict[str, str]:
    """Cache ruta-relativa -> texto. Se lee una sola vez por corrida."""
    textos: dict[str, str] = {}
    for ruta in _archivos(raiz):
        relativa = ruta.relative_to(raiz).as_posix()
        textos[relativa] = ruta.read_text(encoding="utf-8", errors="replace")
    return textos


def _linea_de(texto: str, pos: int) -> int:
    """Numero de linea (1-based) de un offset dentro del texto."""
    return texto.count("\n", 0, pos) + 1


def _sentencia(texto: str, linea: int, max_lineas: int = 4) -> str:
    """Fragmento de sentencia que arranca en `linea` y corta en el primer ';'.

    Se toma desde el inicio de la linea porque el DDL suele venir indentado
    dentro de un literal multilinea, y el `ADD COLUMN` puede caer en la
    siguiente linea del mismo string.
    """
    lineas = texto.splitlines()
    inicio = max(0, linea - 1)
    fragmento = "\n".join(lineas[inicio : inicio + max_lineas])
    corte = fragmento.find(";")
    return fragmento if corte == -1 else fragmento[: corte + 1]


def table_inventory(root: Path) -> dict[str, list[DDLSite]]:
    """Tabla -> todos los sitios que la definen o la mutan.

    Agrupa la salida de `scan_ddl` normalizando el esquema, de modo que
    `public.matches` y `matches` sean la misma tabla. Las listas quedan
    ordenadas por (ruta, linea, tipo) para que el artefacto sea byte-estable.
    """
    inventario: dict[str, list[DDLSite]] = {}
    for sitio in scan_ddl(Path(root)):
        inventario.setdefault(_normaliza_tabla(sitio.table), []).append(sitio)
    for sitios in inventario.values():
        sitios.sort(key=lambda s: (s.path, s.line, s.kind))
    return dict(sorted(inventario.items()))


def referenced_tables(root: Path) -> dict[str, list[Evidence]]:
    """Tabla -> ubicaciones donde el codigo la lee o la escribe.

    Escaneo textual: el SQL vive dentro de literales, asi que `ast` no lo ve.
    Los imports de Python se descartan por linea para que `from datetime
    import ...` no invente una tabla llamada `datetime`.
    """
    textos = _leer_arbol(Path(root))
    return _referencias_desde_textos(textos)


def _referencias_desde_textos(
    textos: dict[str, str],
) -> dict[str, list[Evidence]]:
    referencias: dict[str, set[Evidence]] = {}

    for relativa, texto in sorted(textos.items()):
        lineas = texto.splitlines()
        for verbo, patron in _REFERENCIAS:
            for coincidencia in patron.finditer(texto):
                numero = _linea_de(texto, coincidencia.start())
                cruda = lineas[numero - 1] if numero <= len(lineas) else ""
                if _IMPORT_PY.match(cruda):
                    continue
                tabla = _normaliza_tabla(coincidencia.group(1))
                if tabla in _NO_ES_TABLA:
                    continue
                referencias.setdefault(tabla, set()).add(
                    Evidence(
                        path=relativa,
                        line=numero,
                        key=tabla,
                        quote=f"{verbo} {tabla}",
                    )
                )

    return {
        tabla: sorted(evidencias, key=lambda e: (e.path, e.line or 0, e.quote))
        for tabla, evidencias in sorted(referencias.items())
    }


def scan_schema_changes(root: Path) -> list[SchemaChange]:
    """Toda alteracion de esquema del arbol, clasificada por reversibilidad."""
    raiz = Path(root)
    textos = _leer_arbol(raiz)
    return _cambios_desde(scan_ddl(raiz), textos)


def _cambios_desde(
    sitios: list[DDLSite], textos: dict[str, str]
) -> list[SchemaChange]:
    cambios: list[SchemaChange] = []
    for sitio in sitios:
        if sitio.kind != "alter":
            continue
        sentencia = _sentencia(textos.get(sitio.path, ""), sitio.line)
        for operacion in classify_alteration(sentencia):
            aditiva = operacion in ADDITIVE_OPERATIONS
            cambios.append(
                SchemaChange(
                    path=sitio.path,
                    line=sitio.line,
                    table=_normaliza_tabla(sitio.table),
                    operation=operacion,
                    additive=aditiva,
                    # Solo lo aditivo puede proponerse como aplicable.
                    auto_applicable=aditiva,
                    approval="auto" if aditiva else "explicit-approval",
                    statement=" ".join(sentencia.split())[:200],
                )
            )
    return sorted(
        cambios, key=lambda c: (c.path, c.line, c.table, c.operation)
    )


def _define_columnas(sitio: DDLSite, textos: dict[str, str]) -> bool:
    """True si el sitio define o redefine columnas de la tabla.

    Una creacion explicita y un `to_sql` siempre lo hacen. Un `ALTER TABLE`
    solo cuenta si toca una columna: agregar una constraint o un indice no
    parte la propiedad del esquema.
    """
    if sitio.kind in ("create", "to_sql"):
        return True
    sentencia = _sentencia(textos.get(sitio.path, ""), sitio.line)
    return "column" in sentencia.lower()


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

@check("schema-drift", Category.SCHEMA_DRIFT)
def check_schema(root: Path) -> tuple[list[Finding], int]:
    """Inventaria el DDL disperso y reporta los tres defectos de esquema."""
    raiz = Path(root)
    textos = _leer_arbol(raiz)
    sitios = scan_ddl(raiz)

    inventario: dict[str, list[DDLSite]] = {}
    for sitio in sitios:
        inventario.setdefault(_normaliza_tabla(sitio.table), []).append(sitio)
    for lista in inventario.values():
        lista.sort(key=lambda s: (s.path, s.line, s.kind))

    referencias = _referencias_desde_textos(textos)
    cambios = _cambios_desde(sitios, textos)

    hallazgos: list[Finding] = []
    hallazgos.extend(_hallazgos_multi_modulo(inventario, textos))
    hallazgos.extend(_hallazgos_sin_create(inventario, referencias))
    hallazgos.extend(_hallazgos_no_aditivos(cambios))

    # Ubicaciones inspeccionadas: archivos leidos + sitios DDL + referencias.
    # Nunca es 0 sobre un arbol con fuentes, asi que "0 hallazgos" no puede
    # confundirse con "el checker no corrio".
    inspeccionadas = (
        len(textos)
        + len(sitios)
        + sum(len(evidencias) for evidencias in referencias.values())
    )

    hallazgos.sort(key=lambda f: (f.severity.value, f.id))
    return hallazgos, inspeccionadas


def _hallazgos_multi_modulo(
    inventario: dict[str, list[DDLSite]], textos: dict[str, str]
) -> list[Finding]:
    """Tablas cuyas columnas se definen desde mas de un modulo."""
    hallazgos: list[Finding] = []

    for tabla, sitios in sorted(inventario.items()):
        columnas = [s for s in sitios if _define_columnas(s, textos)]
        modulos = sorted({s.path for s in columnas})
        if len(modulos) < 2:
            continue

        evidencia = [
            Evidence(
                path=s.path,
                line=s.line,
                key=tabla,
                quote=f"{s.kind} {tabla}",
            )
            for s in sorted(columnas, key=lambda s: (s.path, s.line, s.kind))
        ]
        hallazgos.append(
            Finding(
                category=Category.SCHEMA_DRIFT,
                severity=Severity.S2,
                evidence=evidencia,
                impact=(
                    f"Las columnas de `{tabla}` se definen desde "
                    f"{len(modulos)} modulos ({', '.join(modulos)}). Ningun "
                    "archivo describe la forma real de la tabla, y dos "
                    "migraciones perezosas pueden contradecirse sin que nadie "
                    "lo note hasta que una consulta falle en produccion."
                ),
                remediation=(
                    f"Declarar `{tabla}` en un unico sitio y dejar que los "
                    "demas modulos lo referencien; documentar la tabla en "
                    "docs/schema.md como fuente de verdad."
                ),
                effort="M",
            )
        )

    return hallazgos


def _hallazgos_sin_create(
    inventario: dict[str, list[DDLSite]],
    referencias: dict[str, list[Evidence]],
) -> list[Finding]:
    """Tablas que el codigo usa pero que nadie declara explicitamente."""
    hallazgos: list[Finding] = []
    tablas = sorted(set(inventario) | set(referencias))

    for tabla in tablas:
        sitios = inventario.get(tabla, [])
        if any(s.kind == "create" for s in sitios):
            continue

        evidencia = [
            Evidence(
                path=s.path,
                line=s.line,
                key=tabla,
                quote=f"sin creacion explicita: {s.kind} {tabla}",
            )
            for s in sitios
        ]
        evidencia.extend(
            Evidence(
                path=e.path,
                line=e.line,
                key=tabla,
                quote=f"sin creacion explicita: {e.quote}",
            )
            for e in referencias.get(tabla, [])
        )
        if not evidencia:
            continue

        evidencia.sort(key=lambda e: (e.path, e.line or 0, e.quote))
        # Cota deterministica: un hallazgo con 200 anclas no es accionable.
        evidencia = evidencia[:10]

        implicita = any(s.kind == "to_sql" for s in sitios)
        # Sin ningun sitio DDL, el nombre pudo salir de una CTE o una vista:
        # se reporta igual, pero marcado como incierto para que el operador
        # lo confirme antes de actuar.
        incierta = not sitios

        hallazgos.append(
            Finding(
                category=Category.SCHEMA_DRIFT,
                severity=Severity.S2 if sitios else Severity.S3,
                evidence=evidencia,
                impact=(
                    f"La tabla `{tabla}` se usa desde el codigo pero no tiene "
                    "ninguna sentencia de creacion explicita. "
                    + (
                        "La crea `to_sql`, que infiere los tipos a partir del "
                        "DataFrame del momento: la forma real de la tabla solo "
                        "existe en produccion."
                        if implicita
                        else "Su forma real solo existe en la base."
                    )
                ),
                remediation=(
                    f"Declarar `{tabla}` con columnas y tipos explicitos y "
                    "registrarla en docs/schema.md."
                ),
                effort="M",
                uncertain=incierta,
            )
        )

    return hallazgos


def _hallazgos_no_aditivos(cambios: list[SchemaChange]) -> list[Finding]:
    """Alteraciones destructivas que exigen aprobacion explicita.

    Se agrupa por (tabla, operacion) y no por sitio: el ID de un hallazgo se
    deriva de la evidencia sin rutas ni lineas, asi que dos sitios con el mismo
    par colisionarian en un unico ID. Un `DROP COLUMN` repetido en dos archivos
    es ademas un solo problema de esquema, listado con todos sus sitios.
    """
    agrupados: dict[tuple[str, str], list[SchemaChange]] = {}
    for cambio in cambios:
        if cambio.additive:
            continue
        agrupados.setdefault((cambio.table, cambio.operation), []).append(cambio)

    hallazgos: list[Finding] = []
    for (tabla, operacion), sitios in sorted(agrupados.items()):
        # Orden estable: el artefacto debe ser byte-identico entre corridas.
        sitios = sorted(sitios, key=lambda c: (c.path, c.line))
        plural = "" if len(sitios) == 1 else f" en {len(sitios)} sitios"
        hallazgos.append(
            Finding(
                category=Category.SCHEMA_DRIFT,
                severity=_SEVERIDAD_NO_ADITIVA.get(operacion, Severity.S2),
                evidence=[
                    Evidence(
                        path=cambio.path,
                        line=cambio.line,
                        key=f"{tabla}#{operacion}",
                        quote=cambio.statement,
                    )
                    for cambio in sitios
                ],
                impact=(
                    f"Alteracion no aditiva ({operacion}) sobre "
                    f"`{tabla}`{plural}. Contra las ~79K filas historicas puede "
                    "perder datos o romper lectores en caliente, y no es "
                    "reversible con un simple rollback del commit."
                ),
                remediation=(
                    "Requiere aprobacion explicita del operador antes de "
                    "aplicarse: nunca se propone como cambio automatico. "
                    "Preferir una migracion aditiva (columna nueva + backfill "
                    "+ corte de lectores) y retirar la vieja despues."
                ),
                effort="L",
            )
        )

    return hallazgos
