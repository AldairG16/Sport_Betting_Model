"""Indice de modulos y grafo de imports del repositorio, construido con `ast`.

NUNCA importa el modulo bajo analisis: `config/settings.py` lanza RuntimeError
en tiempo de import cuando `DB_URL` no esta definida, asi que todo aqui se
resuelve leyendo el arbol de fuentes y parseandolo estaticamente (FR-006).

`src/` no tiene ningun `__init__.py` (paquetes namespace implicitos), por eso
el nombre punteado se deriva de la ruta relativa y no de marcadores de paquete.
"""

from __future__ import annotations

import ast
from pathlib import Path

__all__ = [
    "EXCLUDED_DIRS",
    "build_import_graph",
    "build_module_index",
    "find_cycles",
    "iter_python_files",
]

# Directorios que jamas se recorren: ruido de VCS, entornos virtuales, cache
# de bytecode y el archivo historico (que por definicion es codigo retirado).
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "archive",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".idea",
        ".vscode",
    }
)


def iter_python_files(root: Path) -> list[Path]:
    """Devuelve todos los .py del arbol, ordenados, saltando EXCLUDED_DIRS."""
    root = Path(root)
    encontrados: list[Path] = []
    for ruta in root.rglob("*.py"):
        partes = ruta.relative_to(root).parts
        if any(parte in EXCLUDED_DIRS for parte in partes[:-1]):
            continue
        encontrados.append(ruta)
    # Orden estable: la salida del auditor tiene que ser byte-identica entre
    # corridas sobre el mismo arbol.
    return sorted(encontrados, key=lambda p: p.relative_to(root).as_posix())


def _module_name(root: Path, ruta: Path) -> str:
    """Nombre punteado de un archivo. `pkg/__init__.py` -> `pkg`."""
    partes = list(ruta.relative_to(root).with_suffix("").parts)
    if partes and partes[-1] == "__init__":
        partes.pop()
    return ".".join(partes)


def build_module_index(root: Path) -> dict[str, Path]:
    """Mapa nombre punteado -> archivo, para cada .py del repositorio."""
    root = Path(root)
    indice: dict[str, Path] = {}
    for ruta in iter_python_files(root):
        nombre = _module_name(root, ruta)
        if not nombre:
            continue
        # Un `pkg/__init__.py` gana sobre un hipotetico `pkg.py` hermano.
        if nombre in indice and ruta.name != "__init__.py":
            continue
        indice[nombre] = ruta
    return {clave: indice[clave] for clave in sorted(indice)}


def _paquetes(indice: dict[str, Path]) -> set[str]:
    """Modulos que son paquetes reales (vienen de un `__init__.py`)."""
    return {
        nombre
        for nombre, ruta in indice.items()
        if ruta.name == "__init__.py"
    }


def _base_relativa(modulo: str, es_paquete: bool, level: int) -> str | None:
    """Resuelve el prefijo de un import relativo (`from . import x`)."""
    partes = modulo.split(".") if modulo else []
    if not es_paquete:
        partes = partes[:-1]
    subir = level - 1
    if subir > len(partes):
        return None
    if subir:
        partes = partes[:-subir]
    return ".".join(partes)


def _resolver(candidato: str, indice: dict[str, Path]) -> str | None:
    """El candidato exacto, o su prefijo mas largo presente en el indice.

    `from tools.audit.model import Finding` produce el candidato
    `tools.audit.model.Finding`, que no es un modulo: hay que recortar hasta
    dar con `tools.audit.model`.
    """
    partes = candidato.split(".")
    while partes:
        nombre = ".".join(partes)
        if nombre in indice:
            return nombre
        partes.pop()
    return None


def build_import_graph(root: Path) -> dict[str, set[str]]:
    """Mapa modulo -> modulos internos que importa.

    Usa `ast.walk`, no solo el cuerpo de nivel superior, para que los imports
    escritos dentro de funciones (patron habitual en tests/ y scripts/) cuenten
    igual que los de cabecera.
    """
    root = Path(root)
    indice = build_module_index(root)
    paquetes = _paquetes(indice)
    grafo: dict[str, set[str]] = {nombre: set() for nombre in indice}

    for modulo, ruta in indice.items():
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            # Un archivo que no parsea no aporta aristas; el checker de
            # referencias rotas lo reporta por su cuenta.
            continue

        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    destino = _resolver(alias.name, indice)
                    if destino and destino != modulo:
                        grafo[modulo].add(destino)
            elif isinstance(nodo, ast.ImportFrom):
                if nodo.level:
                    base = _base_relativa(modulo, modulo in paquetes, nodo.level)
                    if base is None:
                        continue
                    prefijo = f"{base}.{nodo.module}" if nodo.module else base
                else:
                    if not nodo.module:
                        continue
                    prefijo = nodo.module
                for alias in nodo.names:
                    if alias.name == "*":
                        candidatos = [prefijo]
                    else:
                        candidatos = [f"{prefijo}.{alias.name}", prefijo]
                    for candidato in candidatos:
                        destino = _resolver(candidato, indice)
                        if destino and destino != modulo:
                            grafo[modulo].add(destino)
                            break
    return grafo


def find_cycles(
    graph: dict[str, set[str]], max_length: int = 12
) -> list[list[str]]:
    """Ciclos elementales del grafo, en forma canonica y ordenados.

    Cada ciclo se enumera una sola vez, desde su nodo lexicograficamente menor
    (de ahi la restriccion `siguiente > inicio`), asi que dos llamadas sobre el
    mismo grafo devuelven exactamente la misma lista.

    `max_length` acota la busqueda en grafos patologicos; los ciclos reales de
    este repositorio son de 2 a 4 modulos.
    """
    encontrados: set[tuple[str, ...]] = set()

    def _dfs(inicio: str, actual: str, camino: list[str], visitados: set[str]) -> None:
        if len(camino) > max_length:
            return
        for siguiente in sorted(graph.get(actual, ())):
            if siguiente == inicio:
                encontrados.add(tuple(camino))
            elif siguiente > inicio and siguiente not in visitados:
                _dfs(inicio, siguiente, camino + [siguiente], visitados | {siguiente})

    for nodo in sorted(graph):
        _dfs(nodo, nodo, [nodo], {nodo})

    return sorted([list(ciclo) for ciclo in encontrados])
