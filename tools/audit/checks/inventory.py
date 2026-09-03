"""Checker de inventario de scripts (T013).

`scripts/` es la superficie de ejecucion del sistema: cada archivo de ahi
puede ser disparado por un cron de GitHub Actions, por el operador a mano, o
ser un one-shot que YA corrio contra produccion. Sin un registro escrito,
nadie puede responder tres preguntas operativas basicas:

1. Quien dispara esto? (workflow / manual / one-shot ya aplicado)
2. Es seguro volver a correrlo? (idempotencia)
3. Escribe en la base? (radio de daño si se corre por error)

Este modulo cierra ese hueco por partida doble: valida que
`docs/scripts_inventory.md` cubra EXACTAMENTE los scripts que existen en
disco, y deriva de la fuente los campos que se pueden derivar en vez de
confiar en la memoria de quien edito el documento.

QUE SE DERIVA Y QUE SE CURA
---------------------------
- `Writes DB` se DERIVA (`detect_db_writes`): es la salida de una funcion
  deterministica sobre el codigo, no una opinion. El documento y el test
  consumen la MISMA funcion, asi que no pueden discrepar.
- `Trigger` se DERIVA (`detect_trigger`): se busca la ruta del script dentro
  de los YAML de `.github/workflows/`. Un script referenciado por un workflow
  no puede quedar marcado como `manual` por descuido.
- `Idempotent`, `Owner loop` y `Notes` se CURAN a mano. `unknown` es una
  respuesta honesta para la idempotencia; una suposicion no lo es.

Solo stdlib. Este modulo NUNCA importa `config.settings`: levanta
RuntimeError en tiempo de import sin `DB_URL` y romperia la auditoria sin
secretos (FR-006).
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

from tools.audit.model import Category, Evidence, Finding, Severity
from tools.audit.registry import check

__all__ = [
    "COLUMNAS",
    "DOC_RELPATH",
    "ESCRITURAS",
    "IDEMPOTENCIAS",
    "OWNER_LOOPS",
    "SCRIPTS_DIRNAME",
    "TRIGGER_MANUAL",
    "TRIGGER_ONE_SHOT",
    "WORKFLOWS_RELPATH",
    "check_script_inventory",
    "detect_db_writes",
    "detect_trigger",
    "duplicated_rows",
    "iter_scripts",
    "parse_inventory",
    "render_inventory",
]


# ---------------------------------------------------------------------------
# Vocabulario compartido por el documento, el checker y los tests
# ---------------------------------------------------------------------------

DOC_RELPATH = "docs/scripts_inventory.md"
SCRIPTS_DIRNAME = "scripts"
WORKFLOWS_RELPATH = ".github/workflows"

# El orden es el orden de las columnas del documento. El parser localiza el
# encabezado comparando contra esta tupla, de modo que reordenar columnas en
# el markdown sin actualizar aqui rompe ruidosamente en vez de leer basura.
COLUMNAS = (
    "Script",
    "Trigger",
    "Idempotent",
    "Writes DB",
    "Owner loop",
    "Notes",
)

TRIGGER_MANUAL = "manual"
TRIGGER_ONE_SHOT = "one-shot-applied"

# `unknown` es deliberadamente valido: para varios scripts la idempotencia
# solo se puede afirmar leyendo el DDL completo que tocan. Declararlo
# desconocido es honesto; inventar un `yes` es lo que hace que alguien
# re-corra un cleanup destructivo.
IDEMPOTENCIAS = ("yes", "no", "unknown")

# Escrituras a la base: sin `unknown`. La columna se DERIVA de la fuente, y
# una derivacion deterministica siempre tiene respuesta.
ESCRITURAS = ("yes", "no")

# Los tres loops de CLAUDE.md, mas las dos categorias para todo lo demas.
OWNER_LOOPS = (
    "daily-pipeline",
    "pre-kickoff-analyst",
    "pending-resolver",
    "manual",
    "one-shot",
)

# Workflow -> loop dueño. Solo se usa para PROPONER un valor al regenerar el
# documento; el valor que manda es el del markdown, que es curado.
_LOOP_POR_WORKFLOW = {
    "morning.yml": "daily-pipeline",
    "closing.yml": "daily-pipeline",
    "evening.yml": "daily-pipeline",
    "weekly.yml": "daily-pipeline",
    "late_results.yml": "daily-pipeline",
    "pre_kickoff.yml": "pre-kickoff-analyst",
    "resolve_pending.yml": "pending-resolver",
}

# One-shots conocidos: YA corrieron contra produccion. Borrarlos es seguro;
# VOLVER A CORRERLOS no necesariamente. La lista viene del inventario
# operativo del proyecto, no de una heuristica sobre el nombre del archivo.
_ONE_SHOTS_CONOCIDOS = frozenset(
    {
        "db_cleanup_canonical.py",
        "db_cleanup_full.py",
        "db_cleanup_remaining.py",
        "one_shot_data_quality_cleanup.py",
        "migrate_add_ht_goals.py",
    }
)


# ---------------------------------------------------------------------------
# Derivacion: escribe en la base?
# ---------------------------------------------------------------------------

# Marcadores de escritura. Todos exigen mas que una palabra suelta
# (`INSERT INTO`, no `INSERT`) porque los docstrings de este repositorio
# estan en español y hablan constantemente de "insertar" y "actualizar": un
# patron de una sola palabra convertiria cada comentario en falso positivo.
_MARCADORES_ESCRITURA: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("INSERT INTO", re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE)),
    ("DELETE FROM", re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)),
    ("UPDATE ... SET", re.compile(r"\bUPDATE\s+[\w.\"]+\s+SET\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE)),
    ("CREATE TABLE", re.compile(r"\bCREATE\s+TABLE\b", re.IGNORECASE)),
    ("ALTER TABLE", re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)),
    ("DROP TABLE", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("to_sql", re.compile(r"\bto_sql\s*\(")),
    ("session.add", re.compile(r"\bsession\s*\.\s*add(_all)?\s*\(")),
    ("bulk_insert_mappings", re.compile(r"\bbulk_insert_mappings\s*\(")),
)


def _sin_comentarios(fuente: str) -> str:
    """La fuente sin comentarios `#`, con los tokens separados por espacio.

    Los comentarios se descartan porque un `# ya no hacemos DELETE FROM aqui`
    describe lo que el script NO hace. Los literales de cadena se CONSERVAN:
    en este proyecto el SQL vive dentro de `text(...)`, asi que removerlos
    dejaria ciega a la deteccion.

    Los tokens se unen con un espacio (no pegados) para que un patron que
    cruza tokens siga siendo alcanzable por un `\\s*` intermedio.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(fuente).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        # Un script que no tokeniza es un problema real, pero no es EL
        # problema de este checker: se escanea el texto crudo y seguimos.
        return fuente
    return " ".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)


