"""El unico comando documentado del auditor: `python -m tools.audit`.

    python -m tools.audit [--ci] [--out audits/] [--root .]

Corre TODOS los checkers registrados, ordena los hallazgos por riesgo,
escribe `audits/latest.json` y `audits/latest.md`, y compara contra la
corrida anterior para que un defecto arreglado quede marcado `resolved` en
vez de desvanecerse.

Tres decisiones que no son de estilo:

1. Se itera `all_checks()`. NUNCA una lista de checkers escrita a mano: una
   lista manual convierte a un checker nuevo en una categoria invisible, que
   es exactamente el modo de fallo que esta auditoria persigue.

2. Este proceso no importa `config.settings` ni `config.database`, no abre la
   base, no llama a The Odds API ni a Anthropic y no toca el lock del
   orchestrator. Puede correr mientras el cron de la mañana esta corriendo
   (FR-006). El `commit_sha` se lee de los archivos de `.git`, sin invocar a
   `git` como subproceso.

3. Lo unico que se escribe queda bajo `--out`.

`--ci` corre solo el subconjunto barato (importes que no resuelven +
centralizacion de configuracion), no escribe artefactos — publicar un
`latest.json` parcial corromperia la linea base del diff — e imprime cada
violacion con su `archivo:linea` antes de salir con codigo 1.

Solo stdlib. Este modulo NUNCA debe importar `config.settings`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.audit.checks import load_all
from tools.audit.diff import diff_runs, load_run, reassignments
from tools.audit.model import AuditRun, Category, Finding, Severity
from tools.audit.rank import rank, risk_key, top_risks
from tools.audit.registry import CheckSpec, all_checks
from tools.audit.report import write_json, write_markdown

__all__ = [
    "CHECKS_GATE",
    "GateInvalido",
    "build_run",
    "commit_sha",
    "main",
]

# El subconjunto barato del gate de CI. Son los dos defectos que un pull
# request puede introducir y que rompen produccion de inmediato: un import
# que no resuelve tumba la corrida al arrancar, y una constante de
# configuracion declarada fuera de `config/settings.py` crea una segunda
# fuente de verdad que cambia cuanto se apuesta.
CHECKS_GATE: tuple[str, ...] = ("broken-reference", "config-inconsistency")

_NOMBRE_JSON = "latest.json"
_NOMBRE_MD = "latest.md"


# ---------------------------------------------------------------------------
# Metadatos de la corrida
# ---------------------------------------------------------------------------

def _git_dir(root: Path) -> Path | None:
    """Directorio `.git` real, resolviendo el caso de worktree.

    En un worktree `.git` es un ARCHIVO con `gitdir: <ruta>`; ignorarlo
    dejaria el commit en 'unknown' justo en el entorno donde mas se usa.
    """
    ruta = root / ".git"
    if ruta.is_dir():
        return ruta
    if ruta.is_file():
        try:
            texto = ruta.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if texto.startswith("gitdir:"):
            destino = Path(texto.split(":", 1)[1].strip())
            if not destino.is_absolute():
                destino = (root / destino).resolve()
            return destino if destino.is_dir() else None
    return None


def _leer(path: Path) -> str | None:
    """Contenido de un archivo de texto, o None si no se puede leer."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def commit_sha(root: Path) -> str:
    """SHA del commit actual leido de `.git`, o 'unknown'.

    Se leen archivos en vez de invocar `git`: el auditor tiene que correr en
    un runner sin git configurado y un subproceso que falla no puede ser la
    razon de que no haya reporte.
    """
    git_dir = _git_dir(Path(root))
    if git_dir is None:
        return "unknown"

    head = _leer(git_dir / "HEAD")
    if not head:
        return "unknown"
    if not head.startswith("ref:"):
        return head

    ref = head.split(":", 1)[1].strip()

    # En un worktree las refs viven en el directorio comun, no en el propio.
    directorios = [git_dir]
    commondir = _leer(git_dir / "commondir")
    if commondir:
        comun = Path(commondir)
        if not comun.is_absolute():
            comun = (git_dir / comun).resolve()
        directorios.append(comun)

    for base in directorios:
        directo = _leer(base / ref)
        if directo:
            return directo
        empaquetadas = _leer(base / "packed-refs")
        if empaquetadas:
            for linea in empaquetadas.splitlines():
                if linea.startswith(("#", "^")) or not linea.strip():
                    continue
                partes = linea.split()
                if len(partes) == 2 and partes[1] == ref:
                    return partes[0]

    return "unknown"


