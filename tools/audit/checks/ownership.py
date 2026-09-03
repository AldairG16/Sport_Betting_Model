"""Checker de responsabilidades solapadas entre modulos.

Detecta cuando dos o mas modulos de produccion, o dos o mas funciones
publicas de nivel superior en archivos distintos, parecen implementar la
MISMA responsabilidad conceptual sin que ninguno de los dos importe al otro:

- Solapamiento de MODULO: el nombre de archivo comparte un token de dominio
  no generico (`calibration`, no `monitor`) con otro archivo de produccion.
- Solapamiento de FUNCION: el nombre de una funcion publica es una variante
  plural trivial del nombre de otra funcion publica en OTRO archivo
  (`value_bet` en un modulo, `value_bets` en otro).

Cada hallazgo enumera TODAS las ubicaciones que compiten por la misma
responsabilidad: reportar una sola obligaria al operador a buscar la otra a
mano.

Redundancia DELIBERADA no es un defecto. `fetch_results` /
`fetch_results_backup_fbdata` es el patron primario/fallback documentado
(The Odds API primero, football-data.co.uk como respaldo cuando la liga no
esta cubierta) — por eso vive en `_PARES_EXENTOS` en vez de depender de que
la heuristica lo deje pasar por casualidad.

Solo textual + `ast` sobre el arbol de fuentes. Este modulo NUNCA importa
`config.settings`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from tools.audit.graph import iter_python_files
from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.registry import check

__all__ = [
    "check_ownership",
    "find_function_overlaps",
    "find_module_overlaps",
]


# ---------------------------------------------------------------------------
# Alcance y vocabulario compartido
# ---------------------------------------------------------------------------

# El auditor y su propia suite no compiten por responsabilidades de negocio.
_PREFIJOS_EXENTOS = ("tools/", "tests/", "archive/")

# Pares EXACTOS que son redundancia deliberada, no un dueño accidental
# duplicado. Se compara el conjunto completo de identificadores del grupo
# (nombre de funcion o stem de modulo) contra estos pares: si el grupo tiene
# un tercer miembro no documentado, deja de calzar exacto y SI se reporta.
_PARES_EXENTOS: frozenset[frozenset[str]] = frozenset(
    {
        # `fetch_results_backup_fbdata` es el fallback de football-data.co.uk
        # cuando The Odds API no cubre la liga (p.ej. China) — dos
        # implementaciones a proposito, documentado en CLAUDE.md/pending
        # resolver flow, no dos duenos que perdieron sincronia.
        frozenset({"fetch_results", "fetch_results_backup_fbdata"}),
    }
)

# Tokens que casi cualquier archivo de este repositorio contiene: agrupar por
# ellos seria puro ruido, no una señal de responsabilidad compartida.
_TOKENS_GENERICOS = frozenset(
    {
        "audit",
        "auditor",
        "script",
        "scripts",
        "utils",
        "util",
        "helper",
        "helpers",
        "manager",
        "engine",
        "monitor",
        "main",
        "run",
        "runner",
        "test",
        "tests",
        "check",
        "checks",
        "data",
        "model",
        "models",
        "base",
        "core",
        "common",
        "init",
        "config",
        "backup",
        "tracker",
        "loader",
        "reader",
        "writer",
    }
)


def _rel(root: Path, ruta: Path) -> str:
    return ruta.relative_to(root).as_posix()


def _es_grupo_exento(identificadores: set[str]) -> bool:
    """True si el grupo es EXACTAMENTE un par documentado como deliberado."""
    return frozenset(identificadores) in _PARES_EXENTOS


def _tokens_de_dominio(nombre: str) -> set[str]:
    """Tokens de negocio de un identificador: sin genericos, sin plural trivial.

    Un token corto (<4 caracteres) rara vez es especifico de dominio en este
    repositorio (`elo`, `xg` son la excepcion y quedan fuera a proposito: son
    demasiado ambiguos para anclar un hallazgo accionable de modulo).
    """
    salida: set[str] = set()
    for parte in nombre.lower().split("_"):
        if not parte or parte in _TOKENS_GENERICOS or len(parte) < 4:
            continue
        singular = parte[:-1] if parte.endswith("s") and not parte.endswith("ss") else parte
        salida.add(singular)
    return salida


def _forma_normalizada_funcion(nombre: str) -> str:
    """Nombre de funcion sin el plural trivial de su ULTIMO segmento.

    `value_bet` y `value_bets` colapsan a la misma forma. Un par que difiere
    en algo mas que ese plural (`fetch_results` vs
    `fetch_results_backup_fbdata`) NO colapsa aqui — motivo por el cual ese
    par necesita el allowlist explicito en vez de depender de esta regla.
    """
    if nombre.endswith("s") and not nombre.endswith("ss") and len(nombre) > 4:
        return nombre[:-1]
    return nombre


# ---------------------------------------------------------------------------
# Inventario de modulos y funciones de produccion
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Modulo:
    stem: str
    ruta: str


@dataclass(frozen=True)
class _Funcion:
    nombre: str
    ruta: str
    linea: int


def _modulos_produccion(root: Path) -> list[_Modulo]:
    raiz = Path(root)
    salida: list[_Modulo] = []
    for ruta in iter_python_files(raiz):
        relativa = _rel(raiz, ruta)
        if relativa.startswith(_PREFIJOS_EXENTOS) or ruta.stem == "__init__":
            continue
        salida.append(_Modulo(stem=ruta.stem, ruta=relativa))
    return sorted(salida, key=lambda m: m.ruta)


def _funciones_publicas(root: Path) -> list[_Funcion]:
    """Funciones de NIVEL DE MODULO (no metodos), publicas, de archivos de produccion."""
    raiz = Path(root)
    salida: list[_Funcion] = []
    for ruta in iter_python_files(raiz):
        relativa = _rel(raiz, ruta)
        if relativa.startswith(_PREFIJOS_EXENTOS):
            continue
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for nodo in arbol.body:
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and not nodo.name.startswith("_"):
                salida.append(_Funcion(nombre=nodo.name, ruta=relativa, linea=nodo.lineno))
    return sorted(salida, key=lambda f: (f.ruta, f.linea, f.nombre))


# ---------------------------------------------------------------------------
# Agrupamiento
# ---------------------------------------------------------------------------

def find_module_overlaps(root: Path) -> dict[str, list[_Modulo]]:
    """Agrupa modulos de produccion por token de dominio compartido en el nombre.

    Solo cuenta un token que sobrevive el stoplist Y aparece en 2+ ARCHIVOS
    distintos. El par exento se filtra por igualdad exacta del conjunto de
    stems del grupo, no por pertenencia parcial.
    """
    por_token: dict[str, list[_Modulo]] = {}
    for modulo in _modulos_produccion(root):
        for token in _tokens_de_dominio(modulo.stem):
            por_token.setdefault(token, []).append(modulo)

    solapados: dict[str, list[_Modulo]] = {}
    for token, modulos in por_token.items():
        rutas_unicas = {m.ruta for m in modulos}
        if len(rutas_unicas) < 2:
            continue
        stems = {m.stem for m in modulos}
        if _es_grupo_exento(stems):
            continue
        solapados[token] = sorted(modulos, key=lambda m: m.ruta)
    return solapados


def find_function_overlaps(root: Path) -> dict[str, list[_Funcion]]:
    """Agrupa funciones publicas por forma normalizada (colapsa plural trivial).

    Exige nombres ACTUALES distintos en archivos DISTINTOS: la misma funcion
    repetida palabra por palabra en el mismo archivo, o la misma firma vista
    dos veces por accidente de parseo, no es este patron.
    """
    por_forma: dict[str, list[_Funcion]] = {}
    for funcion in _funciones_publicas(root):
        clave = _forma_normalizada_funcion(funcion.nombre)
        por_forma.setdefault(clave, []).append(funcion)

    solapados: dict[str, list[_Funcion]] = {}
    for clave, funciones in por_forma.items():
        nombres = {f.nombre for f in funciones}
        rutas = {f.ruta for f in funciones}
        if len(nombres) < 2 or len(rutas) < 2:
            continue
        if _es_grupo_exento(nombres):
            continue
        solapados[clave] = sorted(funciones, key=lambda f: (f.ruta, f.nombre))
    return solapados


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

@check("ownership-overlap", Category.OWNERSHIP_OVERLAP)
def check_ownership(root: Path) -> tuple[list[Finding], int]:
    """Modulos y funciones que compiten por la misma responsabilidad conceptual."""
    raiz = Path(root)
    hallazgos: list[Finding] = []

    modulos = _modulos_produccion(raiz)
    funciones = _funciones_publicas(raiz)
    inspeccionadas = len(modulos) + len(funciones)

    solapes_modulo = find_module_overlaps(raiz)
    for token in sorted(solapes_modulo):
        grupo = solapes_modulo[token]
        evidencias = [Evidence(path=m.ruta, key=token) for m in grupo]
        archivos = ", ".join(m.ruta for m in grupo)
        hallazgos.append(
            Finding(
                category=Category.OWNERSHIP_OVERLAP,
                severity=Severity.S2,
                evidence=evidencias,
                impact=(
                    f"{len(grupo)} modulos comparten la responsabilidad de "
                    f"`{token}` sin que ninguno importe al otro: {archivos}. "
                    "Sin un dueño unico, una correccion en uno no se propaga "
                    "a los demas y el comportamiento diverge en silencio."
                ),
                remediation=(
                    f"Elegir un dueño unico para `{token}` y hacer que los "
                    "demas lo importen, o documentar la coexistencia "
                    "deliberada en `archive/ARCHIVE.md`."
                ),
                effort="M",
            )
        )

    for clave in sorted(find_function_overlaps(raiz)):
        grupo = find_function_overlaps(raiz)[clave]
        evidencias = [
            Evidence(path=f.ruta, line=f.linea, key=f.nombre) for f in grupo
        ]
        detalle = "; ".join(f"`{f.nombre}` en {f.ruta}:{f.linea}" for f in grupo)
        hallazgos.append(
            Finding(
                category=Category.OWNERSHIP_OVERLAP,
                severity=Severity.S2,
                evidence=evidencias,
                impact=(
                    f"{len(grupo)} funciones con nombres que son variantes "
                    f"triviales una de otra implementan la misma "
                    f"responsabilidad: {detalle}. Quien llame a la version "
                    "equivocada obtiene un resultado distinto del que espera."
                ),
                remediation=(
                    "Unificar en una sola funcion, o renombrar ambas para "
                    "dejar clara la diferencia real de responsabilidad."
                ),
                effort="S",
            )
        )

    hallazgos.sort(key=lambda hallazgo: (hallazgo.severity.value, hallazgo.id))
    return hallazgos, inspeccionadas
