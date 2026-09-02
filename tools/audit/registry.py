"""Registro deterministico de checkers.

Todo checker se enchufa igual: decora una funcion con `@check(...)` y queda
disponible en `all_checks()`, siempre ordenado por nombre. El orden NO
depende del orden de import — esa es justamente la propiedad que hace que
dos corridas sobre el mismo arbol produzcan el mismo artefacto (FR-030).

Un checker escrito pero nunca registrado no existe para el reporte. Por eso
`all_checks()` es la UNICA fuente de verdad y el contador de ubicaciones
inspeccionadas viaja junto a los hallazgos: una categoria con
`0 hallazgos, 0 ubicaciones inspeccionadas` delata al checker que falta,
mientras que `0 hallazgos, 812 ubicaciones inspeccionadas` es una categoria
genuinamente limpia.

Solo stdlib. Este modulo NUNCA debe importar `config.settings` — lanza
RuntimeError en tiempo de import sin `DB_URL` y romperia la auditoria
sin secretos (FR-006).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tools.audit.model import Category, Finding

__all__ = [
    "CheckSpec",
    "all_checks",
    "check",
    "get_check",
    "isolated_registry",
]

# Un checker recibe la raiz del repo y devuelve (hallazgos, ubicaciones
# inspeccionadas). El segundo elemento es obligatorio a proposito: sin el,
# "no encontre nada" y "no mire nada" son indistinguibles.
CheckFunc = Callable[[Path], "tuple[list[Finding], int]"]


@dataclass(frozen=True)
class CheckSpec:
    """Un checker registrado, con las categorias que puede emitir."""

    name: str
    category: Category
    func: CheckFunc
    also: tuple[Category, ...] = field(default_factory=tuple)

    @property
    def categories(self) -> tuple[Category, ...]:
        """Categoria primaria mas las secundarias, sin duplicados y en orden.

        Existe porque hay checkers legitimamente multi-categoria: el de
        referencias emite `broken-reference`, `orphan-code` y
        `circular-dependency` en una sola pasada sobre el grafo de imports.
        Todas ellas quedan cubiertas por sus ubicaciones inspeccionadas.
        """
        out: list[Category] = [self.category]
        for cat in self.also:
            if cat not in out:
                out.append(cat)
        return tuple(out)

    def run(self, root: Path) -> tuple[list[Finding], int]:
        """Ejecuta el checker y valida el contrato de retorno.

        Se valida aqui y no en el decorador porque el error solo es
        observable al ejecutar: un checker que devuelve una lista pelada
        rompe el conteo de ubicaciones inspeccionadas de forma silenciosa,
        que es exactamente la clase de fallo que esta auditoria persigue.
        """
        result = self.func(Path(root))

        if not isinstance(result, tuple) or len(result) != 2:
            raise TypeError(
                f"El checker '{self.name}' debe devolver "
                f"(list[Finding], int); devolvio {type(result).__name__}."
            )

        findings, inspected = result

        if not isinstance(findings, (list, tuple)):
            raise TypeError(
                f"El checker '{self.name}' devolvio hallazgos de tipo "
                f"{type(findings).__name__}; se esperaba una lista."
            )
        if isinstance(inspected, bool) or not isinstance(inspected, int):
            raise TypeError(
                f"El checker '{self.name}' devolvio un conteo de ubicaciones "
                f"de tipo {type(inspected).__name__}; se esperaba int."
            )
        if inspected < 0:
            raise ValueError(
                f"El checker '{self.name}' devolvio un conteo negativo de "
                f"ubicaciones inspeccionadas: {inspected}."
            )

        return list(findings), inspected


# Nombre de checker -> spec. Se ordena al leer, no al escribir, para que el
# orden de import de los modulos de `checks/` sea irrelevante.
_REGISTRY: dict[str, CheckSpec] = {}


def check(
    name: str,
    category: Category,
    *,
    also: Sequence[Category] = (),
) -> Callable[[CheckFunc], CheckFunc]:
    """Decorador que registra un checker bajo `name`.

    Devuelve la funcion original SIN envolver, para que cada checker siga
    siendo importable y testeable de forma directa sin pasar por el registro.

    `also` declara categorias adicionales que el checker puede emitir, de
    modo que sus ubicaciones inspeccionadas cuenten tambien para ellas.
    """
    if not name or not name.strip():
        raise ValueError("El nombre del checker no puede estar vacio.")

    category = Category(category)
    extra = tuple(Category(cat) for cat in also)

    def decorator(func: CheckFunc) -> CheckFunc:
        if name in _REGISTRY:
            raise ValueError(
                f"Ya existe un checker registrado como '{name}' "
                f"({_REGISTRY[name].func.__module__}). Los nombres son la "
                f"clave estable del registro y deben ser unicos."
            )
        _REGISTRY[name] = CheckSpec(
            name=name, category=category, func=func, also=extra
        )
        return func

    return decorator


def all_checks() -> list[CheckSpec]:
    """Todos los checkers registrados, ordenados por nombre.

    El orden es alfabetico y estable, independiente del orden en que se
    hayan importado los modulos de `tools/audit/checks/`.
    """
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def get_check(name: str) -> CheckSpec | None:
    """El checker registrado bajo `name`, o None si no existe."""
    return _REGISTRY.get(name)


@contextmanager
def isolated_registry() -> Iterator[dict[str, CheckSpec]]:
    """Aisla el registro global durante un bloque y lo restaura al salir.

    Pensado para los tests: registrar checkers de prueba en el registro real
    contaminaria las corridas siguientes dentro del mismo proceso de pytest.
    """
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    try:
        yield _REGISTRY
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)