def _run_id(findings: list[Finding], sha: str) -> str:
    """ID de corrida derivado del CONTENIDO, no del reloj.

    Un ID aleatorio o basado en la hora haria imposible comparar dos
    corridas byte a byte: el artefacto cambiaria aunque el arbol no. Al
    derivarlo del conjunto de hallazgos, dos corridas sobre el mismo arbol
    producen el mismo ID y cualquier diferencia en el archivo es una
    diferencia real de hallazgos.
    """
    partes = sorted(f.id for f in findings)
    crudo = sha + "\n" + "\n".join(partes)
    return "run-" + hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Ejecucion
# ---------------------------------------------------------------------------

class GateInvalido(LookupError):
    """El gate nombra checkers que el registro no conoce."""


def _seleccionar(solo: tuple[str, ...] | None) -> list[CheckSpec]:
    """Checkers a ejecutar, siempre a partir del registro.

    Cuando `solo` esta definido se filtra el registro por nombre; no se
    construye una lista aparte. Pero filtrar no basta: si alguien renombra
    un checker del gate, el filtro simplemente no lo encuentra, el gate
    corre con un checker menos y CI sigue saliendo verde. Un gate que
    encoje en silencio es peor que no tener gate, porque el equipo cree
    estar protegido. Por eso un nombre del gate sin checker registrado es
    un error duro: el build se cae aqui, no meses despues en produccion.
    """
    especificaciones = all_checks()
    if solo is None:
        return especificaciones

    permitidos = set(solo)
    seleccion = [spec for spec in especificaciones if spec.name in permitidos]

    faltantes = sorted(permitidos - {spec.name for spec in especificaciones})
    if faltantes:
        raise GateInvalido(
            "el gate nombra checkers que no existen en el registro: "
            + ", ".join(faltantes)
        )
    return seleccion


def build_run(
    root: Path,
    *,
    solo: tuple[str, ...] | None = None,
    timestamp: str | None = None,
) -> AuditRun:
    """Ejecuta los checkers seleccionados y arma la corrida, ya ordenada.

    Las ubicaciones inspeccionadas de un checker se acreditan a TODAS sus
    categorias: el checker de referencias mira el grafo de imports una sola
    vez y de esa pasada salen `broken-reference`, `orphan-code` y
    `circular-dependency`. Acreditarlas solo a la primaria dejaria a las
    otras dos en 'cero ubicaciones inspeccionadas', que es la firma de un
    checker que no corrio.
    """
    raiz = Path(root)
    hallazgos: list[Finding] = []
    inspeccionadas: dict[str, int] = {cat.value: 0 for cat in Category}

    for spec in _seleccionar(solo):
        encontrados, vistas = spec.run(raiz)
        hallazgos.extend(encontrados)
        for categoria in spec.categories:
            inspeccionadas[categoria.value] += vistas

    momento = timestamp or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    sha = commit_sha(raiz)
    ordenados = rank(hallazgos)

    return AuditRun(
        run_id=_run_id(ordenados, sha),
        timestamp=momento,
        commit_sha=sha,
        findings=ordenados,
        inspected=inspeccionadas,
    )


# ---------------------------------------------------------------------------
# Salida por consola
# ---------------------------------------------------------------------------

def _ancla(finding: Finding) -> str:
    """Primera ancla del hallazgo, para la linea de consola."""
    return finding.anchors[0] if finding.anchors else "(sin ancla)"


def _imprimir_resumen(run: AuditRun, elapsed: float) -> None:
    """Resumen legible: conteos, top de riesgo y tiempo de la corrida."""
    por_severidad = run.counts_by_severity()
    abiertos = [f for f in run.findings if f.status == "open"]

    print("")
    print("=" * 62)
    print("AUDITORIA ESTATICA DEL REPOSITORIO")
    print("=" * 62)
    print(f"Corrida        : {run.run_id}")
    print(f"Commit         : {run.commit_sha}")
    print(f"Hallazgos      : {len(run.findings)} ({len(abiertos)} abiertos)")
    print(
        "Por severidad  : "
        + "  ".join(f"{sev.value}={por_severidad[sev.value]}" for sev in Severity)
    )
    print(
        f"Ubicaciones    : {sum(run.inspected.values())} inspeccionadas "
        f"en {len(run.inspected)} categorias"
    )

    principales = top_risks(run.findings, limit=5)
    if principales:
        print("")
        print("Lo que mas puede costar dinero primero:")
        for finding in principales:
            print(
                f"  [{Severity(finding.severity).value}] "
                f"{Category(finding.category).value} · {_ancla(finding)}"
            )

    print("")
    print(f"Completado en {elapsed:.2f}s")


