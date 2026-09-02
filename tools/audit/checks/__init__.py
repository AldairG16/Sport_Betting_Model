"""Paquete de checkers del auditor estatico.

Cada modulo de este paquete registra sus checkers con el decorador
`@check(...)` de `tools.audit.registry`. Un checker vive aqui o no vive:
`load_all()` importa TODOS los modulos del paquete, de modo que agregar un
checker nuevo no exige tocar ninguna lista central que alguien pueda olvidar
actualizar.

Ese olvido es justamente el modo de fallo que la auditoria persigue: un
checker escrito pero nunca importado deja su categoria en cero hallazgos y
cero ubicaciones inspeccionadas, indistinguible a simple vista de una
categoria limpia. El contador de `report.py` lo delata.

Solo stdlib. Ningun modulo de este paquete debe importar `config.settings`.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = ["load_all"]


def load_all() -> list[str]:
    """Importa todos los modulos de checkers y devuelve sus nombres ordenados.

    El orden de import es alfabetico y por lo tanto reproducible, aunque el
    registro vuelve a ordenar por nombre de checker de todas formas: el
    determinismo del reporte no puede depender de este detalle.
    """
    cargados: list[str] = []
    for info in sorted(
        pkgutil.iter_modules(__path__), key=lambda m: m.name
    ):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{info.name}")
        cargados.append(info.name)
    return cargados