def detect_db_writes(fuente: str) -> str:
    """`"yes"` si la fuente contiene algun marcador de escritura, si no `"no"`.

    Deterministica y compartida: el documento, el checker y el test la usan.
    Por eso la columna `Writes DB` no es una suposicion — es, por definicion,
    la salida de esta funcion sobre el codigo del script.
    """
    limpia = _sin_comentarios(fuente)
    for _, patron in _MARCADORES_ESCRITURA:
        if patron.search(limpia):
            return "yes"
    return "no"


def _marcadores_encontrados(fuente: str) -> list[str]:
    """Nombres de los marcadores de escritura presentes, para la evidencia."""
    limpia = _sin_comentarios(fuente)
    return [
        nombre for nombre, patron in _MARCADORES_ESCRITURA if patron.search(limpia)
    ]


def _lee(ruta: Path) -> str:
    """Contenido de un archivo, tolerante a bytes invalidos."""
    try:
        return ruta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Derivacion: quien lo dispara?
# ---------------------------------------------------------------------------

def _workflows(root: Path) -> list[Path]:
    """Los YAML de `.github/workflows/`, ordenados. Lista vacia si no existe."""
    carpeta = Path(root) / WORKFLOWS_RELPATH
    if not carpeta.is_dir():
        return []
    archivos = [
        ruta
        for ruta in carpeta.iterdir()
        if ruta.is_file() and ruta.suffix in (".yml", ".yaml")
    ]
    return sorted(archivos, key=lambda p: p.name)


