"""Comparacion corrida-a-corrida: nada desaparece en silencio.

Un hallazgo que se arregla NO puede limitarse a faltar en el reporte
siguiente. "No esta" y "nunca estuvo" se leen igual, y esa ambiguedad
convierte al artefacto en algo que no se puede auditar: el operador no
distingue el defecto que cerro del checker que dejo de correr.

Por eso `diff_runs` conserva el hallazgo previo y lo marca `resolved`
(FR-032). El artefacto crece con acuses de recibo en vez de encogerse con
olvidos.

Hay un segundo modo de desaparicion, mas sutil: el ID cambia sin que el
defecto cambie. `Evidence.signature()` ya descarta el directorio padre y el
numero de linea, asi que mover un archivo de carpeta o insertar imports
arriba NO reasigna el ID. Lo que si lo reasigna es RENOMBRAR el archivo,
porque el nombre base si entra en la firma. Ese caso se detecta comparando
una clave que ademas descarta la ruta completa, y se reporta como
reasignacion (`reassigned`) en vez de como un cierre mas un alta.

Solo stdlib. Este modulo NUNCA debe importar `config.settings` — lanza
RuntimeError en tiempo de import sin `DB_URL`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from tools.audit.model import AuditRun, Category, Evidence, Finding, Severity
from tools.audit.rank import ESTADOS_CERRADOS, rank
from tools.audit.redact import redact

__all__ = [
    "Reasignacion",
    "diff_runs",
    "identidad",
    "load_run",
    "reassignments",
]

# Atributo de instancia donde `load_run` guarda el ID tal como quedo escrito
# en el artefacto. Es necesario porque el JSON almacena las citas YA
# redactadas: recalcular el ID sobre una cita redactada daria un valor
# distinto al que el propio archivo declara, y el diff compararia manzanas
# con peras. El artefacto es la fuente de verdad de su propio ID.
_ATRIBUTO_ID_GUARDADO = "_stored_id"


@dataclass(frozen=True)
class Reasignacion:
    """Un mismo defecto que cambio de ID entre dos corridas."""

    id_previo: str
    id_actual: str
    categoria: str
    motivo: str


def identidad(finding: Finding) -> str:
    """ID con el que este hallazgo participa del diff.

    Para un hallazgo recien calculado es su `id` derivado. Para uno leido de
    `audits/latest.json` es el ID que el artefacto declara, no el que se
    recalcularia sobre las citas redactadas.
    """
    guardado = getattr(finding, _ATRIBUTO_ID_GUARDADO, None)
    return guardado or finding.id


def _clave_reasignacion(finding: Finding) -> str:
    """Clave del defecto que sobrevive incluso a un renombre de archivo.

    Descarta la ruta ENTERA (no solo el directorio, como hace
    `Evidence.signature()`) y conserva categoria, clave y cita normalizada.
    Las citas pasan por `redact()` en ambos lados de la comparacion porque
    el artefacto las guarda redactadas y la corrida en curso las tiene en
    crudo; sin esa normalizacion todo hallazgo con una credencial en la cita
    pareceria reasignado en cada corrida.
    """
    partes = sorted(
        "{}|{}".format(
            (ev.key or "").strip().lower(),
            " ".join(redact(ev.quote or "").split()),
        )
        for ev in finding.evidence
    )
    return Category(finding.category).value + "\n" + "\n".join(partes)


def _copiar(finding: Finding) -> Finding:
    """Copia superficial que arrastra el ID declarado, si lo hay."""
    copia = replace(finding, evidence=list(finding.evidence))
    guardado = getattr(finding, _ATRIBUTO_ID_GUARDADO, None)
    if guardado:
        setattr(copia, _ATRIBUTO_ID_GUARDADO, guardado)
    return copia


def _cerrar(finding: Finding, estado: str, nota: str) -> Finding:
    """Copia del hallazgo marcada como cerrada, sin tocar el objeto original.

    `replace` produce una instancia nueva: mutar el hallazgo de la corrida
    previa contaminaria al llamador, que puede seguir usando ese `AuditRun`
    (los tests lo hacen).
    """
    copia = replace(
        finding,
        evidence=list(finding.evidence),
        status=estado,
        remediation=f"{nota} {finding.remediation}".strip(),
    )
    # El ID viaja con la copia: es la unica forma de que el hallazgo cerrado
    # se pueda casar con el que el operador vio en el reporte anterior.
    setattr(copia, _ATRIBUTO_ID_GUARDADO, identidad(finding))
    return copia


def reassignments(
    previous: AuditRun | None, current: AuditRun
) -> list[Reasignacion]:
    """Defectos que cambiaron de ID sin dejar de existir.

    Solo se consideran candidatos los hallazgos que desaparecieron del lado
    previo y los que aparecieron del lado actual: un hallazgo presente en
    ambos con el mismo ID no se reasigno nada.
    """
    if previous is None:
        return []

    ids_actuales = {identidad(f) for f in current.findings}
    ids_previos = {identidad(f) for f in previous.findings}

    desaparecidos = [
        f
        for f in previous.findings
        if identidad(f) not in ids_actuales
        and f.status not in ESTADOS_CERRADOS
    ]
    aparecidos = [
        f for f in current.findings if identidad(f) not in ids_previos
    ]

    # Se indexan los aparecidos por clave tolerante al renombre. Si dos
    # hallazgos nuevos comparten clave la correspondencia es ambigua, asi
    # que no se declara reasignacion: preferimos reportar un cierre y un
    # alta antes que inventar un vinculo que puede ser falso.
    indice: dict[str, list[Finding]] = {}
    for finding in aparecidos:
        indice.setdefault(_clave_reasignacion(finding), []).append(finding)

    salida: list[Reasignacion] = []
    for finding in desaparecidos:
        candidatos = indice.get(_clave_reasignacion(finding), [])
        if len(candidatos) != 1:
            continue
        nuevo = candidatos[0]
        salida.append(
            Reasignacion(
                id_previo=identidad(finding),
                id_actual=identidad(nuevo),
                categoria=Category(finding.category).value,
                motivo=(
                    "el archivo cambio de nombre: "
                    f"{', '.join(finding.anchors)} -> "
                    f"{', '.join(nuevo.anchors)}"
                ),
            )
        )

    salida.sort(key=lambda r: (r.id_previo, r.id_actual))
    return salida


def diff_runs(previous: AuditRun | None, current: AuditRun) -> AuditRun:
    """Corrida actual enriquecida con lo que dejo de aparecer.

    El resultado conserva los metadatos de `current` (es la corrida que se
    esta publicando) y agrega, ordenados junto al resto:

    - los hallazgos previos ausentes hoy, con `status='resolved'`;
    - los previos cuyo ID se reasigno, con `status='reassigned'` y una nota
      que apunta al ID nuevo, para que el operador pueda seguir el hilo.

    Nunca elimina un hallazgo de la corrida actual ni cambia su estado.
    """
    findings = [_copiar(f) for f in current.findings]

    if previous is not None:
        ids_actuales = {identidad(f) for f in current.findings}
        reasignados = {r.id_previo: r for r in reassignments(previous, current)}

        for finding in previous.findings:
            clave = identidad(finding)
            if clave in ids_actuales:
                continue
            # Un hallazgo que ya venia cerrado no se vuelve a cerrar: se
            # arrastraria para siempre, corrida tras corrida, hasta ahogar
            # el reporte con acuses de recibo antiguos.
            if finding.status in ESTADOS_CERRADOS:
                continue
            reasignacion = reasignados.get(clave)
            if reasignacion is not None:
                findings.append(
                    _cerrar(
                        finding,
                        "reassigned",
                        f"[ID reasignado a `{reasignacion.id_actual}`]",
                    )
                )
            else:
                findings.append(
                    _cerrar(finding, "resolved", "[resuelto en esta corrida]")
                )

    return AuditRun(
        run_id=current.run_id,
        timestamp=current.timestamp,
        commit_sha=current.commit_sha,
        findings=rank(findings),
        inspected=dict(current.inspected),
    )


def _finding_desde_dict(data: dict) -> Finding:
    """Reconstruye un hallazgo a partir de su forma serializada."""
    evidencias = [
        Evidence(
            path=str(ev.get("path", "")),
            line=ev.get("line"),
            key=ev.get("key"),
            quote=str(ev.get("quote") or ""),
        )
        for ev in data.get("evidence", [])
    ]
    finding = Finding(
        category=Category(data["category"]),
        severity=Severity(data["severity"]),
        evidence=evidencias,
        impact=str(data.get("impact", "")),
        remediation=str(data.get("remediation", "")),
        effort=str(data.get("effort", "S")),
        status=str(data.get("status", "open")),
        runtime_owner=data.get("runtime_owner"),
        uncertain=bool(data.get("uncertain", False)),
    )
    guardado = data.get("id")
    if guardado:
        setattr(finding, _ATRIBUTO_ID_GUARDADO, str(guardado))
    return finding


def load_run(path: Path) -> AuditRun | None:
    """Lee un artefacto `latest.json` previo, o None si no hay o no sirve.

    Devolver None en vez de reventar es deliberado: la PRIMERA corrida no
    tiene artefacto previo, y un archivo corrupto no puede impedir que se
    publique la auditoria de hoy. El costo de tolerarlo es perder el marcado
    de resueltos de una corrida; el costo de reventar es quedarse sin
    auditoria.
    """
    ruta = Path(path)
    if not ruta.is_file():
        return None
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    findings: list[Finding] = []
    for bruto in data.get("findings", []):
        try:
            findings.append(_finding_desde_dict(bruto))
        except (KeyError, TypeError, ValueError):
            # Un hallazgo con una categoria o severidad que ya no existe en
            # el enum pertenece a una version anterior del auditor. Se
            # descarta ese hallazgo, no el artefacto entero.
            continue

    inspected = {
        str(k): int(v)
        for k, v in (data.get("inspected") or {}).items()
        if isinstance(v, int) and not isinstance(v, bool)
    }

    return AuditRun(
        run_id=str(data.get("run_id", "")),
        timestamp=str(data.get("timestamp", "")),
        commit_sha=str(data.get("commit_sha", "")),
        findings=findings,
        inspected=inspected,
    )
