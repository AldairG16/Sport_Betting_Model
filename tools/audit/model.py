"""Contratos de datos del auditor estatico.

Vocabulario compartido por todos los checkers: severidades, categorias,
anclas de evidencia, hallazgos y corridas completas.

IMPORTANTE: solo stdlib. Este modulo NUNCA debe importar `config.settings`
ni `config.database` — `config/settings.py` lanza RuntimeError en tiempo de
import cuando `DB_URL` no esta definida, y la auditoria tiene que poder
correr sin ningun secreto configurado (FR-006).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

__all__ = [
    "AuditRun",
    "Category",
    "Evidence",
    "Finding",
    "Severity",
    "Status",
]


class Severity(str, Enum):
    """Escala de severidad. S1 es lo mas grave; el orden de declaracion manda.

    S1 — puede cambiar una apuesta, dimensionar un stake o silenciar una alerta.
    S2 — rompe una garantia operativa sin mover dinero de forma directa.
    S3 — deuda tecnica o inconsistencia que confunde al operador.
    S4 — cosmetico / informativo.
    """

    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class Status(str, Enum):
    """Estado de triage de un hallazgo. `open` significa SIN TRIAR.

    La distincion importa: un inventario que reporta 200 de 200 abiertos no
    dice nada sobre el repo, solo que nadie lo ha leido. Los cinco estados
    cubren las cinco cosas que le pueden pasar a un hallazgo:

    OPEN       — todavia no lo miro nadie. Es el default y es deuda de triage.
    RESOLVED   — desaparecio de la corrida actual porque se arreglo. Lo pone
                 `diff.py` solo; no se escribe a mano en el ledger.
    REASSIGNED — no desaparecio: el mismo defecto cambio de ID porque su
                 fichero se renombro o se movio. Lo pone `diff.py` solo, igual
                 que `RESOLVED`, y la nota de `remediation` lleva al ID nuevo.
                 Cerrarlo como `resolved` mentiria: nada se arreglo.
    DEFERRED   — defecto REAL, revisado, que no se arregla en este ciclo. Exige
                 una razon: sin razon es indistinguible de haberlo ignorado.
    ACCEPTED   — revisado y cerrado como no-defecto: falso positivo del checker
                 o comportamiento intencional. Tambien exige razon.

    `REASSIGNED` no entra en `triaged()` a proposito: como `RESOLVED`, lo
    asigna la maquina y no un humano, asi que no exige `triage_reason`.
    Meterlo ahi haria que `diff.py` reventara al cerrar un renombre.

    `DEFERRED` y `ACCEPTED` NO son sinonimos y no se deben mezclar: llamarle
    "diferido" a un falso positivo infla para siempre la deuda aparente, y
    llamarle "aceptado" a un defecto real lo entierra.
    """

    OPEN = "open"
    RESOLVED = "resolved"
    DEFERRED = "deferred"
    ACCEPTED = "accepted"
    REASSIGNED = "reassigned"

    @classmethod
    def triaged(cls) -> tuple["Status", ...]:
        """Estados que un humano asigna explicitamente en el ledger."""
        return (cls.DEFERRED, cls.ACCEPTED)


class Category(str, Enum):
    """Las diez categorias mandatadas por el spec. Ni una mas, ni una menos."""

    BROKEN_REFERENCE = "broken-reference"
    ORPHAN_CODE = "orphan-code"
    CONFIG_INCONSISTENCY = "config-inconsistency"
    DOC_DRIFT = "doc-drift"
    SCHEMA_DRIFT = "schema-drift"
    ERROR_HANDLING_RISK = "error-handling-risk"
    CONVENTION_VIOLATION = "convention-violation"
    SECRET_LEAKAGE = "secret-leakage"
    CIRCULAR_DEPENDENCY = "circular-dependency"
    OWNERSHIP_OVERLAP = "ownership-overlap"


@dataclass(frozen=True)
class Evidence:
    """Ancla concreta de un hallazgo.

    Un hallazgo sin ancla no es accionable, asi que `path` es obligatorio y
    se acompania de UNO de estos discriminantes segun el tipo de fuente:

    - `line`  -> codigo Python (`archivo.py:120`)
    - `key`   -> workflow YAML, tabla.columna o clave de configuracion
    - ninguno -> el archivo completo es la evidencia (p.ej. modulo huerfano)

    Es `frozen` para que sea hashable y ordenable: los checkers deduplican
    evidencias con `set()` antes de construir el hallazgo.
    """

    path: str
    line: int | None = None
    key: str | None = None
    quote: str = ""

    @property
    def anchor(self) -> str:
        """Ancla legible para el reporte: `path:line`, `path:key` o `path`."""
        if self.line is not None:
            return f"{self.path}:{self.line}"
        if self.key:
            return f"{self.path}:{self.key}"
        return self.path

    def signature(self) -> str:
        """Firma normalizada y tolerante a movimientos de archivo.

        Deliberadamente descarta:
        - el directorio padre (renombrar/mover una carpeta no crea un hallazgo
          nuevo: seria ruido puro en el diff corrida-a-corrida), y
        - el numero de linea (insertar un import arriba desplaza todo el
          archivo sin cambiar el defecto).

        Conserva el nombre del archivo, la clave y la cita normalizada, que
        juntos identifican el defecto real.
        """
        name = PurePosixPath(self.path.replace("\\", "/")).name.lower()
        key = (self.key or "").strip().lower()
        quote = " ".join((self.quote or "").split())
        return f"{name}|{key}|{quote}"


@dataclass
class Finding:
    """Un defecto concreto, con ancla, impacto y arreglo propuesto.

    No es `frozen` a proposito: `diff.py` marca `status='resolved'` cuando un
    hallazgo previo desaparece de la corrida actual, en vez de dejarlo
    desvanecerse en silencio (FR-032).
    """

    category: Category
    severity: Severity
    evidence: list[Evidence]
    impact: str
    remediation: str
    effort: str = "S"
    status: str = Status.OPEN.value
    runtime_owner: str | None = None
    uncertain: bool = False
    # Por que este hallazgo no esta `open`. Obligatorio para `deferred` y
    # `accepted`: un estado cerrado sin razon no es triage, es un borrado.
    triage_reason: str | None = None

    def __post_init__(self) -> None:
        # Aceptamos strings crudos por comodidad de los checkers, pero
        # normalizamos a enum para que un valor invalido reviente aqui y no
        # a la hora de serializar el reporte.
        self.category = Category(self.category)
        self.severity = Severity(self.severity)
        # Un estado con typo ('defered') se leeria como 'no abierto' en
        # cualquier filtro laxo y desapareceria del inventario sin que nadie
        # lo decidiera. Se valida aqui, no al serializar.
        self.status = Status(self.status).value
        if self.status in {s.value for s in Status.triaged()} and not (
            self.triage_reason or ""
        ).strip():
            raise ValueError(
                f"el hallazgo {self.category.value} en estado "
                f"'{self.status}' no trae razon de triage"
            )

    @property
    def id(self) -> str:
        """ID estable derivado de categoria + firma normalizada de evidencia.

        NO depende de rutas absolutas ni de numeros de linea, de modo que el
        mismo defecto conserva su ID entre corridas aunque el archivo se mueva
        de directorio o se desplace dentro del archivo.
        """
        parts = sorted(ev.signature() for ev in self.evidence)
        raw = self.category.value + "\n" + "\n".join(parts)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self.category.value}-{digest[:12]}"

    @property
    def anchors(self) -> list[str]:
        """Todas las anclas legibles del hallazgo, en orden de evidencia."""
        return [ev.anchor for ev in self.evidence]


@dataclass
class AuditRun:
    """Una corrida completa del auditor."""

    run_id: str
    timestamp: str
    commit_sha: str
    findings: list[Finding] = field(default_factory=list)
    inspected: dict[str, int] = field(default_factory=dict)

    def counts_by_severity(self) -> dict[str, int]:
        """Conteo por severidad, con las cuatro severidades siempre presentes.

        El relleno en cero es intencional: un reporte que omite `S1` cuando
        vale 0 es indistinguible de un reporte donde el checker de S1 no
        corrio (FR-016).
        """
        counts = {sev.value: 0 for sev in Severity}
        for finding in self.findings:
            counts[Severity(finding.severity).value] += 1
        return counts

    def counts_by_status(self) -> dict[str, int]:
        """Conteo por estado de triage, con los cuatro estados presentes.

        Es la metrica que hace auditable la deuda de triage: `open` alto no
        significa "repo roto", significa "nadie ha leido el inventario".
        """
        counts = {estado.value: 0 for estado in Status}
        for finding in self.findings:
            counts[Status(finding.status).value] += 1
        return counts

    def counts_by_category(self) -> dict[str, int]:
        """Conteo por categoria, con las diez categorias siempre presentes."""
        counts = {cat.value: 0 for cat in Category}
        for finding in self.findings:
            counts[Category(finding.category).value] += 1
        return counts