def detect_trigger(root: Path, nombre: str) -> str:
    """Trigger derivado de la fuente: workflows que lo invocan, o `manual`.

    Se busca textualmente `scripts/<nombre>` en cada YAML en vez de parsear
    el `run:` con PyYAML: los comandos viven dentro de bloques literales y lo
    que importa es la referencia, no su estructura. Un script nombrado por
    dos workflows devuelve ambos separados por coma.

    Un one-shot conocido gana sobre `manual`: la diferencia entre "nadie lo
    dispara" y "ya se disparo una vez contra produccion" es exactamente la
    que evita que alguien lo vuelva a correr por curiosidad.
    """
    referencia = f"{SCRIPTS_DIRNAME}/{nombre}"
    encontrados = [
        ruta.name for ruta in _workflows(root) if referencia in _lee(ruta)
    ]
    if encontrados:
        return ", ".join(encontrados)
    if nombre in _ONE_SHOTS_CONOCIDOS:
        return TRIGGER_ONE_SHOT
    return TRIGGER_MANUAL


def _workflows_citados(trigger: str) -> list[str]:
    """Los nombres de workflow mencionados en una celda `Trigger`."""
    piezas = [pieza.strip().strip("`") for pieza in trigger.split(",")]
    return [pieza for pieza in piezas if pieza.endswith((".yml", ".yaml"))]


def _loop_propuesto(trigger: str, nombre: str) -> str:
    """Loop dueño propuesto a partir del trigger derivado."""
    if trigger == TRIGGER_ONE_SHOT or nombre in _ONE_SHOTS_CONOCIDOS:
        return "one-shot"
    for workflow in _workflows_citados(trigger):
        loop = _LOOP_POR_WORKFLOW.get(workflow)
        if loop:
            return loop
    return TRIGGER_MANUAL


# ---------------------------------------------------------------------------
# Lectura del arbol y del documento
# ---------------------------------------------------------------------------

def iter_scripts(root: Path) -> list[Path]:
    """Los `.py` de `scripts/`, ordenados por nombre. Nunca recursivo.

    `scripts/` es plano por convencion; recorrer subdirectorios solo
    arrastraria `__pycache__`.
    """
    carpeta = Path(root) / SCRIPTS_DIRNAME
    if not carpeta.is_dir():
        return []
    return sorted(
        (ruta for ruta in carpeta.glob("*.py") if ruta.is_file()),
        key=lambda p: p.name,
    )


def _limpia_celda(valor: str) -> str:
    """Quita backticks, negritas y el prefijo `scripts/` de una celda."""
    texto = valor.strip().replace("**", "").strip().strip("`").strip()
    if texto.startswith(f"{SCRIPTS_DIRNAME}/"):
        texto = texto[len(SCRIPTS_DIRNAME) + 1 :]
    return texto


def _es_separador(celdas: list[str]) -> bool:
    """True para la fila `|---|---|` que separa encabezado de cuerpo."""
    union = "".join(celdas)
    return bool(union) and set(union) <= set("-: ")


def _filas_crudas(path: Path) -> list[list[str]]:
    """Las filas de datos de la tabla del inventario, ya troceadas en celdas."""
    ruta = Path(path)
    if not ruta.is_file():
        return []
    texto = _lee(ruta)
    if not texto:
        return []

    esperado = [col.lower() for col in COLUMNAS]
    visto_encabezado = False
    filas: list[list[str]] = []

    for linea in texto.splitlines():
        recortada = linea.strip()
        if not recortada.startswith("|"):
            continue
        celdas = [celda.strip() for celda in recortada.strip("|").split("|")]

        if not visto_encabezado:
            if [celda.lower() for celda in celdas] == esperado:
                visto_encabezado = True
            continue

        if _es_separador(celdas) or len(celdas) != len(COLUMNAS):
            continue
        filas.append(celdas)

    return filas


