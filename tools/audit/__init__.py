"""Auditor estatico del repositorio.

Lee el arbol de fuentes con `ast` y PyYAML unicamente: no abre la base de
datos, no llama a The Odds API ni a Anthropic, y no requiere ningun secreto.

Contratos publicos expuestos aqui para que los checkers importen desde
`tools.audit` sin conocer la estructura interna de modulos.
"""

from tools.audit.model import (
    AuditRun,
    Category,
    Evidence,
    Finding,
    Severity,
)
from tools.audit.redact import redact

__all__ = [
    "AuditRun",
    "Category",
    "Evidence",
    "Finding",
    "Severity",
    "redact",
]
