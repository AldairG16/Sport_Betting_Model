"""Escritores de artefactos: `audits/latest.json` y `audits/latest.md`.

Dos reglas gobiernan este modulo:

1. DETERMINISMO. El mismo `AuditRun` serializado dos veces produce archivos
   byte-identicos: `sort_keys=True`, indentacion fija, orden explicito de
   hallazgos y `newline="\n"` al escribir (sin esto, Windows traduce a CRLF
   y el diff corrida-a-corrida contra ubuntu-latest seria ilegible).

2. NINGUNA CATEGORIA SE OMITE. Se emite una seccion por cada miembro de
   `Category`, incluso con cero hallazgos, renderizada como
   `0 hallazgos, N ubicaciones inspeccionadas`. La ausencia de una seccion
   nunca puede leerse como ausencia de defectos, porque simplemente no hay
   ausencia de secciones (FR-016).

Todo texto libre pasa por `redact()`: estos artefactos se commitean y se
suben como artifact de CI, asi que un valor de credencial filtrado aqui
convertiria al auditor en el peor leak del repo.

Solo stdlib. Este modulo NUNCA debe importar `config.settings`.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.audit.model import AuditRun, Category, Evidence, Finding, Severity
from tools.audit.redact import redact

__all__ = [
    "finding_to_dict",
    "run_to_dict",
    "write_json",
    "write_markdown",
]

# Indentacion fija: un cambio de indent reescribiria el archivo entero y
# ahogaria el diff real de hallazgos.
_INDENT = 2


def _sort_key(finding: Finding) -> tuple[str, str, str]:
    """Orden canonico: severidad, luego categoria, luego ID.

    Las severidades son 'S1'..'S4', asi que el orden alfabetico ya coloca lo
    mas grave primero — que es lo que el operador tiene que leer arriba.
    """
    return (
        Severity(finding.severity).value,
        Category(finding.category).value,
        finding.id,
    )


def _evidence_to_dict(evidence: Evidence) -> dict[str, object]:
    """Serializa una evidencia con la cita redactada."""
    return {
        "path": evidence.path,
        "line": evidence.line,
        "key": evidence.key,
        "quote": redact(evidence.quote),
        "anchor": redact(evidence.anchor),
    }


def finding_to_dict(finding: Finding) -> dict[str, object]:
    """Serializa un hallazgo. El ID se materializa porque es la clave del diff."""
    return {
        "id": finding.id,
        "category": Category(finding.category).value,
        "severity": Severity(finding.severity).value,
        "impact": redact(finding.impact),
        "remediation": redact(finding.remediation),
        "effort": finding.effort,
        "status": finding.status,
        "runtime_owner": finding.runtime_owner,
        "uncertain": finding.uncertain,
        "evidence": [_evidence_to_dict(ev) for ev in finding.evidence],
    }


def run_to_dict(run: AuditRun) -> dict[str, object]:
    """Estructura serializable de una corrida completa.

    `inspected` se rellena con las diez categorias en cero por la misma razon
    que `counts_by_category`: un cero explicito distingue "mire y no encontre
    nada" de "no corrio el checker".
    """
    inspected = {cat.value: 0 for cat in Category}
    for key, value in run.inspected.items():
        name = key.value if isinstance(key, Category) else str(key)
        inspected[name] = int(value)

    return {
        "run_id": run.run_id,
        "timestamp": run.timestamp,
        "commit_sha": run.commit_sha,
        "counts": {
            "total": len(run.findings),
            "by_severity": run.counts_by_severity(),
            "by_category": run.counts_by_category(),
        },
        "inspected": inspected,
        "findings": [
            finding_to_dict(f) for f in sorted(run.findings, key=_sort_key)
        ],
    }


def _write_text(path: Path, text: str) -> None:
    """Escribe UTF-8 con saltos LF explicitos, creando el directorio padre."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" es lo que hace que el artefacto sea byte-identico entre
    # Windows local y ubuntu-latest.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(run: AuditRun, path: Path) -> None:
    """Escribe el artefacto JSON en `path` (ruta explicita, nunca absoluta fija)."""
    payload = json.dumps(
        run_to_dict(run),
        sort_keys=True,
        indent=_INDENT,
        ensure_ascii=False,
    )
    _write_text(Path(path), payload + "\n")


def _render_finding(finding: Finding) -> list[str]:
    """Bloque markdown de un hallazgo."""
    anchors = ", ".join(f"`{redact(a)}`" for a in finding.anchors) or "_sin ancla_"
    marca = " ⚠️ incierto" if finding.uncertain else ""
    lines = [
        f"#### [{Severity(finding.severity).value}] `{finding.id}`{marca}",
        "",
        f"- **Ancla:** {anchors}",
        f"- **Impacto:** {redact(finding.impact)}",
        f"- **Arreglo:** {redact(finding.remediation)}",
        f"- **Esfuerzo:** {finding.effort} · **Estado:** {finding.status}",
    ]
    if finding.runtime_owner:
        # El checker estatico no reimplementa la salud en runtime: apunta al
        # verificador que ya existe (FR-017).
        lines.append(f"- **Verificacion en runtime:** {finding.runtime_owner}")
    lines.append("")
    return lines


def write_markdown(run: AuditRun, path: Path) -> None:
    """Escribe el artefacto markdown en `path`, con una seccion por categoria."""
    data = run_to_dict(run)
    inspected: dict[str, int] = data["inspected"]  # type: ignore[assignment]
    by_severity = run.counts_by_severity()

    ordenados = sorted(run.findings, key=_sort_key)
    por_categoria: dict[str, list[Finding]] = {cat.value: [] for cat in Category}
    for finding in ordenados:
        por_categoria[Category(finding.category).value].append(finding)

    lines: list[str] = [
        "# Auditoria estatica del repositorio",
        "",
        f"- **Corrida:** `{run.run_id}`",
        f"- **Fecha:** {run.timestamp}",
        f"- **Commit:** `{run.commit_sha}`",
        f"- **Hallazgos:** {len(run.findings)}",
        f"- **Ubicaciones inspeccionadas:** {sum(inspected.values())}",
        "",
        "## Resumen por severidad",
        "",
        "| Severidad | Hallazgos |",
        "| --- | --- |",
    ]
    for sev in Severity:
        lines.append(f"| {sev.value} | {by_severity[sev.value]} |")
    lines.append("")

    lines.append("## Hallazgos por categoria")
    lines.append("")

    # Orden de declaracion del enum: estable y agrupado por afinidad.
    for categoria in Category:
        nombre = categoria.value
        hallazgos = por_categoria[nombre]
        vistas = inspected.get(nombre, 0)
        lines.append(f"### {nombre}")
        lines.append("")
        # Esta linea se emite SIEMPRE, incluso con cero hallazgos: es la que
        # impide leer una categoria vacia como una categoria no auditada.
        lines.append(
            f"{len(hallazgos)} hallazgos, {vistas} ubicaciones inspeccionadas."
        )
        lines.append("")
        for finding in hallazgos:
            lines.extend(_render_finding(finding))

    _write_text(Path(path), "\n".join(lines).rstrip("\n") + "\n")