def parse_inventory(path: Path) -> dict[str, dict[str, str]]:
    """Lee la tabla del inventario: nombre de script -> campos de su fila.

    Devuelve `{}` cuando el documento no existe o no contiene una tabla con
    el encabezado esperado. Ese vacio NO se disimula: el checker lo convierte
    en un hallazgo por cada script sin fila, que es justo la señal de que el
    documento falta o quedo con las columnas cambiadas.
    """
    filas: dict[str, dict[str, str]] = {}
    for celdas in _filas_crudas(path):
        nombre = _limpia_celda(celdas[0])
        if not nombre:
            continue
        # Una fila duplicada no se sobrescribe en silencio: se conserva la
        # primera y `duplicated_rows` reporta el duplicado por separado.
        filas.setdefault(
            nombre,
            {columna: celdas[i].strip() for i, columna in enumerate(COLUMNAS)},
        )
    return filas


def duplicated_rows(path: Path) -> list[str]:
    """Scripts que aparecen en mas de una fila del documento."""
    vistos: dict[str, int] = {}
    for celdas in _filas_crudas(path):
        nombre = _limpia_celda(celdas[0])
        if nombre:
            vistos[nombre] = vistos.get(nombre, 0) + 1
    return sorted(nombre for nombre, veces in vistos.items() if veces > 1)


# ---------------------------------------------------------------------------
# Renderizado del documento
# ---------------------------------------------------------------------------

_PREAMBULO = """# Inventario de scripts

Registro operativo de **todo** archivo `.py` que vive en `scripts/`. Existe
para responder, sin leer codigo, las tres preguntas que importan antes de
ejecutar cualquier cosa a mano:

1. **Trigger** — quien lo dispara: el nombre del workflow de
   `.github/workflows/` que lo invoca, `manual` si solo lo corre el operador,
   o `one-shot-applied` si ya se aplico contra produccion.
2. **Idempotent** — si volver a correrlo es seguro. `unknown` es una
   respuesta valida y honesta; una suposicion no lo es.
3. **Writes DB** — si el script escribe en PostgreSQL. **Esta columna se
   deriva de la fuente**, no se redacta a mano: es la salida de
   `tools.audit.checks.inventory.detect_db_writes()` sobre el codigo del
   script (busca `INSERT INTO`, `DELETE FROM`, `UPDATE ... SET`, `to_sql(`,
   DDL y escrituras ORM, ignorando comentarios).

**Owner loop** ata el script a uno de los tres loops de `CLAUDE.md`
(`daily-pipeline`, `pre-kickoff-analyst`, `pending-resolver`); todo lo demas
es `manual` o `one-shot`.

## Como se mantiene

El documento no se mantiene solo por disciplina: el checker `inventory` del
auditor estatico falla cuando un archivo de `scripts/` no tiene fila aqui, o
cuando una fila apunta a un script que ya no existe. Un script nuevo no puede
entrar sin dueño.

```bash
python -m pytest tests/test_scripts_inventory.py -v   # verifica el inventario
python -m tools.audit.checks.inventory --write        # regenera la tabla
```

La regeneracion **preserva** las columnas curadas (`Idempotent`, `Owner loop`,
`Notes`) de las filas que ya existen y solo recalcula lo derivable, para que
volver a generar nunca borre conocimiento operativo escrito a mano.
"""


def render_inventory(root: Path, doc_path: Path | None = None) -> str:
    """Construye el markdown del inventario para el arbol `root`.

    Fusiona tres fuentes, en este orden de precedencia por columna:

    - `Script` y `Trigger` y `Writes DB`: SIEMPRE derivados del arbol actual.
      Son verificables, asi que un valor curado que los contradiga es un bug,
      no una opinion a respetar.
    - `Idempotent`, `Owner loop`, `Notes`: se conserva lo que ya diga el
      documento; solo se rellena con un valor propuesto cuando la fila es
      nueva. Asi regenerar no destruye analisis manual.
    """
    raiz = Path(root)
    ruta_doc = Path(doc_path) if doc_path is not None else raiz / DOC_RELPATH
    curados = parse_inventory(ruta_doc)

    lineas = [_PREAMBULO.rstrip(), "", "## Inventario", ""]
    lineas.append("| " + " | ".join(COLUMNAS) + " |")
    lineas.append("|" + "|".join(["---"] * len(COLUMNAS)) + "|")

    for ruta in iter_scripts(raiz):
        nombre = ruta.name
        previo = curados.get(nombre, {})
        trigger = detect_trigger(raiz, nombre)
        escribe = detect_db_writes(_lee(ruta))

        idempotente = previo.get("Idempotent", "").strip() or "unknown"
        loop = previo.get("Owner loop", "").strip() or _loop_propuesto(
            trigger, nombre
        )
        notas = previo.get("Notes", "").strip()
        if not notas and trigger == TRIGGER_ONE_SHOT:
            notas = (
                "One-shot YA aplicado contra produccion. **Re-ejecutar NO es "
                "seguro**: verificar el estado de la tabla antes de siquiera "
                "considerarlo."
            )
        if not notas:
            notas = "Sin notas operativas registradas."

        lineas.append(
            "| "
            + " | ".join(
                [
                    f"`{nombre}`",
                    trigger,
                    idempotente,
                    escribe,
                    loop,
                    notas,
                ]
            )
            + " |"
        )

    lineas.append("")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# El checker