def _imprimir_gate(hallazgos: list[Finding]) -> None:
    """Violaciones del gate, una por linea, con su `archivo:linea`.

    Se reciben ya ordenadas por riesgo (no en el orden canonico del
    artefacto): el autor del pull request suele leer solo las primeras
    lineas del log de CI, asi que arriba tiene que ir lo que de verdad
    bloquea, no lo que empieza con la letra mas chica.
    """
    print("")
    print("GATE DE CI — violaciones que bloquean el merge:")
    for finding in hallazgos:
        print(
            f"  [{Severity(finding.severity).value}] "
            f"{Category(finding.category).value} · {_ancla(finding)}"
        )
        print(f"      {finding.impact}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.audit",
        description=(
            "Auditoria estatica del repositorio. No abre la base de datos, "
            "no llama a ninguna API y no necesita ningun secreto."
        ),
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help=(
            "Corre solo el subconjunto barato del gate "
            f"({', '.join(CHECKS_GATE)}) y sale con codigo 1 si hay "
            "violaciones. No escribe artefactos."
        ),
    )
    parser.add_argument(
        "--out",
        default="audits",
        help="Directorio de salida de los artefactos (default: audits).",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Raiz del repositorio a auditar (default: el directorio actual).",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help=(
            "Fija la marca de tiempo del artefacto (ISO-8601). Sirve para "
            "reproducir una corrida byte a byte; sin esta bandera se usa la "
            "hora UTC actual."
        ),
    )
    return parser


def _consola_utf8() -> None:
    """Evita que un impacto con acentos tumbe la corrida en una consola cp1252.

    Mismo patron que los entry points de `scripts/`. Se hace dentro de
    `main()` y no al importar el modulo para que importar `tools.audit` no
    tenga efectos secundarios sobre el stdout del proceso que lo importa
    (pytest, entre otros). El `hasattr` cubre a los stdout sustitutos, que
    no exponen `reconfigure`.
    """
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            try:
                flujo.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # Un stdout que no admite reconfiguracion no es motivo para
                # quedarse sin auditoria.
                pass


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    _consola_utf8()
    args = _parser().parse_args(argv)
    raiz = Path(args.root).resolve()

    if not raiz.is_dir():
        print(f"ERROR: la raiz '{raiz}' no es un directorio.", file=sys.stderr)
        return 2

    inicio = time.perf_counter()

    # Importa todos los modulos de `checks/` para que se registren. Sin
    # esto el registro estaria vacio y la auditoria reportaria un arbol
    # impecable por la peor de las razones.
    load_all()

    if args.ci:
        try:
            corrida = build_run(
                raiz, solo=CHECKS_GATE, timestamp=args.timestamp
            )
        except GateInvalido as error:
            # Preferimos tumbar el build antes que correr un gate incompleto:
            # un verde falso es la unica salida peor que un rojo.
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

        elapsed = time.perf_counter() - inicio
        violaciones = sorted(
            (f for f in corrida.findings if f.status == "open"),
            key=risk_key,
        )
        if violaciones:
            _imprimir_gate(violaciones)
            print("")
            print(
                f"{len(violaciones)} violaciones del gate "
                f"({elapsed:.2f}s). Build bloqueado."
            )
            return 1
        # Se nombra el subconjunto que corrio: un "limpio" sin decir que se
        # reviso es indistinguible de un gate que no reviso nada.
        print(
            f"Gate de CI limpio ({elapsed:.2f}s) — "
            f"{len(CHECKS_GATE)} checkers: {', '.join(CHECKS_GATE)}."
        )
        return 0

    corrida = build_run(raiz, timestamp=args.timestamp)

    destino = Path(args.out)
    if not destino.is_absolute():
        destino = raiz / destino

    # El diff se hace contra el artefacto que ya vive en el destino, antes
    # de sobrescribirlo.
    previa = load_run(destino / _NOMBRE_JSON)
    publicada = diff_runs(previa, corrida)

    write_json(publicada, destino / _NOMBRE_JSON)
    write_markdown(publicada, destino / _NOMBRE_MD)

    elapsed = time.perf_counter() - inicio
    _imprimir_resumen(publicada, elapsed)

    resueltos = [f for f in publicada.findings if f.status == "resolved"]
    if resueltos:
        print(f"Resueltos desde la corrida anterior: {len(resueltos)}")

    for reasignacion in reassignments(previa, corrida):
        print(
            f"ID reasignado: {reasignacion.id_previo} -> "
            f"{reasignacion.id_actual} ({reasignacion.motivo})"
        )

    print(f"Artefactos: {destino / _NOMBRE_JSON}, {destino / _NOMBRE_MD}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
