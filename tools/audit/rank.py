"""Ordenamiento de hallazgos: primero lo que puede costar dinero.

El artefacto de auditoria se lee de arriba hacia abajo y casi nunca hasta el
final. Por eso el orden NO es un detalle cosmetico: es la unica garantia de
que lo primero que el operador ve responde "que es lo mas probable que me
haga perder dinero la semana que viene".

Hay DOS ordenes, y no son el mismo por una razon concreta:

`rank()` — orden canonico del artefacto. Clave: `(severidad, categoria, id)`.

    1. `severity` es el eje de riesgo. La escala esta definida en `model.py`
       en esos terminos exactos: S1 es "puede cambiar una apuesta,
       dimensionar un stake o silenciar una alerta" y S4 es cosmetico. Con
       poner S1 arriba, lo que mueve dinero ya quedo arriba.
    2. `category` (el valor textual, alfabetico) agrupa: todos los
       `broken-reference` de una severidad quedan contiguos. Un artefacto
       que intercala categorias dentro de la misma severidad obliga a
       leerlo entero para saber cuantos defectos de un tipo hay.
    3. `id` estable cierra el orden. Sin este tercer componente el orden
       dependeria del orden de llegada de los checkers y dos corridas
       identicas produciran artefactos distintos byte a byte, que es
       exactamente lo que el test de determinismo prohibe.

    Las tres componentes juntas hacen de `rank()` una funcion TOTAL: nunca
    quedan dos hallazgos "empatados" cuyo orden relativo dependa de la
    estabilidad del sort.

`top_risks()` — el digest de consola. Ahi si manda `PESO_CATEGORIA`, porque
    esas cinco lineas no se agrupan ni se hojean: son la respuesta directa a
    "que atiendo hoy". Dentro de una misma severidad una credencial filtrada
    o una constante de configuracion partida en dos pesan mas que un modulo
    huerfano, y ese matiz importa cuando solo caben cinco renglones.

Separarlos es deliberado. Mezclar el peso de riesgo en el orden del
artefacto lo vuelve imposible de hojear por categoria; agrupar por categoria
en el digest lo vuelve inutil como lista de prioridades.

Solo stdlib. Este modulo NUNCA debe importar `config.settings`.
"""

from __future__ import annotations

from collections.abc import Iterable

from tools.audit.model import Category, Finding, Severity

__all__ = [
    "ESTADOS_CERRADOS",
    "PESO_CATEGORIA",
    "category_weight",
    "rank",
    "risk_key",
    "sort_key",
    "top_risks",
]


# Estados que ya no representan un riesgo vigente. Se declaran aqui, y no en
# `diff.py`, porque `diff` importa a `rank` y no al reves: invertir esa
# direccion crearia un ciclo entre dos modulos del propio auditor, que es
# justo el defecto que la categoria `circular-dependency` persigue.
ESTADOS_CERRADOS = frozenset({"resolved", "reassigned"})


# Peso por categoria: MENOR es mas urgente. Solo lo usa `top_risks()`.
#
# El criterio es literal: "puede cambiar una apuesta, dimensionar un stake o
# silenciar una alerta" va arriba; "confunde al lector" va abajo.
#
#   0-1  dinero directo   — la credencial filtrada y la constante partida en
#                           dos fuentes de verdad cambian cuanto se apuesta.
#   2-4  produccion rota  — el handler mudo, el import que no resuelve y el
#                           DDL disperso tumban o falsean una corrida.
#   5-7  garantia erosionada — el documento que miente y la responsabilidad
#                           duplicada llevan al operador a configurar mal.
#   8-9  cosmetico        — convencion y codigo huerfano: deuda, no perdida.
PESO_CATEGORIA: dict[Category, int] = {
    Category.SECRET_LEAKAGE: 0,
    Category.CONFIG_INCONSISTENCY: 1,
    Category.ERROR_HANDLING_RISK: 2,
    Category.BROKEN_REFERENCE: 3,
    Category.SCHEMA_DRIFT: 4,
    Category.DOC_DRIFT: 5,
    Category.OWNERSHIP_OVERLAP: 6,
    Category.CIRCULAR_DEPENDENCY: 7,
    Category.CONVENTION_VIOLATION: 8,
    Category.ORPHAN_CODE: 9,
}


def category_weight(category: Category | str) -> int:
    """Peso de riesgo de una categoria. Toda categoria del enum lo tiene.

    Se resuelve contra `PESO_CATEGORIA` y no con `.get(..., default)`: una
    categoria nueva sin peso caeria al mismo cajon que todas las demas y su
    orden relativo quedaria indefinido en silencio. Preferimos que reviente
    aqui, al agregar la categoria, y no meses despues en el reporte.
    """
    return PESO_CATEGORIA[Category(category)]


def sort_key(finding: Finding) -> tuple[str, str, str]:
    """Clave canonica del artefacto: (severidad, categoria, id estable)."""
    return (
        Severity(finding.severity).value,
        Category(finding.category).value,
        finding.id,
    )


def risk_key(finding: Finding) -> tuple[str, int, str]:
    """Clave del digest de consola: (severidad, peso de riesgo, id estable)."""
    return (
        Severity(finding.severity).value,
        category_weight(finding.category),
        finding.id,
    )


def rank(findings: Iterable[Finding]) -> list[Finding]:
    """Los hallazgos en el orden canonico del artefacto.

    Devuelve una lista NUEVA y no toca la de entrada: los checkers conservan
    su propio orden interno, que sus tests verifican por separado.
    """
    return sorted(findings, key=sort_key)


def top_risks(findings: Iterable[Finding], limit: int = 5) -> list[Finding]:
    """Los `limit` hallazgos abiertos que mas pueden costar dinero.

    Ordena por `risk_key`, no por `sort_key`: aqui el peso de categoria si
    manda, porque la lista es corta y su unico proposito es priorizar.

    Filtra los cerrados a proposito: un hallazgo arreglado es un acuse de
    recibo, no un riesgo, y uno reasignado ya aparece en la lista bajo su ID
    nuevo. Encabezar el resumen con cualquiera de los dos no le dice al
    operador que atender hoy.
    """
    if limit <= 0:
        return []
    abiertos = [f for f in findings if f.status not in ESTADOS_CERRADOS]
    return sorted(abiertos, key=risk_key)[:limit]