# ---------------------------------------------------------------------------

@check("inventory", Category.CONVENTION_VIOLATION)
def check_script_inventory(root: Path) -> tuple[list[Finding], int]:
    """Todo script tiene dueño registrado, y todo registro tiene script."""
    raiz = Path(root)
    ruta_doc = raiz / DOC_RELPATH
    scripts = iter_scripts(raiz)
    filas = parse_inventory(ruta_doc)
    hallazgos: list[Finding] = []

    en_disco = {ruta.name for ruta in scripts}

    # --- 1. Script sin fila: entro al repo sin dueño ------------------------
    for ruta in scripts:
        if ruta.name in filas:
            continue
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S2,
                evidence=[
                    Evidence(path=f"{SCRIPTS_DIRNAME}/{ruta.name}"),
                    Evidence(path=DOC_RELPATH, key=ruta.name),
                ],
                impact=(
                    f"`{SCRIPTS_DIRNAME}/{ruta.name}` no tiene fila en "
                    f"`{DOC_RELPATH}`: nadie puede saber quien lo dispara, si "
                    "es idempotente ni si escribe en la base sin leer el "
                    "codigo. Un script sin dueño es el que alguien corre a "
                    "mano en produccion pensando que es inofensivo."
                ),
                remediation=(
                    f"Agregar una fila para `{ruta.name}` en `{DOC_RELPATH}` "
                    "con su trigger, idempotencia, escritura a la base y loop "
                    "dueño, o regenerar la tabla con "
                    "`python -m tools.audit.checks.inventory --write`."
                ),
                effort="S",
            )
        )

    # --- 2. Fila fantasma: el documento describe algo que ya no existe ------
    for nombre in sorted(set(filas) - en_disco):
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S3,
                evidence=[Evidence(path=DOC_RELPATH, key=nombre)],
                impact=(
                    f"`{DOC_RELPATH}` documenta `{nombre}`, que ya no existe "
                    f"en `{SCRIPTS_DIRNAME}/`. Un inventario con entradas "
                    "muertas se deja de leer, y entonces deja de proteger."
                ),
                remediation=(
                    f"Eliminar la fila de `{nombre}` del inventario, o "
                    "restaurar el script si su borrado fue accidental."
                ),
                effort="S",
            )
        )

    # --- 3. Fila duplicada: dos verdades para el mismo script --------------
    for nombre in duplicated_rows(ruta_doc):
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S3,
                evidence=[Evidence(path=DOC_RELPATH, key=nombre)],
                impact=(
                    f"`{nombre}` aparece en mas de una fila del inventario. "
                    "Dos filas pueden contradecirse y el lector no tiene forma "
                    "de saber cual es la vigente."
                ),
                remediation=(
                    f"Dejar una sola fila para `{nombre}`, fusionando las "
                    "notas de las duplicadas."
                ),
                effort="S",
            )
        )

    # --- 4. Trigger que nombra un workflow inexistente ----------------------
    nombres_workflow = {ruta.name for ruta in _workflows(raiz)}
    for nombre in sorted(filas):
        trigger = filas[nombre].get("Trigger", "")
        for workflow in _workflows_citados(trigger):
            if workflow in nombres_workflow:
                continue
            hallazgos.append(
                Finding(
                    category=Category.CONVENTION_VIOLATION,
                    severity=Severity.S2,
                    evidence=[Evidence(path=DOC_RELPATH, key=f"{nombre} -> {workflow}")],
                    impact=(
                        f"El inventario dice que `{nombre}` lo dispara "
                        f"`{workflow}`, pero ese archivo no existe en "
                        f"`{WORKFLOWS_RELPATH}/`. O el script quedo sin cron "
                        "y nadie lo noto, o el documento cita un workflow "
                        "renombrado."
                    ),
                    remediation=(
                        f"Corregir el trigger de `{nombre}` al workflow real, "
                        f"o marcarlo `{TRIGGER_MANUAL}` si ya no tiene cron."
                    ),
                    effort="S",
                )
            )

    # --- 5. One-shot aplicado sin nota de seguridad de re-ejecucion ---------
    for nombre in sorted(filas):
        fila = filas[nombre]
        if fila.get("Trigger", "").strip() != TRIGGER_ONE_SHOT:
            continue
        if fila.get("Notes", "").strip():
            continue
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S2,
                evidence=[Evidence(path=DOC_RELPATH, key=nombre)],
                impact=(
                    f"`{nombre}` esta marcado como one-shot ya aplicado pero "
                    "su celda de notas esta vacia: no dice si volver a "
                    "correrlo es seguro. Borrar un one-shot es inofensivo; "
                    "RE-EJECUTARLO puede no serlo."
                ),
                remediation=(
                    f"Escribir en las notas de `{nombre}` si su re-ejecucion "
                    "es segura y bajo que condiciones."
                ),
                effort="S",
            )
        )

    # --- 6. `Writes DB` que contradice a la fuente --------------------------
    for ruta in scripts:
        fila = filas.get(ruta.name)
        if not fila:
            continue
        declarado = fila.get("Writes DB", "").strip().lower()
        derivado = detect_db_writes(_lee(ruta))
        if declarado == derivado:
            continue
        marcadores = _marcadores_encontrados(_lee(ruta))
        detalle = ", ".join(marcadores) if marcadores else "ninguno"
        hallazgos.append(
            Finding(
                category=Category.CONVENTION_VIOLATION,
                severity=Severity.S2,
                evidence=[
                    Evidence(path=DOC_RELPATH, key=f"{ruta.name}/Writes DB"),
                    Evidence(path=f"{SCRIPTS_DIRNAME}/{ruta.name}"),
                ],
                impact=(
                    f"El inventario declara `Writes DB = {declarado or 'vacio'}` "
                    f"para `{ruta.name}`, pero su fuente dice `{derivado}` "
                    f"(marcadores hallados: {detalle}). El operador decide si "
                    "corre algo a mano leyendo esta columna."
                ),
                remediation=(
                    f"Poner `Writes DB = {derivado}` en la fila de "
                    f"`{ruta.name}`, o regenerar la tabla con "
                    "`python -m tools.audit.checks.inventory --write`."
                ),
                effort="S",
            )
        )

    # Ubicaciones inspeccionadas: cada script del arbol y cada fila del
    # documento. Sin este conteo, "inventario limpio" e "inventario que nunca
    # se leyo" producirian exactamente el mismo reporte.
    inspeccionadas = len(scripts) + len(filas)

    hallazgos.sort(key=lambda hallazgo: (hallazgo.severity.value, hallazgo.id))
    return hallazgos, inspeccionadas


def _main() -> int:
    """`--write` regenera el documento; sin flags, solo lo imprime."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m tools.audit.checks.inventory",
        description="Regenera docs/scripts_inventory.md desde el arbol real.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Escribe el resultado en docs/scripts_inventory.md.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Raiz del repositorio (por defecto, la del propio paquete).",
    )
    args = parser.parse_args()

    raiz = (
        Path(args.root).resolve()
        if args.root
        else Path(__file__).resolve().parents[3]
    )
    contenido = render_inventory(raiz)

    if args.write:
        destino = raiz / DOC_RELPATH
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
        print(f"Inventario regenerado: {destino}")
    else:
        print(contenido)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
