"""Localiza los sitios donde se define o altera el esquema de la base.

El DDL de este proyecto esta disperso: hay `CREATE TABLE` embebidos en strings
de Python, `ALTER TABLE ... ADD COLUMN` de migracion perezosa y escrituras via
`DataFrame.to_sql(...)` que crean tablas sin ningun DDL explicito. Este modulo
inventaria los tres, sin conectarse nunca a PostgreSQL.

El escaneo es textual (regex sobre el fuente) porque el SQL vive dentro de
literales; `ast` no ve dentro de un string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import EXCLUDED_DIRS

__all__ = ["DDLSite", "SCANNED_SUFFIXES", "scan_ddl"]

SCANNED_SUFFIXES = (".py", ".sql")

# Un identificador de tabla puede venir entrecomillado y con esquema:
# `public.bets_history`, "bets_history", bets_history.
_TABLA = r"[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)[\"'`\]]?"

_PATRONES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "create",
        re.compile(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?" + _TABLA,
            re.IGNORECASE,
        ),
    ),
    (
        "alter",
        re.compile(
            r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _TABLA,
            re.IGNORECASE,
        ),
    ),
    (
        "to_sql",
        re.compile(r"\.to_sql\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    ),
)


@dataclass(frozen=True)
class DDLSite:
    """Un sitio concreto que define o modifica una tabla."""

    path: str
    line: int
    table: str
    kind: str  # 'create' | 'alter' | 'to_sql'


def _archivos(raiz: Path) -> list[Path]:
    encontrados: list[Path] = []
    for ruta in raiz.rglob("*"):
        if not ruta.is_file() or ruta.suffix not in SCANNED_SUFFIXES:
            continue
        partes = ruta.relative_to(raiz).parts
        if any(parte in EXCLUDED_DIRS for parte in partes[:-1]):
            continue
        encontrados.append(ruta)
    return sorted(encontrados, key=lambda p: p.relative_to(raiz).as_posix())


def scan_ddl(root: Path) -> list[DDLSite]:
    """Inventario ordenado y sin duplicados de todos los sitios DDL del arbol."""
    raiz = Path(root)
    sitios: set[DDLSite] = set()

    for ruta in _archivos(raiz):
        relativa = ruta.relative_to(raiz).as_posix()
        texto = ruta.read_text(encoding="utf-8", errors="replace")
        for kind, patron in _PATRONES:
            for coincidencia in patron.finditer(texto):
                # +1 porque count() devuelve cuantos saltos hay ANTES del match.
                linea = texto.count("\n", 0, coincidencia.start()) + 1
                sitios.add(
                    DDLSite(
                        path=relativa,
                        line=linea,
                        table=coincidencia.group(1).lower(),
                        kind=kind,
                    )
                )

    return sorted(sitios, key=lambda s: (s.path, s.line, s.kind, s.table))
